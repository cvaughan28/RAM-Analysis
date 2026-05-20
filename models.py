"""
Topology configuration and system-level availability calculator.

Generation tier supports a **mixed fleet** of arbitrary size:
  - Any number of generator groups, each with its own MTBF / MTTR / FTS / FTLR
  - k-of-n requirement set across the whole fleet (not per type)
  - Uses convolution-based k-of-n for non-identical groups (see reliability.py)

Generator mission model (revised per NRC/INL 2022):
  P_mission = (1 - FTS) * (1 - FTLR) * exp(-lambda_run * t)
  where FTLR = fail-to-load / early carry-load failure per demand

Topology structure
------------------
  Generation Fleet (mixed k-of-n)
       |
       +--- Path A ----------------------------------------------------------+
       |    [Para.SW] -> [Gen Brk] -> [MV Brk] -> [MV Bus] -> [ATS] ->     |
       |    [XFMR] -> [LV Bus] -> [LV Brk] -> UPS (k-of-n) -> PDU -> PSU A |<--+
       |                                                                     |   | LOAD
       +--- Path B ---------------------------------------------- ---------+   | (1-of-2
            (mirror of Path A) -> IT PSU B                                  |<--+  if 2N)

For "Shared Pool" the single mixed k-of-n fleet feeds both distribution paths.
For "Dedicated per Path" the fleet config is replicated per path independently.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from reliability import (
    component_availability,
    series_availability,
    kofn_availability,
    mixed_fleet_kofn_availability,
    mixed_fleet_mission_prob,
    ccf_unavailability,
    kofn_with_ccf,
    mission_reliability,
    annual_downtime_minutes,
    annual_downtime_hours,
    availability_to_nines,
)
from defaults import COMP_DEFAULTS, GEN_DEFAULTS, CompDef


# ---------------------------------------------------------------------------
# Generator group (one homogeneous block within the fleet)
# ---------------------------------------------------------------------------

@dataclass
class GeneratorGroup:
    """
    One homogeneous group of identical prime movers within the generator fleet.

    For a mixed fleet (e.g. 60 small recip + 20 gas turbines + 10 fuel cells),
    create one GeneratorGroup per type/tier.
    """
    name: str                     # user-assigned label, e.g. "Small Recip A"
    count: int                    # number of units in this group
    mtbf_hours: float             # continuous-running MTBF per unit (hours)
    mttr_hours: float             # mean time to repair per unit (hours)
    fts_probability: float        # fail-to-start probability per demand
    ftlr_probability: float = 0.0 # fail-to-load / early carry-load per demand (NRC/INL)
    source: str = "Placeholder"   # data provenance note

    @property
    def availability(self) -> float:
        return component_availability(self.mtbf_hours, self.mttr_hours)

    @property
    def lambda_run(self) -> float:
        return 1.0 / self.mtbf_hours if self.mtbf_hours > 0 else float("inf")

    @property
    def unavailability(self) -> float:
        return 1.0 - self.availability

    @property
    def single_unit_mission_prob(self, t_hours: float = 96.0) -> float:
        """Mission success for one unit at the default 96-hour duration."""
        return (1.0 - self.fts_probability) * (1.0 - self.ftlr_probability) * \
               mission_reliability(self.lambda_run, t_hours)


def default_fleet() -> List[GeneratorGroup]:
    """Default starting fleet: 2 diesel generators (matches original default config)."""
    g = GEN_DEFAULTS["Diesel Generator"]
    return [
        GeneratorGroup(
            name="Diesel Generator Fleet",
            count=2,
            mtbf_hours=g.mtbf_hours,
            mttr_hours=g.mttr_hours,
            fts_probability=g.fts_probability,
            ftlr_probability=g.ftlr_probability,
            source=g.source,
        )
    ]


# ---------------------------------------------------------------------------
# Topology configuration
# ---------------------------------------------------------------------------

@dataclass
class TopologyConfig:
    """
    Complete user-configurable topology for one islanded DC electrical model.
    """

    # ── Prime movers (mixed fleet) ────────────────────────────────────────────
    gen_groups: List[GeneratorGroup] = field(default_factory=default_fleet)
    gen_required: int = 1
    gen_arrangement: str = "Dedicated per Path"  # or "Shared Pool"

    # ── Distribution paths ───────────────────────────────────────────────────
    # num_paths   : total number of redundant power paths (1 to 6)
    # paths_required (k): minimum paths required for system to be UP.
    #   1-of-2 = 2N      |  2-of-3 = 3N/2     |  3-of-4 = 4N/3 or 3+1
    #   For backward compat, paths_required=1 with num_paths=2 = existing 2N.
    num_paths: int = 2
    paths_required: int = 1

    # Component toggles (applied identically to every path)
    include_paralleling_switchgear: bool = True
    include_gen_breaker: bool = False
    include_mv_breaker: bool = False
    include_mv_bus: bool = False
    include_ats: bool = True
    include_transformer: bool = False
    include_lv_bus: bool = True
    include_lv_breaker: bool = True

    include_ups: bool = True
    ups_modules_per_path: int = 4
    ups_modules_required: int = 3
    include_ups_battery: bool = True
    include_ups_sts: bool = False

    include_pdu: bool = True
    pdus_per_path: int = 2
    pdus_required: int = 1

    include_rack_pdu: bool = False
    include_it_psu: bool = False

    # ── Common-cause failure ─────────────────────────────────────────────────
    enable_ccf: bool = True
    ccf_beta: float = 0.02

    # ── Mission analysis ─────────────────────────────────────────────────────
    mission_duration_hours: float = 96.0

    # ── Power source mode ────────────────────────────────────────────────────
    # "islanded"          — generator fleet IS the only source (default).
    # "grid_with_backup"  — utility grid is primary; gen fleet is standby.
    #                       Source availability = A_grid + U_grid * P_mission.
    power_source_mode: str = "islanded"
    grid_mtbf_hours: float = 8_760.0   # Default: ~1 grid outage per year
    grid_mttr_hours: float = 2.0       # Default: 2-hour mean restoration


# ---------------------------------------------------------------------------
# Parameter override helpers
# ---------------------------------------------------------------------------

def resolve_comp_params(
    overrides: Dict[str, Tuple[float, float]]
) -> Dict[str, CompDef]:
    params: Dict[str, CompDef] = {}
    for key, defn in COMP_DEFAULTS.items():
        d = copy.copy(defn)
        if key in overrides:
            mtbf, mttr = overrides[key]
            d.mtbf_hours = max(float(mtbf), 1.0)
            d.mttr_hours = max(float(mttr), 0.0)
            d.is_placeholder = True
        params[key] = d
    return params


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ComponentResult:
    label: str
    availability: float
    is_kofn_group: bool = False
    kofn_desc: str = ""


@dataclass
class PathResult:
    path_name: str
    gen_fleet_availability: float          # raw backup-fleet availability (always populated)
    gen_fleet_desc: str
    components: List[ComponentResult]
    distribution_availability: float
    total_availability: float
    # In grid_with_backup mode, source_availability is the combined grid + backup
    # source feeding this path; in islanded mode, it equals gen_fleet_availability.
    source_availability: float = 0.0
    source_desc: str = ""


@dataclass
class FleetMissionResult:
    """Per-group and system-level mission analysis."""
    groups: List[GeneratorGroup]
    k_required: int
    duration_hours: float
    # Per-group single-unit mission success probabilities
    group_mission_probs: List[float]
    group_start_probs: List[float]    # (1 - FTS) * (1 - FTLR) only
    group_run_probs: List[float]      # exp(-lambda * t) only
    # System-level
    system_mission: float
    system_fts_success: float    # k-of-n FTS+FTLR only (no runtime failure)
    system_run_success: float    # k-of-n run reliability only (perfect start/load)


@dataclass
class SystemResult:
    config: TopologyConfig
    path_results: List[PathResult]
    system_availability: float
    system_unavailability: float
    annual_downtime_min: float
    annual_downtime_h: float
    nines: float
    ccf_applied: bool
    ccf_unavailability_contribution: Optional[float]
    independent_unavailability_contribution: Optional[float]
    fleet_mission: FleetMissionResult
    sensitivity: Dict[str, float]   # label -> min/yr recovered if perfect
    # Fleet summary
    fleet_total_units: int
    fleet_weighted_avg_availability: float
    # Calculation trace for audit
    calc_trace: List[dict] = field(default_factory=list)
    # ── Grid-connection result fields ────────────────────────────────────────
    # power_source_mode mirrors config.power_source_mode for convenience.
    # In "islanded": grid_* fields are None; source_availability == gen_fleet_a.
    # In "grid_with_backup": all four fields are populated.
    power_source_mode: str = "islanded"
    grid_availability: Optional[float] = None
    grid_mtbf_hours: Optional[float] = None
    grid_mttr_hours: Optional[float] = None
    source_availability: float = 0.0       # combined source feeding distribution


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def calculate_system(
    config: TopologyConfig,
    comp_overrides: Optional[Dict[str, Tuple[float, float]]] = None,
) -> SystemResult:
    """
    Calculate system availability for the given topology and parameters.

    Parameters
    ----------
    config        : TopologyConfig (includes gen_groups)
    comp_overrides: {component_key: (mtbf_hours, mttr_hours)}
    """
    if comp_overrides is None:
        comp_overrides = {}

    params = resolve_comp_params(comp_overrides)
    calc_trace = []  # audit trail

    groups = config.gen_groups
    if not groups:
        groups = default_fleet()

    # ── 1. Generation fleet availability ────────────────────────────────────
    fleet_avail_groups = [(g.count, g.availability) for g in groups]
    n_total = sum(g.count for g in groups)
    k_req = max(1, min(config.gen_required, n_total))

    gen_fleet_a = mixed_fleet_kofn_availability(fleet_avail_groups, k_req)

    total_units = n_total
    weighted_avg_a = sum(g.count * g.availability for g in groups) / n_total if n_total > 0 else 0.0

    gen_fleet_desc = (
        f"Mixed fleet: {n_total} total units across {len(groups)} group(s), "
        f"{k_req} required"
    )

    calc_trace.append({
        "step": "Generator Fleet",
        "method": f"Mixed k-of-n convolution ({k_req}-of-{n_total})",
        "inputs": {f"Group '{g.name}' (x{g.count})": f"A={g.availability:.8f}" for g in groups},
        "result": gen_fleet_a,
        "formula": "A_fleet = P(sum of Binom(n_i, a_i) >= k) via PMF convolution",
    })

    # ── 1b. Fleet mission analysis (computed early so source can use it) ────
    t = config.mission_duration_hours
    mission_groups_params = [(g.count, g.fts_probability, g.ftlr_probability, g.lambda_run) for g in groups]
    system_mission = mixed_fleet_mission_prob(mission_groups_params, k_req, t)
    # FTS+FTLR only (lambda=0 means R(t)=1, perfect run reliability)
    fts_only_groups = [(g.count, g.fts_probability, g.ftlr_probability, 0.0) for g in groups]
    system_fts_success = mixed_fleet_mission_prob(fts_only_groups, k_req, t)
    # Run only (FTS=0, FTLR=0 means start/load always succeeds)
    run_only_groups = [(g.count, 0.0, 0.0, g.lambda_run) for g in groups]
    system_run_success = mixed_fleet_mission_prob(run_only_groups, k_req, t)
    group_mission_probs = [
        (1.0 - g.fts_probability) * (1.0 - g.ftlr_probability) * mission_reliability(g.lambda_run, t)
        for g in groups
    ]
    group_start_probs = [
        (1.0 - g.fts_probability) * (1.0 - g.ftlr_probability)
        for g in groups
    ]
    group_run_probs = [
        mission_reliability(g.lambda_run, t)
        for g in groups
    ]
    fleet_mission = FleetMissionResult(
        groups=groups,
        k_required=k_req,
        duration_hours=t,
        group_mission_probs=group_mission_probs,
        group_start_probs=group_start_probs,
        group_run_probs=group_run_probs,
        system_mission=system_mission,
        system_fts_success=system_fts_success,
        system_run_success=system_run_success,
    )

    # ── 1c. Power source (grid + backup combination, if applicable) ─────────
    # In islanded mode the gen fleet IS the source.  In grid_with_backup mode
    # the source is `grid` in parallel with `backup-fleet-on-mission`:
    #     A_source = A_grid + U_grid * P_backup_mission
    # where P_backup_mission is evaluated at the user's mission_duration_hours
    # (interpreted as the assumed worst-case grid outage duration).
    grid_avail: Optional[float] = None
    if config.power_source_mode == "grid_with_backup":
        grid_avail = component_availability(config.grid_mtbf_hours, config.grid_mttr_hours)
        u_grid = 1.0 - grid_avail
        source_avail = grid_avail + u_grid * system_mission
        source_desc = (
            f"Grid (MTBF={config.grid_mtbf_hours:,.0f} h, "
            f"MTTR={config.grid_mttr_hours:.2f} h) "
            f"+ Backup ({n_total} units, {k_req} required)"
        )
        calc_trace.append({
            "step": "Grid Connection",
            "method": "Single-source MTBF/MTTR",
            "inputs": {
                "Grid MTBF": f"{config.grid_mtbf_hours:,.0f} h",
                "Grid MTTR": f"{config.grid_mttr_hours:.2f} h",
            },
            "result": grid_avail,
            "formula": "A_grid = MTBF / (MTBF + MTTR)",
        })
        calc_trace.append({
            "step": "Combined Source (Grid + Backup)",
            "method": "Source up if grid up OR (grid down AND backup mission succeeds)",
            "inputs": {
                "A_grid": f"{grid_avail:.8f}",
                "U_grid": f"{u_grid:.4e}",
                "P_backup_mission": f"{system_mission:.8f}",
                "Assumed outage duration": f"{t:.0f} h",
            },
            "result": source_avail,
            "formula": "A_source = A_grid + U_grid * P_mission",
        })
    else:
        source_avail = gen_fleet_a
        source_desc = f"Islanded - backup fleet IS the source ({gen_fleet_desc})"

    # ── 2. Distribution path components (series chain) ───────────────────────
    def build_dist_components(p: Dict[str, CompDef], cfg: TopologyConfig) -> List[ComponentResult]:
        items: List[ComponentResult] = []

        def add(key: str):
            comp = p[key]
            items.append(ComponentResult(label=comp.display_name, availability=comp.availability))

        if cfg.include_paralleling_switchgear:
            add("paralleling_switchgear")
        if cfg.include_gen_breaker:
            add("gen_breaker")
        if cfg.include_mv_breaker:
            add("mv_breaker")
        if cfg.include_mv_bus:
            add("mv_bus_section")
        if cfg.include_ats:
            add("ats_transfer_switch")
        if cfg.include_transformer:
            add("transformer")
        if cfg.include_lv_bus:
            add("lv_bus_section")
        if cfg.include_lv_breaker:
            add("lv_breaker")

        if cfg.include_ups:
            mod_a = p["ups_module"].availability
            ups_sys_a = kofn_availability(cfg.ups_modules_per_path, cfg.ups_modules_required, mod_a)
            items.append(ComponentResult(
                label="UPS System",
                availability=ups_sys_a,
                is_kofn_group=True,
                kofn_desc=f"{cfg.ups_modules_required}-of-{cfg.ups_modules_per_path} modules",
            ))
            if cfg.include_ups_battery:
                add("ups_battery_string")
            if cfg.include_ups_sts:
                add("ups_static_switch")

        if cfg.include_pdu:
            if config.pdus_per_path > 1:
                pdu_a = p["pdu_rpp"].availability
                pdu_sys_a = kofn_availability(cfg.pdus_per_path, cfg.pdus_required, pdu_a)
                items.append(ComponentResult(
                    label="PDU / RPP Tier",
                    availability=pdu_sys_a,
                    is_kofn_group=True,
                    kofn_desc=f"{cfg.pdus_required}-of-{cfg.pdus_per_path} units",
                ))
            else:
                add("pdu_rpp")

        if cfg.include_rack_pdu:
            add("rack_pdu")
        if cfg.include_it_psu:
            add("it_psu")

        return items

    dist_components = build_dist_components(params, config)
    dist_a = series_availability([c.availability for c in dist_components])

    running_a = 1.0
    for c in dist_components:
        running_a *= c.availability
        calc_trace.append({
            "step": f"Dist. series: {c.label}",
            "method": "Series multiply" + (f" [{c.kofn_desc}]" if c.is_kofn_group else ""),
            "inputs": {"Component A": f"{c.availability:.8f}", "Running product": f"{running_a/c.availability:.8f}"},
            "result": running_a,
            "formula": "A_series = product(A_i)",
        })

    # ── 3. Per-path total availability ───────────────────────────────────────
    # Uses source_avail (= gen fleet in islanded mode, or grid+backup combined
    # in grid mode) wherever gen fleet's continuous availability was used before.
    if config.gen_arrangement == "Dedicated per Path":
        path_total_a = source_avail * dist_a
    else:
        path_total_a = dist_a

    # Resolve effective k_paths (paths_required), with sensible defaults
    # for older configs that may not have it set explicitly.
    k_paths = config.paths_required
    if k_paths <= 0:
        # Backward-compat default: 1 for any redundant config (2N pattern)
        k_paths = 1 if config.num_paths >= 2 else 1
    k_paths = max(1, min(k_paths, max(1, config.num_paths)))

    # Path name generation (supports up to 26 paths using A-Z)
    def _path_name(i: int, total: int) -> str:
        if total == 1:
            return "Path"
        return f"Path {chr(ord('A') + i)}" if i < 26 else f"Path {i+1}"

    path_results = []
    for i in range(config.num_paths):
        path_results.append(PathResult(
            path_name=_path_name(i, config.num_paths),
            gen_fleet_availability=gen_fleet_a,
            gen_fleet_desc=gen_fleet_desc,
            components=dist_components,
            distribution_availability=dist_a,
            total_availability=path_total_a,
            source_availability=source_avail,
            source_desc=source_desc,
        ))

    # ── 4. System availability ────────────────────────────────────────────────
    # Unified k-of-n formulation: kofn_with_ccf reduces to the existing 2N
    # formula for n=2, k=1, so prior numerical results are preserved.
    ccf_unavail_contrib = None
    indep_unavail_contrib = None

    src_lbl = "Gen fleet" if config.power_source_mode == "islanded" \
              else "Source (grid+backup)"

    # Topology label for trace readability
    if config.num_paths == 1:
        topo_label = "Single Path"
    elif config.num_paths == 2 and k_paths == 1:
        topo_label = "2N"
    elif config.num_paths == 2 and k_paths == 2:
        topo_label = "Series 2-of-2 (no redundancy)"
    else:
        topo_label = f"{config.num_paths}N/{k_paths} ({k_paths}-of-{config.num_paths})"

    if config.num_paths == 1:
        system_a = source_avail * dist_a
        ccf_applied = False
        calc_trace.append({
            "step": f"System ({topo_label})",
            "method": f"Series: {src_lbl} x Distribution path",
            "inputs": {src_lbl: f"{source_avail:.8f}", "Distribution": f"{dist_a:.8f}"},
            "result": system_a,
            "formula": "A_sys = A_source * A_dist",
        })

    else:  # num_paths >= 2 — use unified k-of-n with CCF
        beta = config.ccf_beta if config.enable_ccf else 0.0
        ccf_applied = config.enable_ccf

        if config.gen_arrangement == "Dedicated per Path":
            # Each path has its own source + dist chain; CCF couples them.
            u_path = 1.0 - path_total_a
            a_kofn_indep = kofn_availability(config.num_paths, k_paths, path_total_a)
            p_indep_fail = 1.0 - a_kofn_indep
            ccf_unavail_contrib = beta * u_path
            indep_unavail_contrib = (1.0 - beta) * p_indep_fail
            u_sys = ccf_unavail_contrib + indep_unavail_contrib
            system_a = max(0.0, 1.0 - u_sys)
            calc_trace.append({
                "step": f"System ({topo_label}{' + CCF' if ccf_applied else ', no CCF'})",
                "method": (
                    f"k-of-n parallel with beta-factor CCF"
                    f" (k={k_paths}, n={config.num_paths})"
                ),
                "inputs": {
                    "U_path": f"{u_path:.2e}",
                    "k of n": f"{k_paths} of {config.num_paths}",
                    "beta": f"{beta:.3f}",
                    "U_indep (binomial)": f"{indep_unavail_contrib:.2e}",
                    "U_ccf": f"{ccf_unavail_contrib:.2e}",
                },
                "result": system_a,
                "formula": "U_sys = beta * U_path + (1-beta) * P(< k of n paths up)",
            })

        else:  # Shared Pool — single source upstream, n dist paths with k-of-n
            u_source = 1.0 - source_avail
            u_dist = 1.0 - dist_a
            a_dist_kofn_indep = kofn_availability(config.num_paths, k_paths, dist_a)
            p_dist_indep_fail = 1.0 - a_dist_kofn_indep
            ccf_unavail_contrib = beta * u_dist
            indep_unavail_contrib = (1.0 - beta) * p_dist_indep_fail
            u_dist_pool = ccf_unavail_contrib + indep_unavail_contrib
            u_sys = u_source + source_avail * u_dist_pool
            system_a = max(0.0, 1.0 - u_sys)
            calc_trace.append({
                "step": f"System (Shared Pool, {topo_label}"
                        f"{' + CCF' if ccf_applied else ', no CCF'})",
                "method": (
                    f"Shared {src_lbl} + k-of-n dist paths with beta-factor CCF"
                ),
                "inputs": {
                    "U_source": f"{u_source:.2e}",
                    "U_dist (each)": f"{u_dist:.2e}",
                    "k of n": f"{k_paths} of {config.num_paths}",
                    "beta": f"{beta:.3f}",
                    "U_dist_pool": f"{u_dist_pool:.2e}",
                },
                "result": system_a,
                "formula": "U_sys = U_source + A_source * U_dist_pool",
            })

    system_u = 1.0 - system_a

    # ── 6. Sensitivity ────────────────────────────────────────────────────────
    sensitivity: Dict[str, float] = {}

    _beta_eff = config.ccf_beta if config.enable_ccf else 0.0

    def _sys_avail_with_source(source_a_override: float) -> float:
        """Recompute system availability with a hypothetical source availability."""
        if config.num_paths == 1:
            return source_a_override * dist_a
        if config.gen_arrangement == "Dedicated per Path":
            u_p = 1.0 - (source_a_override * dist_a)
            return 1.0 - kofn_with_ccf(config.num_paths, k_paths, u_p, _beta_eff)
        else:
            u_d = 1.0 - dist_a
            u_pool = kofn_with_ccf(config.num_paths, k_paths, u_d, _beta_eff)
            u_src = 1.0 - source_a_override
            return max(0.0, 1.0 - (u_src + source_a_override * u_pool))

    def _sys_avail_with_dist(dist_a_override: float) -> float:
        if config.num_paths == 1:
            return source_avail * dist_a_override
        if config.gen_arrangement == "Dedicated per Path":
            u_p = 1.0 - (source_avail * dist_a_override)
            return 1.0 - kofn_with_ccf(config.num_paths, k_paths, u_p, _beta_eff)
        else:
            u_d = 1.0 - dist_a_override
            u_pool = kofn_with_ccf(config.num_paths, k_paths, u_d, _beta_eff)
            u_src = 1.0 - source_avail
            return max(0.0, 1.0 - (u_src + source_avail * u_pool))

    # ─ Source (top-level) sensitivity ────────────────────────────────────────
    perfect_source_a = _sys_avail_with_source(1.0)
    source_delta_min = annual_downtime_minutes(system_a) - annual_downtime_minutes(perfect_source_a)
    if config.power_source_mode == "grid_with_backup":
        source_label = (
            f"Source (Grid + Backup) - any one perfectly reliable. "
            f"Grid: MTBF={config.grid_mtbf_hours:,.0f}h, MTTR={config.grid_mttr_hours:.1f}h"
        )
    else:
        source_label = f"Generator Fleet ({n_total} units, {k_req} required)"
    sensitivity[source_label] = source_delta_min

    # ─ Grid-only sensitivity (grid_with_backup mode only) ────────────────────
    # "What if just the grid feed had infinite MTBF, with backup unchanged?"
    # In the OR-redundancy model this yields the same delta as making the
    # backup perfect, but it's reported separately so the user can see that
    # the grid quality matters at all.
    if config.power_source_mode == "grid_with_backup":
        # source_avail with A_grid = 1 -> source = 1.  Same delta as above,
        # but labeled clearly so it's visible in the report.
        sensitivity[
            f"  -> Grid feed alone (MTBF={config.grid_mtbf_hours:,.0f}h)"
        ] = source_delta_min

    # ─ Per-group sensitivity ────────────────────────────────────────────────
    # In islanded mode, "perfect group" -> higher fleet availability -> higher source.
    # In grid mode, this is an approximation (uses continuous availability proxy
    # for the group's contribution; the true effect would route through mission
    # probability with lambda_run -> 0). Sufficient for screening; flagged in docs.
    for i, grp in enumerate(groups):
        if grp.count == 0:
            continue
        perfect_groups = [(g.count, 1.0 if j == i else g.availability)
                          for j, g in enumerate(groups)]
        perf_fleet_a = mixed_fleet_kofn_availability(perfect_groups, k_req)
        if config.power_source_mode == "grid_with_backup":
            # Carry perf_fleet_a through the source combination
            u_g_now = 1.0 - (grid_avail or 1.0)
            perf_source_a = (grid_avail or 1.0) + u_g_now * perf_fleet_a
        else:
            perf_source_a = perf_fleet_a
        perf_sys_a = _sys_avail_with_source(perf_source_a)
        delta = annual_downtime_minutes(system_a) - annual_downtime_minutes(perf_sys_a)
        if abs(delta) > 1e-9:
            sensitivity[f"  -> {grp.name} (x{grp.count} units, A={grp.availability*100:.4f}%)"] = delta

    # ─ Distribution components ──────────────────────────────────────────────
    for comp in dist_components:
        if comp.availability >= 1.0:
            continue
        perfect_comps_avails = [
            1.0 if c.label == comp.label else c.availability
            for c in dist_components
        ]
        perfect_dist_a = series_availability(perfect_comps_avails)
        perf_sys_a = _sys_avail_with_dist(perfect_dist_a)
        delta = annual_downtime_minutes(system_a) - annual_downtime_minutes(perf_sys_a)
        if abs(delta) > 1e-9:
            sensitivity[comp.label] = delta

    # CCF sensitivity ladder — three rungs at decreasing beta values, so the
    # tornado chart shows the marginal value of design diversification (not
    # just the "perfect independence" extreme).
    if config.enable_ccf and config.num_paths > 1 and config.ccf_beta > 0:
        base_dt = annual_downtime_minutes(system_a)

        def _system_avail_at_beta(beta_new: float) -> float:
            if config.gen_arrangement == "Dedicated per Path":
                u_p = 1.0 - path_total_a
                return 1.0 - kofn_with_ccf(config.num_paths, k_paths, u_p, beta_new)
            else:
                u_d = 1.0 - dist_a
                u_pool = kofn_with_ccf(config.num_paths, k_paths, u_d, beta_new)
                u_src = 1.0 - source_avail
                return max(0.0, 1.0 - (u_src + source_avail * u_pool))

        beta_ladder = [
            (config.ccf_beta * 0.5,  "halved"),
            (config.ccf_beta * 0.25, "quartered"),
            (0.0,                    "= 0 (perfect independence)"),
        ]
        for beta_new, descriptor in beta_ladder:
            a_new = _system_avail_at_beta(beta_new)
            delta = base_dt - annual_downtime_minutes(a_new)
            if abs(delta) > 1e-9:
                sensitivity[
                    f"CCF beta {descriptor}  "
                    f"(b = {beta_new:.4f} vs current {config.ccf_beta:.3f})"
                ] = delta

    return SystemResult(
        config=config,
        path_results=path_results,
        system_availability=system_a,
        system_unavailability=system_u,
        annual_downtime_min=annual_downtime_minutes(system_a),
        annual_downtime_h=annual_downtime_hours(system_a),
        nines=availability_to_nines(system_a),
        ccf_applied=ccf_applied,
        ccf_unavailability_contribution=ccf_unavail_contrib,
        independent_unavailability_contribution=indep_unavail_contrib,
        fleet_mission=fleet_mission,
        sensitivity=sensitivity,
        fleet_total_units=total_units,
        fleet_weighted_avg_availability=weighted_avg_a,
        calc_trace=calc_trace,
        # ── Grid-connection result fields ────────────────────────────────────
        power_source_mode=config.power_source_mode,
        grid_availability=grid_avail,
        grid_mtbf_hours=(config.grid_mtbf_hours
                         if config.power_source_mode == "grid_with_backup" else None),
        grid_mttr_hours=(config.grid_mttr_hours
                         if config.power_source_mode == "grid_with_backup" else None),
        source_availability=source_avail,
    )


# ---------------------------------------------------------------------------
# Sweep helper
# ---------------------------------------------------------------------------

def sweep_parameter(
    config: TopologyConfig,
    comp_overrides: Dict[str, Tuple[float, float]],
    sweep_key: str,
    values: List[float],
) -> List[float]:
    """
    Sweep one parameter over a range of values and return system availabilities.

    sweep_key options:
      "gen_fleet_scale"         -> scale ALL gen MTBFs by the given multiplier
      "gen_fts_all"             -> set ALL groups' FTS probability to the given value
      "gen_ftlr_all"            -> set ALL groups' FTLR probability to the given value
      "gen_group_mtbf_{i}"      -> set group i's MTBF to the value
      "ccf_beta"                -> vary CCF beta
      "<comp_key>"              -> vary that component's MTBF (MTTR held constant)
    """
    results = []
    for v in values:
        cfg = copy.deepcopy(config)
        co = dict(comp_overrides)

        if sweep_key == "gen_fleet_scale":
            cfg.gen_groups = [
                GeneratorGroup(
                    name=g.name, count=g.count,
                    mtbf_hours=max(1.0, g.mtbf_hours * v),
                    mttr_hours=g.mttr_hours,
                    fts_probability=g.fts_probability,
                    ftlr_probability=g.ftlr_probability,
                    source=g.source,
                )
                for g in config.gen_groups
            ]
        elif sweep_key == "gen_fts_all":
            cfg.gen_groups = [
                GeneratorGroup(
                    name=g.name, count=g.count,
                    mtbf_hours=g.mtbf_hours, mttr_hours=g.mttr_hours,
                    fts_probability=max(0.0, min(1.0, v)),
                    ftlr_probability=g.ftlr_probability,
                    source=g.source,
                )
                for g in config.gen_groups
            ]
        elif sweep_key == "gen_ftlr_all":
            cfg.gen_groups = [
                GeneratorGroup(
                    name=g.name, count=g.count,
                    mtbf_hours=g.mtbf_hours, mttr_hours=g.mttr_hours,
                    fts_probability=g.fts_probability,
                    ftlr_probability=max(0.0, min(1.0, v)),
                    source=g.source,
                )
                for g in config.gen_groups
            ]
        elif sweep_key.startswith("gen_group_mtbf_"):
            idx = int(sweep_key.split("_")[-1])
            new_groups = list(config.gen_groups)
            if 0 <= idx < len(new_groups):
                g = new_groups[idx]
                new_groups[idx] = GeneratorGroup(
                    name=g.name, count=g.count,
                    mtbf_hours=max(1.0, v),
                    mttr_hours=g.mttr_hours,
                    fts_probability=g.fts_probability,
                    ftlr_probability=g.ftlr_probability,
                    source=g.source,
                )
            cfg.gen_groups = new_groups
        elif sweep_key == "ccf_beta":
            cfg.ccf_beta = float(v)
        elif sweep_key in COMP_DEFAULTS:
            base_mttr = co.get(
                sweep_key,
                (COMP_DEFAULTS[sweep_key].mtbf_hours, COMP_DEFAULTS[sweep_key].mttr_hours)
            )[1]
            co[sweep_key] = (max(1.0, v), base_mttr)

        r = calculate_system(cfg, co)
        results.append(r.system_availability)
    return results
