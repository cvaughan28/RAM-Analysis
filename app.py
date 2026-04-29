"""
Data Center Electrical RAM Analysis Tool
=========================================
Streamlit application for behind-the-meter, fully islanded data centers.

Scope: electrical system reliability only (generation + distribution).

Run:
    python -m streamlit run app.py
"""

import math
import copy
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

from defaults import COMP_DEFAULTS, GEN_DEFAULTS, GEN_TYPE_LIST
from models import (
    TopologyConfig, GeneratorGroup, default_fleet,
    calculate_system, sweep_parameter,
)
from reliability import (
    annual_downtime_minutes, availability_to_nines,
    component_availability, mixed_fleet_kofn_availability,
    mission_reliability, kofn_availability,
)


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


# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------

def _fleet_to_df(groups: list[GeneratorGroup]) -> pd.DataFrame:
    rows = []
    for g in groups:
        rows.append({
            "Group Name": g.name,
            "Count": g.count,
            "MTBF (hours)": g.mtbf_hours,
            "MTTR (hours)": g.mttr_hours,
            "FTS Probability": g.fts_probability,
            "Avail. (%)": round(g.availability * 100, 6),
            "Source / Notes": g.source,
        })
    return pd.DataFrame(rows)


def _df_to_fleet(df: pd.DataFrame) -> list[GeneratorGroup]:
    groups = []
    for _, row in df.iterrows():
        try:
            count = max(1, int(row["Count"]))
            mtbf = max(1.0, float(row["MTBF (hours)"]))
            mttr = max(0.0, float(row["MTTR (hours)"]))
            fts = max(0.0, min(1.0, float(row["FTS Probability"])))
            groups.append(GeneratorGroup(
                name=str(row["Group Name"]),
                count=count,
                mtbf_hours=mtbf,
                mttr_hours=mttr,
                fts_probability=fts,
                source=str(row.get("Source / Notes", "User-defined")),
            ))
        except Exception:
            pass
    return groups if groups else default_fleet()


def get_fleet() -> list[GeneratorGroup]:
    if "fleet_df" not in st.session_state:
        st.session_state["fleet_df"] = _fleet_to_df(default_fleet())
    return _df_to_fleet(st.session_state["fleet_df"])


def get_fleet_total() -> int:
    groups = get_fleet()
    return sum(g.count for g in groups)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar():
    st.sidebar.title("⚙️ System Configuration")

    # ── Generation ────────────────────────────────────────────────────────────
    st.sidebar.header("Generation")

    gen_arrangement = st.sidebar.radio(
        "Generation arrangement",
        ["Dedicated per Path", "Shared Pool"],
        index=0,
        help=(
            "**Dedicated per Path** — the fleet defined in the Generator Fleet tab "
            "is replicated independently for each path (true 2N). "
            "**Shared Pool** — one common fleet feeds all distribution paths; "
            "a total pool failure is a site-wide common event."
        ),
    )

    n_total = get_fleet_total()
    gen_required = st.sidebar.number_input(
        f"Generators required  (of {n_total} installed{'  per path' if gen_arrangement == 'Dedicated per Path' else '  total'})",
        min_value=1, max_value=max(1, n_total), value=min(1, n_total), step=1,
        help=(
            "Minimum generators that must be running to carry the load.  \n"
            "For Dedicated per Path: required per path.  \n"
            "For Shared Pool: required from the total pool."
        ),
    )

    # ── Distribution ──────────────────────────────────────────────────────────
    st.sidebar.header("Distribution Topology")

    num_paths = st.sidebar.radio(
        "Power paths",
        [1, 2],
        index=1,
        format_func=lambda x: "1 — Single (radial)" if x == 1 else "2 — Dual path (2N)",
    )

    st.sidebar.subheader("Path components")

    inc_para  = st.sidebar.checkbox("Paralleling switchgear / gen bus", value=True)
    inc_gen_brk = st.sidebar.checkbox("Generator output breaker (LV)", value=False)
    use_mv    = st.sidebar.checkbox("MV distribution (MV breaker + MV bus)", value=False)
    inc_ats   = st.sidebar.checkbox("ATS / path transfer switch", value=True)
    inc_xfmr  = st.sidebar.checkbox("Step-down transformer (MV → LV)", value=False)
    inc_lv_bus = st.sidebar.checkbox("LV bus section / busway", value=True)
    inc_lv_brk = st.sidebar.checkbox("LV main breaker", value=True)

    st.sidebar.subheader("UPS")
    inc_ups = st.sidebar.checkbox("Include UPS system", value=True)
    ups_mods, ups_req, inc_ups_batt, inc_ups_sts = 4, 3, True, False
    if inc_ups:
        ups_mods = st.sidebar.number_input("UPS modules per path", 1, 32, 4, 1)
        ups_req  = st.sidebar.number_input("UPS modules required (k)", 1, int(ups_mods), min(3, int(ups_mods)), 1)
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

    # ── CCF ──────────────────────────────────────────────────────────────────
    st.sidebar.header("Common-Cause Failure")
    enable_ccf = st.sidebar.checkbox("Apply CCF beta-factor", value=True)
    ccf_beta = 0.02
    if enable_ccf:
        ccf_beta = st.sidebar.slider("CCF beta (β)", 0.001, 0.20, 0.02, 0.001, format="%.3f")

    # ── Mission ───────────────────────────────────────────────────────────────
    st.sidebar.header("Mission Analysis")
    mission_hours = st.sidebar.number_input(
        "Mission duration (hours)", min_value=1.0, max_value=720.0, value=96.0, step=1.0,
    )

    # ── Build config ──────────────────────────────────────────────────────────
    config = TopologyConfig(
        gen_groups=get_fleet(),
        gen_required=int(gen_required),
        gen_arrangement=gen_arrangement,
        num_paths=num_paths,
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
    )

    comp_overrides: dict = st.session_state.get("comp_overrides", {})
    return config, comp_overrides


# ---------------------------------------------------------------------------
# Tab: Generator Fleet
# ---------------------------------------------------------------------------

def render_fleet_tab():
    st.header("Generator Fleet Configuration")
    st.caption(
        "Define every generator group in your fleet. Each row represents a block of "
        "identical prime movers.  \n"
        "For **Dedicated per Path** this table defines the fleet *per path* "
        "(e.g. 150 units here = 150 per path, 300 total installed).  \n"
        "For **Shared Pool** this table defines the *total* pool.  \n\n"
        "All ⚠ MTBF values are **placeholders** — replace with OEM service data or fleet CMMS history."
    )

    # Initialise session state
    if "fleet_df" not in st.session_state:
        st.session_state["fleet_df"] = _fleet_to_df(default_fleet())

    # ── Add a group from a type template ─────────────────────────────────────
    with st.expander("➕  Add generator group from type template", expanded=False):
        col1, col2, col3 = st.columns([3, 1, 1])
        new_type = col1.selectbox("Select type", GEN_TYPE_LIST, key="new_gen_type")
        new_count = col2.number_input("Count", min_value=1, value=10, step=1, key="new_gen_count")
        add_clicked = col3.button("Add Group", use_container_width=True)
        if add_clicked:
            gd = GEN_DEFAULTS[new_type]
            new_row = pd.DataFrame([{
                "Group Name": f"{new_type} (×{new_count})",
                "Count": int(new_count),
                "MTBF (hours)": gd.mtbf_hours,
                "MTTR (hours)": gd.mttr_hours,
                "FTS Probability": gd.fts_probability,
                "Avail. (%)": round(gd.availability * 100, 6),
                "Source / Notes": gd.source,
            }])
            st.session_state["fleet_df"] = pd.concat(
                [st.session_state["fleet_df"], new_row], ignore_index=True
            )
            st.rerun()

    st.divider()

    # ── Fleet editor table ───────────────────────────────────────────────────
    st.subheader("Fleet Composition")
    st.caption(
        "Edit cells directly. Delete rows using the trash icon on each row. "
        "Avail. (%) updates after you save your edits."
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
                "FTS Probability", min_value=0.0, max_value=1.0, format="%.4f",
                help="Fail-to-start probability per demand (used in mission analysis only).",
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

    # Recalculate Avail. % whenever MTBF/MTTR change
    if edited_df is not None and len(edited_df) > 0:
        edited_df["Avail. (%)"] = edited_df.apply(
            lambda r: round(
                component_availability(
                    max(1.0, float(r["MTBF (hours)"])),
                    max(0.0, float(r["MTTR (hours)"])),
                ) * 100, 6
            ), axis=1
        )
        st.session_state["fleet_df"] = edited_df

    st.divider()

    # ── Fleet summary ─────────────────────────────────────────────────────────
    st.subheader("Fleet Summary")
    groups = _df_to_fleet(st.session_state["fleet_df"])

    if not groups:
        st.warning("No generator groups defined.")
        return

    n_total = sum(g.count for g in groups)
    w_avg_a = sum(g.count * g.availability for g in groups) / n_total
    n_groups = len(groups)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Units Installed", f"{n_total:,}")
    c2.metric("Generator Groups", n_groups)
    c3.metric("Weighted Avg. Single-Unit Avail.", fmt_avail(w_avg_a))
    c4.metric("Weighted Avg. Unit Downtime", fmt_downtime(annual_downtime_minutes(w_avg_a)))

    # Per-group bar chart
    group_names  = [g.name for g in groups]
    group_counts = [g.count for g in groups]
    group_avails = [g.availability * 100 for g in groups]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Unit count",
        x=group_names, y=group_counts,
        marker_color="#3498db",
        yaxis="y",
        text=group_counts, textposition="outside",
    ))
    fig.add_trace(go.Scatter(
        name="Single-unit availability (%)",
        x=group_names, y=group_avails,
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

    # Fleet k-of-n availability sweep
    st.subheader("Fleet Availability vs. Generators Required (k)")
    st.caption(
        "Shows P(at least k generators are available simultaneously). "
        "Use this to choose the k value in the sidebar."
    )
    k_range = list(range(1, n_total + 1))
    fleet_groups_avail = [(g.count, g.availability) for g in groups]
    k_availabilities = [mixed_fleet_kofn_availability(fleet_groups_avail, k) * 100
                        for k in k_range]
    k_downtimes = [annual_downtime_minutes(a / 100) for a in k_availabilities]

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=k_range, y=k_availabilities,
        mode="lines", name="Fleet availability (%)",
        line=dict(color="#2980b9", width=2),
    ))
    fig2.update_layout(
        title="Fleet Availability P(working units ≥ k) vs. k",
        xaxis_title="k — Generators Required",
        yaxis_title="Fleet Availability (%)",
        height=380,
    )
    st.plotly_chart(fig2, use_container_width=True)

    # Table
    summary_rows = []
    for k in k_range[::max(1, n_total // 30)]:   # sample at most ~30 rows
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
    c4.metric("CCF Beta", f"β = {cfg.ccf_beta:.3f}" if r.ccf_applied else "Not applied")

    st.divider()

    # Generation fleet summary
    path = r.path_results[0]
    cols = st.columns(3 if cfg.num_paths == 2 else 2)
    cols[0].metric(
        f"Generation Fleet ({r.fleet_total_units:,} units, {cfg.gen_required} req.)",
        fmt_avail(path.gen_fleet_availability),
        help=path.gen_fleet_desc,
    )
    cols[1].metric("Distribution Path", fmt_avail(path.distribution_availability))
    if cfg.num_paths == 2:
        cols[2].metric("Total Path Availability", fmt_avail(path.total_availability))

    if r.ccf_applied and r.ccf_unavailability_contribution is not None and r.system_unavailability > 0:
        ccf_pct = r.ccf_unavailability_contribution / r.system_unavailability * 100
        indep_pct = r.independent_unavailability_contribution / r.system_unavailability * 100
        st.info(
            f"**CCF drives {ccf_pct:.1f}% of system unavailability** "
            f"(independent coincident failure: {indep_pct:.1f}%).  \n"
            "This is expected for mature 2N designs — common-cause assumptions dominate "
            "once independent redundancy is in place."
        )

    st.divider()

    # Component breakdown table
    st.subheader("Component Availability — Per Distribution Path")
    rows = [
        {
            "Component": path.gen_fleet_desc,
            "Arrangement": f"{cfg.gen_required}-of-{r.fleet_total_units} (mixed fleet)",
            "Avail. (%)": f"{path.gen_fleet_availability * 100:.8f}",
            "Unavail. (ppm)": f"{(1 - path.gen_fleet_availability) * 1e6:.4f}",
            "Downtime (min/yr)": f"{annual_downtime_minutes(path.gen_fleet_availability):.4f}",
        }
    ]
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
        yaxis_title="Unavailability (failures per 10⁶ hours)",
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
        "Generator parameters are set in the **Generator Fleet** tab."
    )

    co_state: dict = st.session_state.get("comp_overrides", {})
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
        "**Data source hierarchy:** Site CMMS/FRACAS → OEM service bulletins → "
        "IEEE 493 → Quanterion ROADS/EPRD → OREDA → NREL (generators)"
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

    # ── Tornado chart ─────────────────────────────────────────────────────────
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

    # ── Parameter sweep ───────────────────────────────────────────────────────
    st.subheader("Parameter Sweep")

    sweep_options: dict[str, str] = {
        "All Gen MTBF — scale factor (×current)": "gen_fleet_scale",
        "All Gen FTS probability": "gen_fts_all",
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
        lo = col_a.number_input("Scale low (×)", value=0.1, step=0.1, format="%.2f", min_value=0.01)
        hi = col_b.number_input("Scale high (×)", value=5.0, step=0.5, format="%.2f")
        cur_x = 1.0
    elif sweep_key == "gen_fts_all":
        lo = col_a.number_input("FTS low", value=0.0001, step=0.0001, format="%.4f")
        hi = col_b.number_input("FTS high", value=0.05, step=0.001, format="%.4f")
        cur_x = float(np.mean([g.fts_probability for g in cfg.gen_groups]))
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

    sweep_vals = np.linspace(lo, hi, 60).tolist()
    sweep_avails = sweep_parameter(cfg, co, sweep_key, sweep_vals)
    sweep_downtime = [annual_downtime_minutes(a) for a in sweep_avails]

    cur_downtime = annual_downtime_minutes(sweep_parameter(cfg, co, sweep_key, [cur_x])[0])

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
        "Answers: *What is the probability the generator fleet successfully starts AND runs "
        "for a given duration?*  \n"
        "Model: P(mission) = k-of-n convolution of (1 − FTS_i) × exp(−λ_i × t) per unit.  \n"
        "Ref: NREL 2020 EDG study (NREL/TP-5D00-76553)."
    )

    m = result.fleet_mission
    groups = m.groups
    k = m.k_required
    t = m.duration_hours

    # System-level summary
    c1, c2, c3 = st.columns(3)
    c1.metric(
        f"System Mission Success ({t:.0f} h)",
        f"{m.system_mission * 100:.4f}%",
        help=f"P(at least {k} of {sum(g.count for g in groups)} generators complete mission)",
    )
    c2.metric("System Start Success (k-of-n FTS)", f"{m.system_fts_success * 100:.4f}%")
    c3.metric("System Run Success (k-of-n run)", f"{m.system_run_success * 100:.4f}%")

    st.divider()

    # Per-group table
    st.subheader("Per-Group Mission Probability")
    group_rows = []
    for i, g in enumerate(groups):
        p_m = m.group_mission_probs[i]
        group_rows.append({
            "Group": g.name,
            "Units": g.count,
            "FTS (%)": f"{g.fts_probability * 100:.3f}",
            "Start success (%)": f"{(1 - g.fts_probability) * 100:.4f}",
            f"Run success ({t:.0f} h) (%)": f"{mission_reliability(g.lambda_run, t) * 100:.4f}",
            f"Mission success ({t:.0f} h) (%)": f"{p_m * 100:.4f}",
        })
    st.dataframe(pd.DataFrame(group_rows), use_container_width=True, hide_index=True)

    st.divider()

    # Mission success vs time for entire mixed fleet
    st.subheader("System Mission Success vs. Duration")
    t_max = st.slider("Maximum duration to plot (hours)", 24, 720, 168, 24)
    t_vals = np.linspace(0.1, t_max, 300).tolist()

    system_by_t = []
    for t_v in t_vals:
        mission_grps = [(g.count, g.fts_probability, g.lambda_run) for g in groups]
        from reliability import mixed_fleet_mission_prob as _mfmp
        system_by_t.append(_mfmp(mission_grps, k, t_v) * 100)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t_vals, y=system_by_t,
        mode="lines", name=f"System ({k}-of-{sum(g.count for g in groups)})",
        line=dict(color="#2980b9", width=2),
    ))
    # Per-group single-unit lines
    for g in groups:
        single = [(1 - g.fts_probability) * mission_reliability(g.lambda_run, t_v) * 100
                  for t_v in t_vals]
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

    # k sensitivity: how does mission success change with k?
    st.subheader("Mission Success vs. Generators Required (k)")
    n_total = sum(g.count for g in groups)
    k_range = list(range(1, n_total + 1))
    mission_grps_params = [(g.count, g.fts_probability, g.lambda_run) for g in groups]
    from reliability import mixed_fleet_mission_prob as _mfmp2
    k_mission = [_mfmp2(mission_grps_params, kk, t) * 100 for kk in k_range]

    fig3 = go.Figure(go.Scatter(
        x=k_range, y=k_mission,
        mode="lines", line=dict(color="#8e44ad", width=2),
    ))
    fig3.add_vline(x=k, line_dash="dot", line_color="gray",
                   annotation_text=f"k = {k}")
    fig3.update_layout(
        title=f"Mission Success P(fleet completes {t:.0f} h mission) vs. k",
        xaxis_title="k — Generators Required",
        yaxis_title="Mission Success (%)",
        height=360,
    )
    st.plotly_chart(fig3, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Methodology
# ---------------------------------------------------------------------------

def render_methodology_tab(result):
    st.header("Methodology & Assumptions")
    st.subheader("Scope")
    st.markdown("""
**In scope:** prime mover fleet (any mixture, any size) · paralleling switchgear ·
MV/LV switchgear, breakers, bus sections, transformers · UPS (modular k-of-n) ·
PDU/RPP · rack PDUs · IT PSUs · CCF beta-factor between redundant paths.

**Out of scope:** mechanical cooling · IT hardware · BMS/EPMS/control layer ·
fuel supply · structural/civil.
""")

    st.subheader("Core Formulas")
    st.markdown(r"""
**Single component:** $A = \dfrac{MTBF}{MTBF + MTTR}$

**Series system:** $A_s = \prod_i A_i$

**k-of-n identical:** $A_s = \displaystyle\sum_{i=k}^{n} \binom{n}{i} A^i (1-A)^{n-i}$

**Mixed-fleet k-of-n (convolution):**
Joint PMF = $\bigotimes_i \text{Binomial}(n_i, a_i)$, then $A_s = P(\text{working} \geq k)$

**Two-path CCF (beta-factor):** $U_{sys} = (1-\beta)\,U_A\,U_B + \beta\,\max(U_A,U_B)$

**Generator mission (demand + run):** $P = (1-FTS) \cdot e^{-\lambda t}$, applied k-of-n across mixed fleet.
""")

    st.subheader("All Default Parameter Values")
    rows = []
    for key, defn in COMP_DEFAULTS.items():
        rows.append({
            "Component": defn.display_name,
            "MTBF (h)": defn.mtbf_hours,
            "MTTR (h)": defn.mttr_hours,
            "Avail. (%)": round(defn.availability * 100, 8),
            "Status": defn.quality_tag,
            "Source": defn.source,
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
            "Avail. (%)": round(gd.availability * 100, 6),
            "Status": gd.quality_tag,
            "Source": gd.source,
        })
    st.dataframe(pd.DataFrame(gen_rows), use_container_width=True, hide_index=True)

    st.warning("""
**Important limitations:**
1. All ⚠ Placeholder values must be replaced with OEM / CMMS / fleet data before use in final design assurance.
2. VRLA batteries are wear-out items — constant-hazard MTBF is a screening approximation only (use IEEE 1188).
3. Maintenance-induced unavailability is not modeled.
4. Control/BMS/EPMS dependencies are excluded (optimistic).
5. This tool covers the electrical backbone only.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    st.title("⚡ Data Center Electrical RAM Analysis")
    st.caption(
        "Behind-the-meter · Fully islanded · Electrical system only · "
        "Mixed generator fleets of any size supported."
    )

    config, comp_overrides = render_sidebar()

    try:
        result = calculate_system(config, comp_overrides)
    except Exception as exc:
        st.error(f"Calculation error: {exc}")
        st.exception(exc)
        return

    tab_fleet, tab_results, tab_params, tab_sensitivity, tab_mission, tab_method = st.tabs([
        "🏭 Generator Fleet",
        "📊 Results",
        "🔧 Elec. Components",
        "📈 Sensitivity",
        "🔋 Mission Analysis",
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

    with tab_method:
        render_methodology_tab(result)


if __name__ == "__main__":
    main()
