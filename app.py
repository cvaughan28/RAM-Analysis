"""
Data Center Electrical RAM Analysis Tool
=========================================
Streamlit application for behind-the-meter, fully islanded data centers.

Scope: electrical system reliability only (generation + distribution).

Run:
    python -m streamlit run app.py

Tabs:
  Generator Fleet     — define mixed fleet composition
  Results             — system availability and component breakdown
  Elec. Components    — MTBF/MTTR overrides for electrical equipment
  Sensitivity         — tornado chart and parameter sweep
  Mission Analysis    — standby mission success probability
  Topology Compare    — save and compare multiple configurations
  Audit & QA          — assumptions register, calculation trace, export
  Methodology         — formulas and data source documentation
"""

import math
import copy
import datetime
import io
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

from defaults import COMP_DEFAULTS, GEN_DEFAULTS, GEN_TYPE_LIST, UPS_SYSTEM_REFS
from models import (
    TopologyConfig, GeneratorGroup, default_fleet,
    calculate_system, sweep_parameter,
)
from reliability import (
    annual_downtime_minutes, availability_to_nines,
    component_availability, mixed_fleet_kofn_availability,
    mission_reliability, kofn_availability,
)
from pdf_report import build_pdf_report
from diagrams import build_topology_diagram
from sld import build_sld, sld_to_png_bytes


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="DC Electrical RAM Analysis",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_avail(a: float) -> str:
    return f"{a * 100:.6f}%"

def fmt_nines(n: float) -> str:
    return f"{n:.2f} nines"

def fmt_downtime(minutes: float) -> str:
    if minutes < 1:
        return f"{minutes * 60:.1f} sec/yr"
    if minutes < 120:
        return f"{minutes:.2f} min/yr"
    return f"{minutes / 60:.2f} hr/yr"


def _interpret_kofn(n: int, k: int) -> str:
    """Human-readable label for the (n, k) path topology choice."""
    if n == 1:
        return "Radial (Tier I / II reference)"
    if n == 2 and k == 1:
        return "2N — dual-cord, tolerates 1 path failure"
    if n == 2 and k == 2:
        return "Series — both paths required (no redundancy at path layer)"
    if n == 3 and k == 2:
        return "3N/2 — distributed redundancy, tolerates 1 of 3 path failures"
    if n == 4 and k == 3:
        return "4N/3 (or 3+1) — block redundancy, tolerates 1 of 4 path failures"
    if n == 5 and k == 4:
        return "5N/4 — tolerates 1 of 5 path failures"
    if n == 6 and k == 5:
        return "6N/5 — tolerates 1 of 6 path failures"
    if k == n - 1:
        return f"{n}N/{k} — distributed redundancy, tolerates 1 of {n} path failures"
    if k == n:
        return f"{n} paths, all required (no redundancy at path layer)"
    return f"{k}-of-{n} — tolerates {n - k} simultaneous path failure(s)"


# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------

def _fleet_to_df(groups: list) -> pd.DataFrame:
    rows = []
    for g in groups:
        rows.append({
            "Group Name": g.name,
            "Count": g.count,
            "MTBF (hours)": g.mtbf_hours,
            "MTTR (hours)": g.mttr_hours,
            "FTS Probability": g.fts_probability,
            "FTLR Probability": g.ftlr_probability,
            "Avail. (%)": round(g.availability * 100, 6),
            "Source / Notes": g.source,
        })
    return pd.DataFrame(rows)


def _df_to_fleet(df: pd.DataFrame) -> list:
    groups = []
    for _, row in df.iterrows():
        try:
            count = max(1, int(row["Count"]))
            mtbf = max(1.0, float(row["MTBF (hours)"]))
            mttr = max(0.0, float(row["MTTR (hours)"]))
            fts = max(0.0, min(1.0, float(row["FTS Probability"])))
            ftlr = max(0.0, min(1.0, float(row.get("FTLR Probability", 0.0))))
            groups.append(GeneratorGroup(
                name=str(row["Group Name"]),
                count=count,
                mtbf_hours=mtbf,
                mttr_hours=mttr,
                fts_probability=fts,
                ftlr_probability=ftlr,
                source=str(row.get("Source / Notes", "User-defined")),
            ))
        except Exception:
            pass
    return groups if groups else default_fleet()


def get_fleet() -> list:
    if "fleet_df" not in st.session_state:
        st.session_state["fleet_df"] = _fleet_to_df(default_fleet())
    return _df_to_fleet(st.session_state["fleet_df"])


def get_fleet_total() -> int:
    return sum(g.count for g in get_fleet())


# ---------------------------------------------------------------------------
# Preset topology configurations
# ---------------------------------------------------------------------------

TOPOLOGY_PRESETS = {
    "Tier IV — 2N Dual Path with UPS (default)": {
        "desc": "Two independent power paths, each with UPS 3-of-4 modules. "
                "2 dedicated generators, 1 required per path. CCF beta=0.02.",
        "config_kwargs": dict(
            num_paths=2, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=False,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=4, ups_modules_required=3,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=2, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.02, mission_duration_hours=96.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 2, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
    "Tier IV — 2N with MV Distribution": {
        "desc": "Two paths including MV breakers, transformer, and UPS. "
                "Adds MV switchgear layer upstream of UPS.",
        "config_kwargs": dict(
            num_paths=2, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=True,
            include_mv_breaker=True, include_mv_bus=True,
            include_ats=True, include_transformer=True,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=4, ups_modules_required=3,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=2, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.02, mission_duration_hours=96.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 2, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
    "Tier IV — N+1 Shared Generator Pool": {
        "desc": "Shared generator pool feeding two distribution paths. "
                "3 generators, 2 required (N+1 pool). CCF modeled on dist paths. "
                "Note: shared-pool arrangement has HIGHER coupling between paths "
                "(shared gens, shared controls), so default beta is bumped from "
                "0.02 to 0.030 vs typical 2N.",
        "config_kwargs": dict(
            num_paths=2, gen_arrangement="Shared Pool",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=False,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=4, ups_modules_required=3,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=2, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.030, mission_duration_hours=96.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 3, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 2,
    },
    "Tier III — Single Path with UPS (radial reference)": {
        "desc": "Single radial path with UPS. No redundancy. "
                "Use as baseline comparison to quantify redundancy gain.",
        "config_kwargs": dict(
            num_paths=1, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=False,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=2, ups_modules_required=1,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=1, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=False, ccf_beta=0.02, mission_duration_hours=96.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 1, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
    "Tier IV — 2N No CCF (independence assumed)": {
        "desc": "2N dual path, UPS, but CCF disabled. "
                "Shows 'optimistic independence' model vs. beta-factor model.",
        "config_kwargs": dict(
            num_paths=2, paths_required=1, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=False,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=4, ups_modules_required=3,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=2, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=False, ccf_beta=0.0, mission_duration_hours=96.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 2, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },

    # ── Distributed-redundancy SLDs (3N/2, 4N/3, 3+1) ────────────────────────
    "Tier IV — 3N/2 Distributed Redundancy (3 paths, any 2 needed)": {
        "desc": "Three independent power chains; loads cross-corded so any 1 path "
                "may fail without dropping load. Each chain carries ~50% of full "
                "load. Each chain has its own utility + dedicated backup gen. "
                "Maps to PDF page 2. CCF default tuned to beta=0.015 (lower than "
                "2N=0.020 because 3 chains imply more routing/control diversity).",
        "config_kwargs": dict(
            num_paths=3, paths_required=2, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=True,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=2, ups_modules_required=1,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=1, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.015, mission_duration_hours=96.0,
            power_source_mode="grid_with_backup",
            grid_mtbf_hours=8760.0, grid_mttr_hours=2.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 1, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
    "Tier IV — 4N/3 Block Redundancy (4 paths, any 3 needed)": {
        "desc": "Four independent power chains; loads cross-corded across path "
                "pairs (6 load groups). Tolerates 1 of 4 path failures. Each "
                "chain at ~75% of full load. Maps to PDF page 3. CCF default "
                "tuned to beta=0.010 (lower than 2N because 4 separate chains "
                "imply substantial diversity in routing, fuel, and controls).",
        "config_kwargs": dict(
            num_paths=4, paths_required=3, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=True,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=2, ups_modules_required=1,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=1, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.010, mission_duration_hours=96.0,
            power_source_mode="grid_with_backup",
            grid_mtbf_hours=8760.0, grid_mttr_hours=2.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 1, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
    "Tier IV — 3+1 Distributed (4 paths, 3 active + 1 standby)": {
        "desc": "Same RAM math as 4N/3 (4 paths, tolerate 1 failure); the '+1' "
                "represents an operational standby philosophy rather than a "
                "different reliability shape. Maps to PDF page 4. CCF default "
                "beta=0.010 (matches 4N/3 — same physical diversity).",
        "config_kwargs": dict(
            num_paths=4, paths_required=3, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=True,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=2, ups_modules_required=1,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=1, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.010, mission_duration_hours=96.0,
            power_source_mode="grid_with_backup",
            grid_mtbf_hours=8760.0, grid_mttr_hours=2.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 1, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },

    # ── Tier reference SLDs ──────────────────────────────────────────────────
    "Tier III — Concurrently Maintainable (radial critical, N+1 gens)": {
        "desc": "Single critical path (radial through one UPS), single utility "
                "feed, 2 backup gens (N+1) with shared bus, and a maintenance "
                "bypass at each component. Reliability is dominated by the "
                "single-path components (UPS, PDU); the dual gens and dual "
                "upstream provide concurrent-maintenance capability but no "
                "fault tolerance for the critical load. Maps to PDF page 9.",
        "config_kwargs": dict(
            num_paths=1, paths_required=1, gen_arrangement="Shared Pool",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=True,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=2, ups_modules_required=1,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=1, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=False, ccf_beta=0.0, mission_duration_hours=96.0,
            power_source_mode="grid_with_backup",
            grid_mtbf_hours=8760.0, grid_mttr_hours=2.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 2, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
    "Tier IV — Fault Tolerant Minimum (2N dual UPS, single utility)": {
        "desc": "Dual UPS systems, dual LV switchboards, 2 backup gens (N+1), "
                "single utility feed. Fault tolerant for any single fault AND "
                "concurrently maintainable. Maps to PDF page 10. CCF default "
                "beta=0.020 (standard 2N: single utility = some shared coupling, "
                "but dual UPS and LV swbd downstream).",
        "config_kwargs": dict(
            num_paths=2, paths_required=1, gen_arrangement="Dedicated per Path",
            include_paralleling_switchgear=True, include_gen_breaker=False,
            include_mv_breaker=False, include_mv_bus=False,
            include_ats=True, include_transformer=True,
            include_lv_bus=True, include_lv_breaker=True,
            include_ups=True, ups_modules_per_path=2, ups_modules_required=1,
            include_ups_battery=True, include_ups_sts=False,
            include_pdu=True, pdus_per_path=1, pdus_required=1,
            include_rack_pdu=False, include_it_psu=False,
            enable_ccf=True, ccf_beta=0.020, mission_duration_hours=96.0,
            power_source_mode="grid_with_backup",
            grid_mtbf_hours=8760.0, grid_mttr_hours=2.0,
        ),
        "fleet": [{"name": "Diesel Gen", "count": 2, "mtbf": 4380, "mttr": 33.9, "fts": 0.0013, "ftlr": 0.00331}],
        "gen_required": 1,
    },
}


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar():
    st.sidebar.title("⚙️ System Configuration")

    # ── Power source mode ────────────────────────────────────────────────────
    st.sidebar.header("Power Source")
    power_source_mode = st.sidebar.radio(
        "Power source configuration",
        options=["islanded", "grid_with_backup"],
        index=0,
        format_func=lambda x: {
            "islanded":          "Islanded (behind-the-meter, gens only)",
            "grid_with_backup":  "Grid-connected + backup gens",
        }[x],
        help=(
            "**Islanded** — fully behind-the-meter, no utility connection; "
            "generators are the primary source. **Grid-connected + backup** — "
            "utility grid is primary, generators are standby. In grid mode the "
            "source is grid OR (grid down AND backup mission succeeds)."
        ),
    )

    # Grid reliability inputs (only shown in grid mode)
    grid_mtbf_hours = 8_760.0
    grid_mttr_hours = 2.0
    if power_source_mode == "grid_with_backup":
        st.sidebar.markdown(
            "<small><i>Grid feed reliability — replace defaults with utility-"
            "supplied SAIDI/SAIFI data or IEEE 1366 reference for your service "
            "territory.</i></small>",
            unsafe_allow_html=True,
        )
        grid_mtbf_hours = st.sidebar.number_input(
            "Grid MTBF / MTTF (hours)",
            min_value=10.0, max_value=1_000_000.0,
            value=8_760.0, step=100.0,
            help=(
                "Mean time between grid outages. Default 8,760 h = 1 outage / year. "
                "Typical US urban grid (IEEE 1366): SAIFI ≈ 1.3/yr → MTBF ≈ 6,700 h. "
                "Single feeder from a hardened substation: 4,000–8,000 h. "
                "Dual-feed from separate substations: 20,000+ h."
            ),
        )
        grid_mttr_hours = st.sidebar.number_input(
            "Grid MTTR (hours)",
            min_value=0.1, max_value=720.0,
            value=2.0, step=0.5,
            help=(
                "Mean restoration time after a grid outage. Default 2 h ≈ urban "
                "average. IEEE 1366 nationwide CAIDI ≈ 2.3 h excluding major events. "
                "Rural / heavily damaged: 6–24 h. Major-event days can exceed 72 h."
            ),
        )
        # Show derived A_grid so user sees what their inputs imply
        derived_a = grid_mtbf_hours / (grid_mtbf_hours + grid_mttr_hours)
        derived_dt_min = (1 - derived_a) * 525_600
        st.sidebar.caption(
            f"→ Implied grid availability: {derived_a*100:.4f}%  "
            f"({derived_dt_min:.1f} min/yr outage)"
        )

    st.sidebar.divider()
    st.sidebar.header("Generation")

    gen_arrangement = st.sidebar.radio(
        "Generation arrangement",
        ["Dedicated per Path", "Shared Pool"],
        index=0,
        help=(
            "**Dedicated per Path** — the fleet defined in the Generator Fleet tab "
            "is replicated independently for each path (true 2N). "
            "**Shared Pool** — one common fleet feeds all distribution paths. "
            "In grid-connected mode, the grid is treated as the shared "
            "upstream source regardless of this setting."
        ),
    )

    n_total = get_fleet_total()
    gen_required = st.sidebar.number_input(
        f"Generators required  (of {n_total} installed{'  per path' if gen_arrangement == 'Dedicated per Path' else '  total'})",
        min_value=1, max_value=max(1, n_total), value=min(1, n_total), step=1,
    )

    st.sidebar.header("Distribution Topology")

    num_paths = st.sidebar.selectbox(
        "Number of power paths (N)",
        options=[1, 2, 3, 4, 5, 6],
        index=1,  # default 2
        format_func=lambda n: {
            1: "1 — Radial (no path redundancy)",
            2: "2 — 2N dual-cord",
            3: "3 — 3N/2 distributed",
            4: "4 — 4N/3 or 3+1 distributed",
            5: "5 — 5N/4 distributed",
            6: "6 — 6N/5 distributed",
        }[n],
        help=(
            "Number of redundant power paths from source to load. "
            "2N = dual-cord (1 of 2 needed). "
            "3N/2, 4N/3, 3+1 = 'distributed redundancy' where loads are "
            "cross-corded across many paths; the system tolerates any "
            "single path failure but at lower per-path capacity."
        ),
    )

    # Paths required (k-of-n at the path layer)
    if num_paths == 1:
        paths_required = 1
        st.sidebar.caption("Radial — system fails if the single path fails.")
    else:
        default_k = 1 if num_paths == 2 else num_paths - 1
        paths_required = st.sidebar.number_input(
            f"Paths required (k) — k-of-{num_paths}",
            min_value=1, max_value=int(num_paths),
            value=int(default_k), step=1,
            help=(
                "How many paths must be UP for the system to be UP. "
                "1-of-2 = 2N. 2-of-3 = 3N/2. 3-of-4 = 4N/3 or 3+1. "
                f"Default for N={num_paths} is k={default_k} "
                "(tolerates any single path failure)."
            ),
        )
        # Show a one-line interpretation so the engineer can sanity-check
        _tier_label = _interpret_kofn(int(num_paths), int(paths_required))
        st.sidebar.caption(f"-> {_tier_label}")

    st.sidebar.subheader("Path components")

    inc_para    = st.sidebar.checkbox("Paralleling switchgear / gen bus", value=True)
    inc_gen_brk = st.sidebar.checkbox("Generator output breaker (LV)", value=False)
    use_mv      = st.sidebar.checkbox("MV distribution (MV breaker + MV bus)", value=False)
    inc_ats     = st.sidebar.checkbox("ATS / path transfer switch", value=True)
    inc_xfmr    = st.sidebar.checkbox("Step-down transformer (MV -> LV)", value=False)
    inc_lv_bus  = st.sidebar.checkbox("LV bus section / busway", value=True)
    inc_lv_brk  = st.sidebar.checkbox("LV main breaker", value=True)

    st.sidebar.subheader("UPS")
    inc_ups = st.sidebar.checkbox("Include UPS system", value=True)
    ups_mods, ups_req, inc_ups_batt, inc_ups_sts = 4, 3, True, False
    if inc_ups:
        ups_mods     = st.sidebar.number_input("UPS modules per path", 1, 32, 4, 1)
        ups_req      = st.sidebar.number_input("UPS modules required (k)", 1, int(ups_mods), min(3, int(ups_mods)), 1)
        inc_ups_batt = st.sidebar.checkbox("Include UPS battery string", value=True)
        inc_ups_sts  = st.sidebar.checkbox("Include UPS static transfer switch", value=False)

    st.sidebar.subheader("Load-side distribution")
    inc_pdu = st.sidebar.checkbox("Include PDU / RPP tier", value=True)
    pdus_per_path, pdus_req = 2, 1
    if inc_pdu:
        pdus_per_path = st.sidebar.number_input("PDU/RPP units per path", 1, 8, 2, 1)
        pdus_req      = st.sidebar.number_input("PDU/RPP required (k)", 1, int(pdus_per_path), 1, 1)
    inc_rack_pdu = st.sidebar.checkbox("Include rack PDU", value=False)
    inc_it_psu   = st.sidebar.checkbox("Include IT PSU (per cord)", value=False)

    st.sidebar.header("Common-Cause Failure")
    enable_ccf = st.sidebar.checkbox("Apply CCF beta-factor", value=True)
    ccf_beta = 0.02
    if enable_ccf:
        ccf_beta = st.sidebar.slider("CCF beta", 0.001, 0.20, 0.02, 0.001, format="%.3f")

    st.sidebar.header("Mission Analysis")
    mission_hours = st.sidebar.number_input(
        "Mission duration (hours)",
        min_value=1.0, max_value=8760.0, value=96.0, step=1.0,
        help=(
            "How long the backup fleet must carry the load on a single demand. "
            "Reference values: 96 h = ~4-day worst-case outage; 168 h = 1 week; "
            "720 h = 1 month; 8760 h = 1 year. "
            "Note: for missions >~250-500 h the constant-MTBF mission model "
            "is conservative -- it assumes lambda = 1/MTBF applies for the "
            "full duration without any PM, oil changes, or refueling. Real "
            "generators receive maintenance during long runs that this model "
            "does not credit. For full-year planning horizons, also check the "
            "steady-state fleet availability (Results tab, which is "
            "independent of mission duration)."
        ),
    )
    # Friendly day/week display for any value above 24 h
    if mission_hours >= 24:
        if mission_hours < 24 * 14:
            _dur_caption = f"= {mission_hours / 24:.1f} days"
        elif mission_hours < 24 * 90:
            _dur_caption = f"= {mission_hours / (24 * 7):.1f} weeks"
        elif mission_hours < 24 * 365:
            _dur_caption = f"= {mission_hours / (24 * 30.44):.1f} months"
        else:
            _dur_caption = f"= {mission_hours / 8760:.2f} years"
        st.sidebar.caption(_dur_caption)

    config = TopologyConfig(
        gen_groups=get_fleet(),
        gen_required=int(gen_required),
        gen_arrangement=gen_arrangement,
        num_paths=int(num_paths),
        paths_required=int(paths_required),
        include_paralleling_switchgear=inc_para,
        include_gen_breaker=inc_gen_brk,
        include_mv_breaker=use_mv,
        include_mv_bus=use_mv,
        include_ats=inc_ats,
        include_transformer=inc_xfmr,
        include_lv_bus=inc_lv_bus,
        include_lv_breaker=inc_lv_brk,
        include_ups=inc_ups,
        ups_modules_per_path=int(ups_mods),
        ups_modules_required=int(ups_req),
        include_ups_battery=inc_ups_batt,
        include_ups_sts=inc_ups_sts,
        include_pdu=inc_pdu,
        pdus_per_path=int(pdus_per_path),
        pdus_required=int(pdus_req),
        include_rack_pdu=inc_rack_pdu,
        include_it_psu=inc_it_psu,
        enable_ccf=enable_ccf,
        ccf_beta=float(ccf_beta),
        mission_duration_hours=float(mission_hours),
        power_source_mode=power_source_mode,
        grid_mtbf_hours=float(grid_mtbf_hours),
        grid_mttr_hours=float(grid_mttr_hours),
    )

    comp_overrides = st.session_state.get("comp_overrides", {})
    return config, comp_overrides


# ---------------------------------------------------------------------------
# Tab: Generator Fleet
# ---------------------------------------------------------------------------

def render_fleet_tab():
    st.header("Generator Fleet Configuration")
    st.caption(
        "Define every generator group in your fleet. Each row represents a block of "
        "identical prime movers.  \n"
        "**FTS** = Fail-to-Start (per demand). "
        "**FTLR** = Fail-to-Load/Run — early carry-load failure per demand (NRC/INL 2022).  \n"
        "Mission model: P = (1 − FTS) × (1 − FTLR) × exp(−λ × t)"
    )

    if "fleet_df" not in st.session_state:
        st.session_state["fleet_df"] = _fleet_to_df(default_fleet())

    with st.expander("➕  Add generator group from type template", expanded=False):
        col1, col2, col3 = st.columns([3, 1, 1])
        new_type  = col1.selectbox("Select type", GEN_TYPE_LIST, key="new_gen_type")
        new_count = col2.number_input("Count", min_value=1, value=10, step=1, key="new_gen_count")
        add_clicked = col3.button("Add Group", use_container_width=True)
        if add_clicked:
            gd = GEN_DEFAULTS[new_type]
            new_row = pd.DataFrame([{
                "Group Name": f"{new_type} (x{new_count})",
                "Count": int(new_count),
                "MTBF (hours)": gd.mtbf_hours,
                "MTTR (hours)": gd.mttr_hours,
                "FTS Probability": gd.fts_probability,
                "FTLR Probability": gd.ftlr_probability,
                "Avail. (%)": round(gd.availability * 100, 6),
                "Source / Notes": gd.source[:80],
            }])
            st.session_state["fleet_df"] = pd.concat(
                [st.session_state["fleet_df"], new_row], ignore_index=True
            )
            st.rerun()

    st.divider()
    st.subheader("Fleet Composition")
    st.caption(
        "Edit cells directly. **FTLR** (fail-to-load/run) is the NRC/INL 2022 early carry-load "
        "failure probability per demand — default 0.00331 for diesel EDGs. "
        "Avail. (%) updates after edits."
    )

    edited_df = st.data_editor(
        st.session_state["fleet_df"],
        column_config={
            "Group Name": st.column_config.TextColumn("Group Name", width="medium"),
            "Count": st.column_config.NumberColumn(
                "Count", min_value=1, max_value=10_000, step=1, format="%d",
            ),
            "MTBF (hours)": st.column_config.NumberColumn(
                "MTBF (hours)", min_value=1.0, max_value=1_000_000.0, format="%.0f",
                help="Continuous-running MTBF per unit. PLACEHOLDER — replace with OEM/CMMS data.",
            ),
            "MTTR (hours)": st.column_config.NumberColumn(
                "MTTR (hours)", min_value=0.1, max_value=8760.0, format="%.1f",
            ),
            "FTS Probability": st.column_config.NumberColumn(
                "FTS Prob.", min_value=0.0, max_value=1.0, format="%.5f",
                help="Fail-to-start probability per demand (NREL/NRC basis).",
            ),
            "FTLR Probability": st.column_config.NumberColumn(
                "FTLR Prob.", min_value=0.0, max_value=1.0, format="%.5f",
                help="Fail-to-load/run (early carry-load failure) per demand. NRC/INL 2022 EDG mean: 0.00331.",
            ),
            "Avail. (%)": st.column_config.NumberColumn(
                "Avail. (%)", disabled=True, format="%.6f",
            ),
            "Source / Notes": st.column_config.TextColumn("Source / Notes", width="large"),
        },
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key="fleet_editor",
    )

    if edited_df is not None and len(edited_df) > 0:
        edited_df["Avail. (%)"] = edited_df.apply(
            lambda r: round(
                component_availability(
                    max(1.0, float(r["MTBF (hours)"])),
                    max(0.0, float(r["MTTR (hours)"])),
                ) * 100, 6
            ), axis=1
        )
        # Ensure FTLR column exists
        if "FTLR Probability" not in edited_df.columns:
            edited_df["FTLR Probability"] = 0.0
        st.session_state["fleet_df"] = edited_df

    st.divider()
    st.subheader("Fleet Summary")
    groups = _df_to_fleet(st.session_state["fleet_df"])

    if not groups:
        st.warning("No generator groups defined.")
        return

    n_total   = sum(g.count for g in groups)
    w_avg_a   = sum(g.count * g.availability for g in groups) / n_total
    n_groups  = len(groups)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Units Installed", f"{n_total:,}")
    c2.metric("Generator Groups", n_groups)
    c3.metric("Weighted Avg. Single-Unit Avail.", fmt_avail(w_avg_a))
    c4.metric("Weighted Avg. Unit Downtime", fmt_downtime(annual_downtime_minutes(w_avg_a)))

    group_names  = [g.name for g in groups]
    group_counts = [g.count for g in groups]
    group_avails = [g.availability * 100 for g in groups]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Unit count", x=group_names, y=group_counts,
        marker_color="#3498db", yaxis="y",
        text=group_counts, textposition="outside",
    ))
    fig.add_trace(go.Scatter(
        name="Single-unit availability (%)", x=group_names, y=group_avails,
        mode="lines+markers",
        marker=dict(size=10, color="#e74c3c"),
        line=dict(color="#e74c3c", width=2),
        yaxis="y2",
    ))
    fig.update_layout(
        title="Fleet Composition — Unit Count and Single-Unit Availability",
        yaxis=dict(title="Unit Count", side="left"),
        yaxis2=dict(title="Single-Unit Availability (%)", side="right", overlaying="y",
                    range=[max(0, min(group_avails) - 0.5), 100.2]),
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
        height=380,
    )
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Fleet Availability vs. Generators Required (k)")
    k_range = list(range(1, min(n_total + 1, 201)))
    fleet_groups_avail = [(g.count, g.availability) for g in groups]
    k_availabilities = [mixed_fleet_kofn_availability(fleet_groups_avail, k) * 100
                        for k in k_range]

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=k_range, y=k_availabilities,
        mode="lines", name="Fleet availability (%)",
        line=dict(color="#2980b9", width=2),
    ))
    fig2.update_layout(
        title="Fleet Availability P(working units >= k) vs. k",
        xaxis_title="k — Generators Required",
        yaxis_title="Fleet Availability (%)",
        height=360,
    )
    st.plotly_chart(fig2, use_container_width=True)

    sample_k = k_range[::max(1, len(k_range) // 30)]
    summary_rows = []
    for k in sample_k:
        a = mixed_fleet_kofn_availability(fleet_groups_avail, k) * 100
        summary_rows.append({
            "k Required": k,
            "Fleet Availability (%)": f"{a:.8f}",
            "Annual Downtime": fmt_downtime(annual_downtime_minutes(a / 100)),
            "Nines": f"{availability_to_nines(a / 100):.2f}",
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab: Results
# ---------------------------------------------------------------------------

def render_results_tab(result):
    r = result
    cfg = r.config

    st.header("System Availability Results")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("System Availability", fmt_avail(r.system_availability))
    c2.metric("Annual Downtime", fmt_downtime(r.annual_downtime_min))
    c3.metric("Reliability Nines", fmt_nines(r.nines))
    c4.metric("CCF Beta", f"b = {cfg.ccf_beta:.3f}" if r.ccf_applied else "Not applied")

    # Tier IV benchmark
    target_nines = 4.699  # 99.995%
    gap = r.nines - target_nines
    if gap >= 0:
        st.success(f"✅ Meets 99.995% Tier IV target (by {gap:.3f} nines margin).")
    else:
        st.warning(f"⚠️ Below 99.995% Tier IV target ({abs(gap):.3f} nines short — {annual_downtime_minutes(0.99995) - r.annual_downtime_min:.2f} min/yr gap).")

    st.divider()

    # ── Power source breakdown (different display in islanded vs grid mode) ─
    path = r.path_results[0]

    if r.power_source_mode == "grid_with_backup":
        st.subheader("Power Source — Grid-Connected with Backup")
        st.caption(
            "Grid is primary; backup fleet runs during outages. Source is "
            "available if grid is up OR (grid is down AND backup carries the "
            "load for the assumed outage duration)."
        )
        gc1, gc2, gc3, gc4 = st.columns(4)
        gc1.metric(
            "Grid Availability",
            fmt_avail(r.grid_availability),
            help=f"From MTBF={r.grid_mtbf_hours:,.0f} h, MTTR={r.grid_mttr_hours:.2f} h",
        )
        gc2.metric(
            "Grid Annual Outage",
            fmt_downtime(annual_downtime_minutes(r.grid_availability)),
        )
        gc3.metric(
            f"Backup Fleet ({r.fleet_total_units} units, {cfg.gen_required} req.)",
            fmt_avail(path.gen_fleet_availability),
            help="Continuous availability of backup gens if they were the only source.",
        )
        gc4.metric(
            "Combined Source",
            fmt_avail(r.source_availability),
            help="A_source = A_grid + U_grid * P_backup_mission. This is what "
                 "actually feeds the distribution chain.",
        )
        st.info(
            f"Combined source availability ({r.source_availability*100:.6f}%) "
            f"is higher than either grid alone ({r.grid_availability*100:.4f}%) "
            f"or backup alone ({path.gen_fleet_availability*100:.4f}%) because the "
            f"two are redundant — the backup runs only when the grid fails."
        )
        st.divider()
        st.subheader("Distribution-side Breakdown")

    cols = st.columns(3 if cfg.num_paths == 2 else 2)
    src_label = (
        f"Generation Fleet ({r.fleet_total_units:,} units, {cfg.gen_required} req.)"
        if r.power_source_mode == "islanded"
        else "Source (Grid + Backup combined)"
    )
    src_value = (path.gen_fleet_availability
                 if r.power_source_mode == "islanded"
                 else r.source_availability)
    cols[0].metric(
        src_label,
        fmt_avail(src_value),
        help=path.source_desc or path.gen_fleet_desc,
    )
    cols[1].metric("Distribution Path", fmt_avail(path.distribution_availability))
    if cfg.num_paths == 2:
        cols[2].metric("Total Path Availability", fmt_avail(path.total_availability))

    if r.ccf_applied and r.ccf_unavailability_contribution is not None and r.system_unavailability > 0:
        ccf_pct   = r.ccf_unavailability_contribution / r.system_unavailability * 100
        indep_pct = r.independent_unavailability_contribution / r.system_unavailability * 100
        st.info(
            f"**CCF drives {ccf_pct:.1f}% of system unavailability** "
            f"(independent coincident failure: {indep_pct:.1f}%).  \n"
            "This is expected for mature 2N designs — common-cause assumptions dominate "
            "once independent redundancy is in place. Tier IV performance is won by "
            "controlling shared dependencies, not just adding parallel paths."
        )

    st.divider()
    st.subheader("Component Availability — Per Distribution Path")
    rows = [{
        "Component": path.gen_fleet_desc,
        "Arrangement": f"{cfg.gen_required}-of-{r.fleet_total_units} (mixed fleet)",
        "Avail. (%)": f"{path.gen_fleet_availability * 100:.8f}",
        "Unavail. (ppm)": f"{(1 - path.gen_fleet_availability) * 1e6:.4f}",
        "Downtime (min/yr)": f"{annual_downtime_minutes(path.gen_fleet_availability):.4f}",
    }]
    for c in path.components:
        rows.append({
            "Component": c.label,
            "Arrangement": c.kofn_desc if c.is_kofn_group else "Series",
            "Avail. (%)": f"{c.availability * 100:.8f}",
            "Unavail. (ppm)": f"{(1 - c.availability) * 1e6:.4f}",
            "Downtime (min/yr)": f"{annual_downtime_minutes(c.availability):.4f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Unavailability Contribution (single path)")
    _render_waterfall(path, r.fleet_total_units, cfg.gen_required)


def _render_waterfall(path, n_total, k_req):
    labels, values = [], []
    gen_u = (1 - path.gen_fleet_availability) * 1e6
    labels.append(f"Gen Fleet\n({k_req}-of-{n_total})")
    values.append(gen_u)
    for c in path.components:
        u = (1 - c.availability) * 1e6
        if u > 0:
            labels.append(c.label)
            values.append(u)

    fig = go.Figure(go.Bar(
        x=labels, y=values,
        marker_color=["#e74c3c" if v == max(values) else "#3498db" for v in values],
        text=[f"{v:.3f}" for v in values],
        textposition="outside",
    ))
    fig.update_layout(
        title="Component Unavailability (ppm) — Single Distribution Path",
        yaxis_title="Unavailability (failures per 10^6 hours)",
        height=380, margin=dict(t=50, b=80),
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Component Parameters
# ---------------------------------------------------------------------------

def render_params_tab(config: TopologyConfig):
    st.header("Electrical Component Parameters")
    st.caption(
        "Edit MTBF and MTTR for every active electrical component. "
        "Generator parameters are set in the **Generator Fleet** tab.  \n"
        "⚠ values are placeholders — replace with OEM / site CMMS data before final use."
    )

    co_state   = st.session_state.get("comp_overrides", {})
    active_keys = _get_active_component_keys(config)

    table_rows = []
    for key in active_keys:
        defn = COMP_DEFAULTS[key]
        mtbf_cur = co_state.get(key, (defn.mtbf_hours, defn.mttr_hours))[0]
        mttr_cur = co_state.get(key, (defn.mtbf_hours, defn.mttr_hours))[1]
        avail = component_availability(mtbf_cur, mttr_cur)
        table_rows.append({
            "Key": key,
            "Component": defn.display_name,
            "Status": defn.quality_tag,
            "Confidence": defn.confidence,
            "MTBF (hours)": float(mtbf_cur),
            "MTTR (hours)": float(mttr_cur),
            "Avail. (%)": round(avail * 100, 8),
            "Downtime (min/yr)": round(annual_downtime_minutes(avail), 4),
            "Source": defn.source,
        })

    edited = st.data_editor(
        pd.DataFrame(table_rows),
        column_config={
            "Key": st.column_config.TextColumn("Key", disabled=True, width="small"),
            "Component": st.column_config.TextColumn("Component", disabled=True, width="medium"),
            "Status": st.column_config.TextColumn("Status", disabled=True, width="small"),
            "Confidence": st.column_config.TextColumn("Conf.", disabled=True, width="small"),
            "MTBF (hours)": st.column_config.NumberColumn(
                "MTBF (hours)", min_value=1.0, max_value=100_000_000.0, format="%.0f",
            ),
            "MTTR (hours)": st.column_config.NumberColumn(
                "MTTR (hours)", min_value=0.1, max_value=8760.0, format="%.1f",
            ),
            "Avail. (%)": st.column_config.NumberColumn("Avail. (%)", disabled=True, format="%.8f"),
            "Downtime (min/yr)": st.column_config.NumberColumn("Downtime (min/yr)", disabled=True, format="%.4f"),
            "Source": st.column_config.TextColumn("Source", disabled=True, width="large"),
        },
        use_container_width=True, hide_index=True, num_rows="fixed",
        key="comp_table_editor",
    )

    new_overrides = {}
    for _, row in edited.iterrows():
        new_overrides[row["Key"]] = (float(row["MTBF (hours)"]), float(row["MTTR (hours)"]))
    st.session_state["comp_overrides"] = new_overrides

    st.caption(
        "**Data source hierarchy:** "
        "Tier A: Site CMMS/FRACAS → "
        "Tier B: OEM model-specific data sheets → "
        "Tier C: IEEE 493 / NREL / NRC/INL / OREDA → "
        "Tier D: Engineering judgment (requires owner + expiry plan)"
    )


def _get_active_component_keys(config: TopologyConfig) -> list:
    keys = []
    if config.include_paralleling_switchgear: keys.append("paralleling_switchgear")
    if config.include_gen_breaker:            keys.append("gen_breaker")
    if config.include_mv_breaker:             keys.append("mv_breaker")
    if config.include_mv_bus:                 keys.append("mv_bus_section")
    if config.include_ats:                    keys.append("ats_transfer_switch")
    if config.include_transformer:            keys.append("transformer")
    if config.include_lv_bus:                 keys.append("lv_bus_section")
    if config.include_lv_breaker:             keys.append("lv_breaker")
    if config.include_ups:
        keys.append("ups_module")
        if config.include_ups_battery: keys.append("ups_battery_string")
        if config.include_ups_sts:     keys.append("ups_static_switch")
    if config.include_pdu:
        keys.append("pdu_rpp")
    if config.include_rack_pdu: keys.append("rack_pdu")
    if config.include_it_psu:   keys.append("it_psu")
    return keys


# ---------------------------------------------------------------------------
# Tab: Sensitivity Analysis
# ---------------------------------------------------------------------------

def render_sensitivity_tab(result):
    st.header("Sensitivity Analysis")
    r = result
    cfg = r.config
    co = st.session_state.get("comp_overrides", {})

    st.subheader("Tornado Chart — Downtime Recovered if Component Were Perfect")
    st.caption(
        "Each bar = minutes/year eliminated if that component (or group) had 100% availability. "
        "Largest bar = highest improvement leverage."
    )

    sensitivity = r.sensitivity
    if not sensitivity:
        st.info("No sensitivity data available.")
        return

    sorted_items = sorted(sensitivity.items(), key=lambda x: abs(x[1]), reverse=True)
    labels = [k for k, _ in sorted_items]
    deltas = [v for _, v in sorted_items]
    colors = ["#e74c3c" if d == max(deltas) else "#3498db" for d in deltas]

    fig = go.Figure(go.Bar(
        y=labels, x=deltas, orientation="h",
        marker_color=colors,
        text=[f"{d:.4f} min/yr" for d in deltas],
        textposition="auto",
    ))
    fig.update_layout(
        title="Minutes/Year Recovered if Component Were Perfect",
        xaxis_title="Annual Downtime Reduction (min/yr)",
        yaxis=dict(autorange="reversed"),
        height=max(350, 50 * len(labels)),
        margin=dict(l=380, r=60, t=50, b=50),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Parameter Sweep")

    sweep_options: dict = {
        "All Gen MTBF — scale factor (x current)": "gen_fleet_scale",
        "All Gen FTS probability": "gen_fts_all",
        "All Gen FTLR probability": "gen_ftlr_all",
        "CCF Beta factor": "ccf_beta",
    }
    for i, g in enumerate(cfg.gen_groups):
        sweep_options[f"Group MTBF — {g.name} ({g.count} units)"] = f"gen_group_mtbf_{i}"
    for key in _get_active_component_keys(cfg):
        sweep_options[f"{COMP_DEFAULTS[key].display_name} MTBF"] = key

    sweep_choice = st.selectbox("Select parameter to sweep", list(sweep_options.keys()))
    sweep_key = sweep_options[sweep_choice]

    col_a, col_b = st.columns(2)

    if sweep_key == "gen_fleet_scale":
        lo = col_a.number_input("Scale low (x)", value=0.1, step=0.1, format="%.2f", min_value=0.01)
        hi = col_b.number_input("Scale high (x)", value=5.0, step=0.5, format="%.2f")
        cur_x = 1.0
    elif sweep_key == "gen_fts_all":
        lo = col_a.number_input("FTS low", value=0.0001, step=0.0001, format="%.4f")
        hi = col_b.number_input("FTS high", value=0.05, step=0.001, format="%.4f")
        cur_x = float(np.mean([g.fts_probability for g in cfg.gen_groups]))
    elif sweep_key == "gen_ftlr_all":
        lo = col_a.number_input("FTLR low", value=0.0001, step=0.0001, format="%.4f")
        hi = col_b.number_input("FTLR high", value=0.05, step=0.001, format="%.4f")
        cur_x = float(np.mean([g.ftlr_probability for g in cfg.gen_groups]))
    elif sweep_key == "ccf_beta":
        lo = col_a.number_input("Beta low", value=0.001, step=0.001, format="%.3f")
        hi = col_b.number_input("Beta high", value=0.10, step=0.005, format="%.3f")
        cur_x = cfg.ccf_beta
    elif sweep_key.startswith("gen_group_mtbf_"):
        idx = int(sweep_key.split("_")[-1])
        cur_mtbf = cfg.gen_groups[idx].mtbf_hours if idx < len(cfg.gen_groups) else 4380.0
        lo = col_a.number_input("MTBF low (h)", value=max(100.0, cur_mtbf * 0.1), step=100.0)
        hi = col_b.number_input("MTBF high (h)", value=cur_mtbf * 10.0, step=100.0)
        cur_x = cur_mtbf
    else:
        cur_mtbf = co.get(sweep_key, (COMP_DEFAULTS[sweep_key].mtbf_hours, COMP_DEFAULTS[sweep_key].mttr_hours))[0]
        lo = col_a.number_input("MTBF low (h)", value=max(100.0, cur_mtbf * 0.1), step=100.0)
        hi = col_b.number_input("MTBF high (h)", value=cur_mtbf * 10.0, step=100.0)
        cur_x = cur_mtbf

    sweep_vals   = np.linspace(lo, hi, 60).tolist()
    sweep_avails = sweep_parameter(cfg, co, sweep_key, sweep_vals)
    sweep_downtime = [annual_downtime_minutes(a) for a in sweep_avails]
    cur_downtime   = annual_downtime_minutes(sweep_parameter(cfg, co, sweep_key, [cur_x])[0])

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=sweep_vals, y=sweep_downtime,
        mode="lines", name="Annual downtime",
        line=dict(color="#2980b9", width=2),
    ))
    fig2.add_trace(go.Scatter(
        x=[cur_x], y=[cur_downtime],
        mode="markers", name="Current value",
        marker=dict(color="#e74c3c", size=12, symbol="diamond"),
    ))
    fig2.update_layout(
        title=f"System Annual Downtime vs. {sweep_choice}",
        xaxis_title=sweep_choice,
        yaxis_title="Annual Downtime (min/yr)",
        height=400,
    )
    st.plotly_chart(fig2, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Generator Mission Analysis
# ---------------------------------------------------------------------------

def render_mission_tab(result):
    st.header("Generator Mission Analysis")
    st.caption(
        "Answers: *What is the probability the generator fleet starts, loads, AND runs "
        "for a given duration?*  \n"
        "Model (NRC/INL 2022): **P = (1 − FTS) × (1 − FTLR) × exp(−λ × t)**  \n"
        "Where: FTS = fail-to-start, FTLR = fail-to-load/run, λ = run-failure rate.  \n"
        "Ref: NREL 2020 (NREL/TP-5D00-76553), NRC/INL 2022 EPS EDG performance."
    )

    m = result.fleet_mission
    groups = m.groups
    k = m.k_required
    t = m.duration_hours

    c1, c2, c3 = st.columns(3)
    c1.metric(
        f"System Mission Success ({t:.0f} h)",
        f"{m.system_mission * 100:.4f}%",
        help=f"P(at least {k} of {sum(g.count for g in groups)} generators complete mission)",
    )
    c2.metric("System Start+Load Success (k-of-n)", f"{m.system_fts_success * 100:.4f}%",
              help="P(k-of-n generators successfully start AND accept load). No runtime failure.")
    c3.metric("System Run Success (k-of-n run)", f"{m.system_run_success * 100:.4f}%",
              help="P(k-of-n generators complete run). Assumes perfect start and load.")

    st.divider()

    st.subheader("Per-Group Mission Probability")
    group_rows = []
    for i, g in enumerate(groups):
        p_m     = m.group_mission_probs[i]
        p_start = m.group_start_probs[i]
        p_run   = m.group_run_probs[i]
        group_rows.append({
            "Group": g.name,
            "Units": g.count,
            "FTS (%)": f"{g.fts_probability * 100:.4f}",
            "FTLR (%)": f"{g.ftlr_probability * 100:.4f}",
            "Start+Load success (%)": f"{p_start * 100:.4f}",
            f"Run success ({t:.0f} h) (%)": f"{p_run * 100:.4f}",
            f"Mission success ({t:.0f} h) (%)": f"{p_m * 100:.4f}",
        })
    st.dataframe(pd.DataFrame(group_rows), use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("System Mission Success vs. Duration")
    t_max = st.slider("Maximum duration to plot (hours)", 24, 720, 168, 24)
    t_vals = np.linspace(0.1, t_max, 300).tolist()

    from reliability import mixed_fleet_mission_prob as _mfmp

    system_by_t = []
    for t_v in t_vals:
        mission_grps = [(g.count, g.fts_probability, g.ftlr_probability, g.lambda_run) for g in groups]
        system_by_t.append(_mfmp(mission_grps, k, t_v) * 100)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t_vals, y=system_by_t,
        mode="lines", name=f"System ({k}-of-{sum(g.count for g in groups)})",
        line=dict(color="#2980b9", width=2),
    ))
    for g in groups:
        single = [(1 - g.fts_probability) * (1 - g.ftlr_probability) *
                  mission_reliability(g.lambda_run, t_v) * 100 for t_v in t_vals]
        fig.add_trace(go.Scatter(
            x=t_vals, y=single,
            mode="lines", name=f"Single unit — {g.name}",
            line=dict(dash="dot", width=1),
        ))
    fig.add_vline(x=t, line_dash="dot", line_color="gray",
                  annotation_text=f"{t:.0f} h target")
    fig.update_layout(
        title="Mission Success Probability vs. Duration",
        xaxis_title="Mission Duration (hours)",
        yaxis_title="Mission Success (%)",
        height=440,
        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Mission Success vs. Generators Required (k)")
    n_total = sum(g.count for g in groups)
    k_range = list(range(1, min(n_total + 1, 201)))
    mission_grps_params = [(g.count, g.fts_probability, g.ftlr_probability, g.lambda_run) for g in groups]
    k_mission = [_mfmp(mission_grps_params, kk, t) * 100 for kk in k_range]

    fig3 = go.Figure(go.Scatter(
        x=k_range, y=k_mission,
        mode="lines", line=dict(color="#8e44ad", width=2),
    ))
    fig3.add_vline(x=k, line_dash="dot", line_color="gray", annotation_text=f"k = {k}")
    fig3.update_layout(
        title=f"Mission Success P(fleet completes {t:.0f} h mission) vs. k",
        xaxis_title="k — Generators Required",
        yaxis_title="Mission Success (%)",
        height=360,
    )
    st.plotly_chart(fig3, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Topology Comparison
# ---------------------------------------------------------------------------

def render_compare_tab(result):
    st.header("Topology Comparison")
    st.caption(
        "Save snapshots of the current configuration and compare multiple topologies "
        "side-by-side. Useful for evaluating design options (2N vs. N+1, with/without MV, etc.).  \n"
        "Or load a preset topology template to see standard configurations."
    )

    # ── Load a preset ─────────────────────────────────────────────────────────
    with st.expander("📋  Load a preset topology template into the sidebar", expanded=False):
        preset_name = st.selectbox(
            "Select preset",
            list(TOPOLOGY_PRESETS.keys()),
            key="preset_select",
        )
        preset = TOPOLOGY_PRESETS[preset_name]
        st.markdown(f"**Description:** {preset['desc']}")
        st.info(
            "Loading a preset will rewrite the generator fleet in session state "
            "and display its results here for comparison. "
            "The sidebar controls will update on the next rerun."
        )
        if st.button("Calculate preset and save to comparison list"):
            # Build fleet from preset
            fleet = []
            for g in preset["fleet"]:
                fleet.append(GeneratorGroup(
                    name=g["name"], count=g["count"],
                    mtbf_hours=g["mtbf"], mttr_hours=g["mttr"],
                    fts_probability=g["fts"], ftlr_probability=g["ftlr"],
                    source="Preset template — replace with OEM data",
                ))
            cfg_kwargs = dict(preset["config_kwargs"])
            cfg_kwargs["gen_groups"] = fleet
            cfg_kwargs["gen_required"] = preset["gen_required"]
            preset_cfg = TopologyConfig(**cfg_kwargs)
            try:
                preset_result = calculate_system(preset_cfg, {})
                _save_scenario(preset_result, name=preset_name, notes=preset["desc"])
                st.success(f"Preset '{preset_name}' calculated and saved to comparison list.")
                st.rerun()
            except Exception as exc:
                st.error(f"Preset calculation failed: {exc}")

    # ── Save current config ───────────────────────────────────────────────────
    with st.expander("💾  Save current configuration as a scenario", expanded=True):
        col_n, col_s = st.columns([2, 3])
        scenario_name  = col_n.text_input(
            "Scenario name",
            value=f"Scenario {len(st.session_state.get('scenarios', [])) + 1}",
            key="scenario_name_input",
        )
        scenario_notes = col_s.text_input(
            "Short description (optional)",
            value="",
            key="scenario_notes_input",
        )
        if st.button("💾 Save Scenario", use_container_width=False):
            _save_scenario(result, name=scenario_name, notes=scenario_notes)
            st.success(f"Saved '{scenario_name}' to comparison list.")
            st.rerun()

    # ── Comparison list ───────────────────────────────────────────────────────
    scenarios = st.session_state.get("scenarios", [])

    if not scenarios:
        st.info(
            "No scenarios saved yet. "
            "Configure the topology in the sidebar, then click 'Save Scenario' above — "
            "or load a preset template."
        )
        # Show current result for reference
        st.subheader("Current Configuration (live)")
        _show_scenario_metrics(result, "Current")
        return

    st.divider()
    st.subheader(f"Saved Scenarios ({len(scenarios)})")

    # Delete buttons
    col_del = st.columns(min(len(scenarios), 4))
    for i, sc in enumerate(scenarios):
        if col_del[i % 4].button(f"🗑 Delete '{sc['name']}'", key=f"del_sc_{i}"):
            st.session_state["scenarios"].pop(i)
            st.rerun()

    if st.button("🗑 Clear all saved scenarios"):
        st.session_state["scenarios"] = []
        st.rerun()

    # ── Comparison table ──────────────────────────────────────────────────────
    st.subheader("Comparison Table")

    # Always include live current config
    all_scenarios = [{"name": "▶ Current (live)", **_result_to_snapshot(result, "")}] + scenarios

    comp_rows = []
    for sc in all_scenarios:
        comp_rows.append({
            "Scenario": sc["name"],
            "Notes": sc.get("notes", ""),
            "Availability (%)": f"{sc['availability'] * 100:.6f}",
            "Downtime (min/yr)": f"{sc['downtime_min']:.4f}",
            "Nines": f"{sc['nines']:.3f}",
            "Gen Fleet Avail. (%)": f"{sc['gen_fleet_a'] * 100:.6f}",
            "Dist. Path Avail. (%)": f"{sc['dist_a'] * 100:.6f}",
            "Paths": sc["n_paths"],
            "CCF beta": f"{sc['ccf_beta']:.3f}" if sc["ccf_applied"] else "off",
            "Fleet": f"{sc['fleet_total']} units, {sc['k_req']} req.",
            "Arrangement": sc["gen_arrangement"],
            "Mission (96h)": f"{sc['mission_prob'] * 100:.3f}%",
        })

    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

    # ── Comparison bar chart ──────────────────────────────────────────────────
    st.subheader("Availability Comparison")
    names   = [sc["name"] for sc in all_scenarios]
    avails  = [sc["availability"] * 100 for sc in all_scenarios]
    nines_v = [sc["nines"] for sc in all_scenarios]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Availability (%)",
        x=names, y=avails,
        marker_color=["#27ae60" if a >= 99.995 else "#e67e22" if a >= 99.99 else "#e74c3c"
                      for a in avails],
        text=[f"{a:.6f}%" for a in avails],
        textposition="outside",
        yaxis="y",
    ))
    fig.add_trace(go.Scatter(
        name="Nines",
        x=names, y=nines_v,
        mode="lines+markers",
        marker=dict(size=10, color="#2980b9"),
        line=dict(color="#2980b9", width=2, dash="dot"),
        yaxis="y2",
    ))
    fig.add_hline(
        y=99.995, line_dash="dot", line_color="green",
        annotation_text="99.995% Tier IV target",
        yref="y",
    )
    fig.update_layout(
        title="Scenario Availability and Nines Comparison",
        yaxis=dict(title="Availability (%)", side="left"),
        yaxis2=dict(title="Nines", side="right", overlaying="y"),
        height=420,
        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
        margin=dict(b=120),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Annual downtime comparison ────────────────────────────────────────────
    st.subheader("Annual Downtime Comparison")
    downtimes = [sc["downtime_min"] for sc in all_scenarios]
    fig2 = go.Figure(go.Bar(
        x=names, y=downtimes,
        marker_color=["#27ae60" if d <= 26.28 else "#e67e22" if d <= 52.56 else "#e74c3c"
                      for d in downtimes],
        text=[fmt_downtime(d) for d in downtimes],
        textposition="outside",
    ))
    fig2.add_hline(y=26.28, line_dash="dot", line_color="green",
                   annotation_text="26.28 min/yr Tier IV budget")
    fig2.update_layout(
        title="Annual Downtime Budget Comparison",
        yaxis_title="Annual Downtime (min/yr)",
        height=380, margin=dict(b=120),
    )
    st.plotly_chart(fig2, use_container_width=True)

    # ── Export comparison ─────────────────────────────────────────────────────
    comp_csv = pd.DataFrame(comp_rows).to_csv(index=False).encode("utf-8")
    st.download_button(
        "📥 Download Comparison Table (CSV)",
        data=comp_csv,
        file_name=f"topology_comparison_{datetime.date.today()}.csv",
        mime="text/csv",
    )


def _result_to_snapshot(result, notes: str) -> dict:
    return {
        "notes": notes,
        "availability": result.system_availability,
        "downtime_min": result.annual_downtime_min,
        "nines": result.nines,
        "gen_fleet_a": result.path_results[0].gen_fleet_availability,
        "dist_a": result.path_results[0].distribution_availability,
        "ccf_applied": result.ccf_applied,
        "ccf_beta": result.config.ccf_beta,
        "n_paths": result.config.num_paths,
        "fleet_total": result.fleet_total_units,
        "k_req": result.config.gen_required,
        "gen_arrangement": result.config.gen_arrangement,
        "mission_prob": result.fleet_mission.system_mission,
        "mission_hours": result.fleet_mission.duration_hours,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _save_scenario(result, name: str, notes: str):
    if "scenarios" not in st.session_state:
        st.session_state["scenarios"] = []
    snap = _result_to_snapshot(result, notes)
    snap["name"] = name
    st.session_state["scenarios"].append(snap)


def _show_scenario_metrics(result, label: str):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"{label} — Availability", fmt_avail(result.system_availability))
    c2.metric("Annual Downtime", fmt_downtime(result.annual_downtime_min))
    c3.metric("Nines", fmt_nines(result.nines))
    c4.metric("Mission (96h)", f"{result.fleet_mission.system_mission * 100:.3f}%")


# ---------------------------------------------------------------------------
# Tab: Audit & QA
# ---------------------------------------------------------------------------

def render_audit_tab(result, config: TopologyConfig, comp_overrides: dict):
    st.header("Audit & Quality Review")
    st.caption(
        "This tab provides the material needed to peer-review, validate, and defend "
        "the calculation methodology. It is organized per the revised RAM study "
        "recommendations: data quality register, calculation trace, and limitations register."
    )

    # ── Quality scorecard ─────────────────────────────────────────────────────
    st.subheader("Data Quality Scorecard")

    active_keys = _get_active_component_keys(config)
    co_state = comp_overrides

    # Count by status
    all_comp_items = []
    for key in active_keys:
        defn = COMP_DEFAULTS[key]
        mtbf = co_state.get(key, (defn.mtbf_hours, defn.mttr_hours))[0]
        mttr = co_state.get(key, (defn.mtbf_hours, defn.mttr_hours))[1]
        is_user_override = key in co_state
        all_comp_items.append({
            "type": "Electrical Component",
            "name": defn.display_name,
            "placeholder": defn.is_placeholder and not is_user_override,
            "confidence": defn.confidence,
            "source_type": defn.source_type,
        })

    for g in config.gen_groups:
        gd = GEN_DEFAULTS.get(g.name.split(" (")[0], None)
        all_comp_items.append({
            "type": "Generator Group",
            "name": g.name,
            "placeholder": True,  # all gen MTBFs are placeholders until site data loaded
            "confidence": "Medium" if g.source and "NREL" in g.source else "Low",
            "source_type": "Public Study" if "NREL" in g.source or "NRC" in g.source else "Assumption",
        })

    # CCF
    if config.enable_ccf:
        all_comp_items.append({
            "type": "Model Parameter",
            "name": f"CCF Beta factor (b={config.ccf_beta:.3f})",
            "placeholder": True,
            "confidence": "Low",
            "source_type": "Assumption",
        })

    n_total_items  = len(all_comp_items)
    n_specified    = sum(1 for x in all_comp_items if not x["placeholder"])
    n_placeholder  = n_total_items - n_specified
    n_high         = sum(1 for x in all_comp_items if x["confidence"] == "High")
    n_medium       = sum(1 for x in all_comp_items if x["confidence"] == "Medium")
    n_low          = sum(1 for x in all_comp_items if x["confidence"] == "Low")

    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
    sc1.metric("Total Assumptions", n_total_items)
    sc2.metric("✓ Specified", n_specified, delta=None)
    sc3.metric("⚠ Placeholder", n_placeholder, delta=None)
    sc4.metric("High Confidence", n_high)
    sc5.metric("Low Confidence", n_low)

    pct_specified = n_specified / n_total_items * 100 if n_total_items else 0
    if pct_specified < 40:
        st.error(f"⛔ Only {pct_specified:.0f}% of model inputs are from specified sources. "
                 "Replace placeholders before using results for final design decisions.")
    elif pct_specified < 70:
        st.warning(f"⚠️ {pct_specified:.0f}% of inputs are from specified sources. "
                   "Replace remaining placeholders with OEM/site data.")
    else:
        st.success(f"✅ {pct_specified:.0f}% of inputs are from specified sources.")

    # Pie chart
    fig_pie = go.Figure(go.Pie(
        labels=["✓ Specified", "⚠ Placeholder"],
        values=[n_specified, n_placeholder],
        marker_colors=["#27ae60", "#e74c3c"],
        hole=0.4,
    ))
    fig_pie.update_layout(title="Input Data Quality", height=280, margin=dict(t=40, b=10))
    col_pie, col_conf = st.columns(2)
    col_pie.plotly_chart(fig_pie, use_container_width=True)

    fig_conf = go.Figure(go.Pie(
        labels=["High", "Medium", "Low"],
        values=[n_high, n_medium, n_low],
        marker_colors=["#27ae60", "#f39c12", "#e74c3c"],
        hole=0.4,
    ))
    fig_conf.update_layout(title="Confidence Levels", height=280, margin=dict(t=40, b=10))
    col_conf.plotly_chart(fig_conf, use_container_width=True)

    # ── Assumptions register ──────────────────────────────────────────────────
    st.divider()
    st.subheader("Assumptions Register")
    st.caption(
        "Full register of model inputs with source classification. "
        "Every Assumption-tier item requires an owner and expiry/replacement plan. "
        "Format per revised RAM study recommendations."
    )

    register_rows = []
    assumption_id = 1

    # Electrical components
    for key in active_keys:
        defn = COMP_DEFAULTS[key]
        mtbf = co_state.get(key, (defn.mtbf_hours, defn.mttr_hours))[0]
        mttr = co_state.get(key, (defn.mtbf_hours, defn.mttr_hours))[1]
        avail = component_availability(mtbf, mttr)
        is_user_override = key in co_state
        register_rows.append({
            "ID": f"EC-{assumption_id:03d}",
            "Type": "Elec. Component",
            "Component": defn.display_name,
            "Value": f"MTBF={mtbf:.0f} h, MTTR={mttr:.1f} h",
            "Avail.": f"{avail*100:.6f}%",
            "Source Type": "User Override" if is_user_override else defn.source_type,
            "Confidence": defn.confidence,
            "Status": "✓ Specified" if (not defn.is_placeholder or is_user_override) else "⚠ Placeholder",
            "Action Required": "None" if not defn.is_placeholder or is_user_override
                               else "Replace with OEM / site CMMS data",
            "Source Ref.": defn.source[:120],
        })
        assumption_id += 1

    # Generator groups
    for g in config.gen_groups:
        register_rows.append({
            "ID": f"GEN-{assumption_id:03d}",
            "Type": "Generator Group",
            "Component": g.name,
            "Value": (f"MTBF={g.mtbf_hours:.0f} h, MTTR={g.mttr_hours:.1f} h, "
                      f"FTS={g.fts_probability:.5f}, FTLR={g.ftlr_probability:.5f}"),
            "Avail.": f"{g.availability*100:.6f}%",
            "Source Type": "Public Study" if "NREL" in g.source or "NRC" in g.source else "Assumption",
            "Confidence": "Medium" if "NREL" in g.source else "Low",
            "Status": "⚠ Placeholder (MTBF)",
            "Action Required": "Replace continuous MTBF with OEM service data; "
                                "validate FTS/FTLR against site start log",
            "Source Ref.": g.source[:120],
        })
        assumption_id += 1

    # CCF
    if config.enable_ccf:
        register_rows.append({
            "ID": f"CCF-{assumption_id:03d}",
            "Type": "Model Parameter",
            "Component": "CCF Beta Factor",
            "Value": f"beta = {config.ccf_beta:.4f}",
            "Avail.": "N/A",
            "Source Type": "Assumption",
            "Confidence": "Low",
            "Status": "⚠ Placeholder",
            "Action Required": "Justify beta from site dependency analysis "
                                "(shared controls, procedures, fuel, maintenance)",
            "Source Ref.": "IEC 61508 / IEC TR 62380 beta-factor model. "
                            "Default 0.02 is engineering judgment.",
        })
        assumption_id += 1

    reg_df = pd.DataFrame(register_rows)
    st.dataframe(reg_df, use_container_width=True, hide_index=True)

    # ── UPS architecture cross-check ──────────────────────────────────────────
    if config.include_ups:
        st.divider()
        st.subheader("UPS Architecture Cross-Check (ABB DPA Reference)")
        st.caption(
            "The k-of-n per-module model can be validated against published ABB system-level "
            "MTBF values for standard configurations. The table below shows the reference values."
        )
        arch_rows = []
        for key, ref in UPS_SYSTEM_REFS.items():
            arch_rows.append({
                "Configuration": ref["label"],
                "System MTBF (h)": f"{ref['mtbf_hours']:,}",
                "lambda (/10^6 h)": f"{ref['lambda_per_1e6h']:.3f}",
                "Downtime (min/yr)": fmt_downtime(
                    annual_downtime_minutes(ref["mtbf_hours"] / (ref["mtbf_hours"] + ref["mttr_hours"]))
                ),
                "Source": ref["source"][:100],
                "Confidence": ref["confidence"],
                "Notes": ref["notes"][:120],
            })
        st.dataframe(pd.DataFrame(arch_rows), use_container_width=True, hide_index=True)

        # Compute model result for 4-mod N+1
        mod_a = component_availability(
            co_state.get("ups_module", (COMP_DEFAULTS["ups_module"].mtbf_hours,
                                        COMP_DEFAULTS["ups_module"].mttr_hours))[0],
            co_state.get("ups_module", (COMP_DEFAULTS["ups_module"].mtbf_hours,
                                        COMP_DEFAULTS["ups_module"].mttr_hours))[1],
        )
        model_ups_a = kofn_availability(config.ups_modules_per_path, config.ups_modules_required, mod_a)
        model_ups_dt = annual_downtime_minutes(model_ups_a)
        ref_sep_dt   = annual_downtime_minutes(
            UPS_SYSTEM_REFS["ABB_DPA_4mod_N1_sep_batt"]["mtbf_hours"] /
            (UPS_SYSTEM_REFS["ABB_DPA_4mod_N1_sep_batt"]["mtbf_hours"] +
             UPS_SYSTEM_REFS["ABB_DPA_4mod_N1_sep_batt"]["mttr_hours"])
        )
        if config.ups_modules_per_path == 4 and config.ups_modules_required == 3:
            st.info(
                f"**Model result** for current {config.ups_modules_required}-of-"
                f"{config.ups_modules_per_path} UPS: "
                f"downtime = {fmt_downtime(model_ups_dt)}.  \n"
                f"**ABB DPA reference** (4-module N+1, sep. batt.): "
                f"downtime = {fmt_downtime(ref_sep_dt)}.  \n"
                "These should be in the same order of magnitude for a well-calibrated model."
            )

    # ── Calculation trace ─────────────────────────────────────────────────────
    st.divider()
    st.subheader("Calculation Trace")
    st.caption(
        "Step-by-step calculation log for peer review. "
        "Each row shows the formula applied, inputs, and result at that step."
    )

    trace_rows = []
    for step in result.calc_trace:
        inputs_str = "; ".join(f"{k}: {v}" for k, v in step.get("inputs", {}).items())
        trace_rows.append({
            "Step": step["step"],
            "Method": step["method"],
            "Key Inputs": inputs_str[:200],
            "Result": f"{step['result']:.8f}",
            "Formula": step["formula"],
        })

    # Add final system result
    trace_rows.append({
        "Step": "FINAL — System Availability",
        "Method": "See above steps",
        "Key Inputs": f"A_sys = {result.system_availability:.8f}",
        "Result": f"{result.system_availability:.8f}",
        "Formula": f"Annual downtime = {result.annual_downtime_min:.4f} min/yr  |  {result.nines:.3f} nines",
    })

    st.dataframe(pd.DataFrame(trace_rows), use_container_width=True, hide_index=True)

    # ── Sensitivity ranking ───────────────────────────────────────────────────
    st.divider()
    st.subheader("Sensitivity Priority Ranking")
    st.caption(
        "Per the revised RAM study, the following items have highest impact on Tier IV "
        "system availability. Items marked with 🔴 are dominant contributors."
    )

    priority_rows = [
        {"Rank": "🔴 Highest", "Variable": "Common-cause factor (beta) between A/B paths",
         "Why": "In 1oo2 systems, independent-failure term is quadratic; CCF dominates total risk"},
        {"Rank": "🔴 Highest", "Variable": "Generator FTS / FTLR / run-failure rates",
         "Why": "Every utility outage drives these probabilities directly"},
        {"Rank": "🔴 Highest", "Variable": "Repair logistics and spares lead time",
         "Why": "At near-5-nines, downtime is often logistics-limited, not hardware-limited"},
        {"Rank": "🟠 High",    "Variable": "Battery architecture (separate vs. common)",
         "Why": "Separate vs common battery changes UPS system MTBF materially (ABB DPA)"},
        {"Rank": "🟠 High",    "Variable": "Human error during maintenance/testing",
         "Why": "Tier IV performance is often consumed by intervention states"},
        {"Rank": "🟡 Medium",  "Variable": "HDD/SSD infant mortality and firmware quality",
         "Why": "Storage risk is family- and firmware-dependent, not captured by nameplate MTTF"},
        {"Rank": "🟡 Medium",  "Variable": "Utility interruption frequency",
         "Why": "Important, but in well-designed sites drives standby demand, not direct downtime"},
    ]
    st.dataframe(pd.DataFrame(priority_rows), use_container_width=True, hide_index=True)

    # ── Open items ────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Open Items and Known Limitations")
    limitations = [
        "Maintenance-induced unavailability is NOT modeled (Tier IV performance is often consumed here).",
        "Control/BMS/EPMS/fire interface dependencies are excluded (this is optimistic).",
        "Battery wear-out and aging are not modeled (constant MTBF is a screening approximation only).",
        "Utility feeder reliability is excluded — add as a series element if utility-dependent.",
        "Cooling system reliability is excluded from this model.",
        "Repair queue effects (multiple simultaneous failures) are not modeled.",
        "Common-cause beta factor is an assumption — justify from site dependency analysis.",
        "All generator MTBFs are continuous-run placeholders — replace with OEM service data.",
        "ATS, PDU/RPP, and transformer values remain unspecified from strong public sources.",
        "IT PSU, rack PDU values are placeholders — obtain from server OEM data sheets.",
    ]

    ph_items = [x for x in register_rows if "Placeholder" in x.get("Status", "")]
    if ph_items:
        limitations.append(f"{len(ph_items)} inputs are still placeholders — see Assumptions Register above.")

    for item in limitations:
        st.markdown(f"- {item}")

    # ── Export ────────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Export Audit Package")

    # Build combined export
    export_buf = io.StringIO()
    export_buf.write(f"RAM Analysis Audit Export\n")
    export_buf.write(f"Generated: {datetime.datetime.now().isoformat()}\n")
    export_buf.write(f"System Availability: {result.system_availability:.8f}\n")
    export_buf.write(f"Annual Downtime: {result.annual_downtime_min:.4f} min/yr\n")
    export_buf.write(f"Nines: {result.nines:.3f}\n\n")
    export_buf.write("ASSUMPTIONS REGISTER\n")
    reg_df.to_csv(export_buf, index=False)
    export_buf.write("\nCALCULATION TRACE\n")
    pd.DataFrame(trace_rows).to_csv(export_buf, index=False)

    col_exp1, col_exp2 = st.columns(2)
    col_exp1.download_button(
        "📥 Download Full Audit Package (CSV)",
        data=export_buf.getvalue().encode("utf-8"),
        file_name=f"ram_audit_{datetime.date.today()}.csv",
        mime="text/csv",
    )
    col_exp2.download_button(
        "📥 Download Assumptions Register Only (CSV)",
        data=reg_df.to_csv(index=False).encode("utf-8"),
        file_name=f"assumptions_register_{datetime.date.today()}.csv",
        mime="text/csv",
    )

    # ── Full PDF report ──────────────────────────────────────────────────────
    st.markdown("**Full PDF report** — single document containing configuration, "
                "results, sensitivity (with tornado chart), full calculation "
                "trace, and limitations.  Use this to share or archive a "
                "complete audit-ready snapshot of one analysis run.")

    if st.button("📄 Generate Full PDF Report", key="gen_pdf_btn"):
        with st.spinner("Building PDF report…"):
            try:
                pdf_bytes = build_pdf_report(result, comp_overrides)
                st.session_state["last_pdf_bytes"] = pdf_bytes
                st.session_state["last_pdf_filename"] = (
                    f"ram_full_report_{datetime.date.today()}.pdf"
                )
                st.session_state["last_pdf_built_at"] = (
                    datetime.datetime.now().strftime("%H:%M:%S")
                )
                st.success(
                    f"PDF generated ({len(pdf_bytes) / 1024:.0f} KB). "
                    "Click the Download button below."
                )
            except Exception as exc:
                st.error(f"PDF generation failed: {exc}")

    if st.session_state.get("last_pdf_bytes"):
        built_at = st.session_state.get("last_pdf_built_at", "")
        st.download_button(
            f"📥 Download PDF Report  (built at {built_at})",
            data=st.session_state["last_pdf_bytes"],
            file_name=st.session_state.get(
                "last_pdf_filename", "ram_report.pdf"
            ),
            mime="application/pdf",
            key="pdf_dl_btn",
        )
        st.caption(
            "If you've changed any settings since the timestamp above, click "
            "**Generate Full PDF Report** again to refresh."
        )


# ---------------------------------------------------------------------------
# Tab: Methodology
# ---------------------------------------------------------------------------

def render_diagram_tab(result, comp_overrides: dict):
    """Render the System Diagram tab — toggle between RBD (graphviz labeled
    blocks) and SLD (matplotlib electrical symbols)."""
    st.header("System Topology Diagram")

    # ── View toggle: RBD vs SLD ──────────────────────────────────────────
    view = st.radio(
        "Diagram style",
        options=["RBD", "SLD"],
        index=0,
        format_func=lambda v: {
            "RBD": "📦 Reliability Block Diagram (labeled blocks, auto-layout)",
            "SLD": "⚡ Single-Line Diagram (simplified electrical symbols)",
        }[v],
        horizontal=True,
        key="diagram_view_toggle",
        help=(
            "**RBD** shows each component / k-of-n group as a labeled box, "
            "color-coded by data provenance. Easier to read at a glance and "
            "shows the reliability math structure.\n\n"
            "**SLD** shows the same topology using simplified IEEE-style "
            "electrical symbols (gen circle, transformer two-coil, breaker "
            "square, UPS, battery, etc.). Looks closer to an electrical "
            "engineering drawing."
        ),
    )
    st.divider()

    st.caption(
        "Auto-generated from your current configuration.  Border / outline color "
        "indicates data provenance.  Use the controls below to switch what "
        "reliability number is shown on each component."
    )

    # ── Controls ──────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([2, 2, 3])

    annotation_label = c1.radio(
        "Show on each component",
        options=["availability", "downtime", "unavailability"],
        index=0,
        format_func=lambda x: {
            "availability":   "Availability (%)",
            "downtime":       "Downtime (min/yr)",
            "unavailability": "Unavailability (ppm)",
        }[x],
        horizontal=False,
        key="diagram_annotation",
    )

    highlight = c2.checkbox(
        "Highlight top bottleneck (red)",
        value=True,
        help=(
            "Outlines the single component contributing the most to system "
            "downtime in red. Pulled from the sensitivity ranking."
        ),
        key="diagram_highlight",
    )

    with c3:
        st.markdown("**Legend**")
        st.markdown(
            "<small>"
            "🟢 Specified (published source)  &nbsp;|&nbsp; "
            "🟡 Placeholder (replace with OEM data)<br/>"
            "🔵 User override  &nbsp;|&nbsp; "
            "🔴 Top sensitivity contributor"
            "</small>",
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Render the chosen view ───────────────────────────────────────────
    if view == "RBD":
        dot_string = build_topology_diagram(
            result,
            comp_overrides=comp_overrides,
            annotation=annotation_label,
            highlight_bottleneck=highlight,
        )
        st.graphviz_chart(dot_string, use_container_width=True)

        # PNG download (best-effort)
        st.markdown("**Export the diagram**")
        try:
            import graphviz as _gv
            png_bytes = _gv.Source(dot_string).pipe(format="png")
            st.download_button(
                "📥 Download RBD as PNG",
                data=png_bytes,
                file_name=f"ram_rbd_{datetime.date.today()}.png",
                mime="image/png",
                key="rbd_png_dl",
            )
        except Exception as exc:
            st.info(
                f"PNG download not available in this environment "
                f"({type(exc).__name__}: {exc}).  The interactive diagram "
                "above still works — right-click → Save image to grab it."
            )

        with st.expander("Show graphviz DOT source", expanded=False):
            st.code(dot_string, language="dot")

    else:  # SLD view
        with st.spinner("Drawing single-line diagram…"):
            fig = build_sld(
                result,
                comp_overrides=comp_overrides,
                annotation=annotation_label,
                highlight_bottleneck=highlight,
            )
        st.pyplot(fig, use_container_width=True)

        # PNG download from the matplotlib figure
        st.markdown("**Export the diagram**")
        png_bytes = sld_to_png_bytes(fig)
        st.download_button(
            "📥 Download SLD as PNG",
            data=png_bytes,
            file_name=f"ram_sld_{datetime.date.today()}.png",
            mime="image/png",
            key="sld_png_dl",
        )

        # Important caveat about the SLD scope
        st.info(
            "**About this SLD:**  This is a *simplified* representation using "
            "common IEEE-style symbols — not a CAD-grade single-line drawing.  "
            "Use it to verify the topology that the RAM model is calculating "
            "against, not as a substitute for the project's actual electrical "
            "engineering drawings.  Backup gens are drawn to the left of each "
            "path's main vertical chain to keep the layout readable; in a real "
            "SLD they'd typically be on the alternate side of an ATS."
        )

        # Avoid memory accumulation: close the figure after rendering
        import matplotlib.pyplot as _plt
        _plt.close(fig)


def render_methodology_tab(result):
    st.header("Methodology & Assumptions")
    st.subheader("Scope")
    st.markdown("""
**In scope:** prime mover fleet (any mixture, any size) · paralleling switchgear ·
MV/LV switchgear, breakers, bus sections, transformers · UPS (modular k-of-n) ·
PDU/RPP · rack PDUs · IT PSUs · CCF beta-factor between redundant paths.

**Out of scope:** mechanical cooling · IT hardware · BMS/EPMS/control layer ·
fuel supply · structural/civil · maintenance-induced unavailability.
""")

    st.subheader("Core Formulas")
    st.markdown(r"""
**Single component:** $A = \dfrac{MTBF}{MTBF + MTTR}$

**Series system:** $A_s = \prod_i A_i$

**k-of-n identical:** $A_s = \displaystyle\sum_{i=k}^{n} \binom{n}{i} A^i (1-A)^{n-i}$

**Mixed-fleet k-of-n (convolution):**
Joint PMF = $\bigotimes_i \text{Binomial}(n_i, a_i)$, then $A_s = P(\text{working} \geq k)$

**Two-path CCF (beta-factor):** $U_{sys} = (1-\beta)\,U_A\,U_B + \beta\,\max(U_A,U_B)$

**Generator mission (revised — NRC/INL 2022):**
$$P_{mission} = (1-FTS) \cdot (1-FTLR) \cdot e^{-\lambda t}$$

Where:
- $FTS$ = fail-to-start probability per demand
- $FTLR$ = fail-to-load / early carry-load failure per demand (NRC/INL 2022 EDG mean: 0.331%)
- $\lambda$ = run-failure rate after first hour (NRC/INL mean: $1.18 \times 10^{-3}$ / run-h)
- $t$ = mission duration (hours)

Applied k-of-n across mixed fleet via PMF convolution.

**Availability nines:** $\text{nines} = -\log_{10}(1 - A)$

**Annual downtime:** $D_{min/yr} = (1-A) \times 525{,}960$
""")

    st.subheader("Data Quality Framework")
    st.markdown("""
| Tier | Source | Use |
|------|--------|-----|
| **A** | Site CMMS, service tickets, EPMS/DCIM history, start logs | Best source for site RAM inputs |
| **B** | OEM model-specific data sheets, service advisories, MTBF reports | Use when site history is short |
| **C** | IEEE 493, NREL, NRC/INL, ABB/Vertiv OEM, OREDA | Screening, QA, proxy values |
| **D** | Engineering judgment / assumption | Allowed only with owner + expiry plan |
""")

    st.subheader("All Default Electrical Component Values")
    rows = []
    for key, defn in COMP_DEFAULTS.items():
        rows.append({
            "Component": defn.display_name,
            "MTBF (h)": defn.mtbf_hours,
            "MTTR (h)": defn.mttr_hours,
            "Avail. (%)": round(defn.availability * 100, 8),
            "Status": defn.quality_tag,
            "Confidence": defn.confidence,
            "Source Type": defn.source_type,
            "Source": defn.source[:150],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.subheader("Generator Type Defaults")
    gen_rows = []
    for gtype, gd in GEN_DEFAULTS.items():
        gen_rows.append({
            "Type": gtype,
            "MTBF (h)": gd.mtbf_hours,
            "MTTR (h)": gd.mttr_hours,
            "FTS": gd.fts_probability,
            "FTLR": gd.ftlr_probability,
            "Avail. (%)": round(gd.availability * 100, 6),
            "Status": gd.quality_tag,
            "Confidence": gd.confidence,
            "Source": gd.source[:150],
        })
    st.dataframe(pd.DataFrame(gen_rows), use_container_width=True, hide_index=True)

    st.subheader("UPS Architecture Reference Values (ABB DPA White Paper)")
    arch_rows = []
    for key, ref in UPS_SYSTEM_REFS.items():
        arch_rows.append({
            "Config": ref["label"],
            "System MTBF (h)": f"{ref['mtbf_hours']:,}",
            "lambda (/10^6 h)": f"{ref['lambda_per_1e6h']:.3f}",
            "Confidence": ref["confidence"],
            "Source": ref["source"],
            "Notes": ref["notes"],
        })
    st.dataframe(pd.DataFrame(arch_rows), use_container_width=True, hide_index=True)

    st.subheader("Tier IV Benchmark")
    st.markdown(r"""
| Availability Level | Annual Downtime Budget | Notes |
|---|---|---|
| **99.999%** ("five nines") | 5.26 min/yr | Above Tier IV target |
| **99.995%** | 26.28 min/yr | **Tier IV screening target** |
| **99.99%** | 52.56 min/yr | Tier III+ |
| **99.9%** | 525.6 min/yr | Tier II reference |

At 99.995%, the independent-failure term for each path can be as low as:
$U_{path} \leq \sqrt{U_{sys}} = \sqrt{5 \times 10^{-5}} \approx 7.07 \times 10^{-3}$
meaning each path could be only 99.293% available under pure independence —
**proof that Tier IV RAM is won by controlling CCF, maintenance exposure, and standby performance,
not simply by installing two paths.**
""")

    st.warning("""
**Important limitations:**
1. All ⚠ Placeholder values must be replaced with OEM / CMMS / fleet data before use in final design assurance.
2. VRLA batteries are wear-out items — constant-hazard MTBF is a screening approximation only (use IEEE 1188).
3. Maintenance-induced unavailability, test states, and human error are not modeled.
4. Control/BMS/EPMS dependencies are excluded (optimistic).
5. This tool covers the electrical backbone only — cooling and IT hardware are excluded.
6. OREDA compressor data is NOT a valid proxy for data-center chiller reliability.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    st.title("⚡ Data Center Electrical RAM Analysis")
    st.caption(
        "Behind-the-meter or grid-connected · Electrical system only · "
        "Mixed generator fleets of any size · "
        "Revised per NRC/INL 2022 generator mission model"
    )

    config, comp_overrides = render_sidebar()

    try:
        result = calculate_system(config, comp_overrides)
    except Exception as exc:
        st.error(f"Calculation error: {exc}")
        st.exception(exc)
        return

    (tab_fleet, tab_results, tab_params,
     tab_sensitivity, tab_mission,
     tab_diagram, tab_compare, tab_audit,
     tab_method) = st.tabs([
        "🏭 Generator Fleet",
        "📊 Results",
        "🔧 Elec. Components",
        "📈 Sensitivity",
        "🔋 Mission Analysis",
        "📐 System Diagram",
        "🔄 Topology Compare",
        "🔍 Audit & QA",
        "📚 Methodology",
    ])

    with tab_fleet:
        render_fleet_tab()

    with tab_results:
        render_results_tab(result)

    with tab_params:
        render_params_tab(config)

    with tab_sensitivity:
        render_sensitivity_tab(result)

    with tab_mission:
        render_mission_tab(result)

    with tab_diagram:
        render_diagram_tab(result, comp_overrides)

    with tab_compare:
        render_compare_tab(result)

    with tab_audit:
        render_audit_tab(result, config, comp_overrides)

    with tab_method:
        render_methodology_tab(result)


if __name__ == "__main__":
    main()
