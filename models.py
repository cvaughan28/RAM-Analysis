"""
Topology configuration and system-level availability calculator.

Generation tier now supports a **mixed fleet** of arbitrary size:
  - Any number of generator groups, each with its own MTBF / MTTR / FTS
  - k-of-n requirement set across the whole fleet (not per type)
  - Uses convolution-based k-of-n for non-identical groups (see reliability.py)

Topology structure
------------------
  Generation Fleet (mixed k-of-n)
       │
       ├─── Path A ─────────────────────────────────────────────────────────┐
       │    [Para.SW] ► [Gen Brk] ► [MV Brk] ► [MV Bus] ► [ATS] ►        │
       │    [XFMR] ► [LV Bus] ► [LV Brk] ► UPS (k-of-n) ► PDU ► IT PSU A │◄──┐
       │                                                                    │   │ LOAD
       └─── Path B ─────────────────────────────────────────────────────────┐   │ (1-of-2
            (mirror of Path A) ► IT PSU B                                   │◄──┘  if 2N)

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
    name: str                    # user-assigned label, e.g. "Small Recip A"
    count: int                   # number of units in this group
    mtbf_hours: float            # continuous-running MTBF per unit (hours)
    mttr_hours: float            # mean time to repair per unit (hours)
    fts_probability: float       # fail-to-start probability (for mission analysis)
    source: str = "Placeholder"  # data provenance note

    @property
    def availability(self) -> float:
        return component_availability(self.mtbf_hours, self.mttr_hours)

    @property
    def lambda_run(self) -> float:
        return 1.0 / self.mtbf_hours if self.mtbf_hours > 0 else float("inf")

    @property
    def unavailability(self) -> float:
        return 1.0 - self.availability


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
    # gen_groups defines the fleet composition (per path if Dedicated, total if Shared).
    gen_groups: List[GeneratorGroup] = field(default_factory=default_fleet)

    # Minimum generators required from the fleet:
    #   Dedicated per Path → required per path
    #   Shared Pool        → required across the whole pool
    gen_required: int = 1

    # "Dedicated per Path" → each path has its own independent fleet (gen_groups is per path)
    # "Shared Pool"        → one common fleet feeds all distribution paths
    gen_arrangement: str = "Dedicated per Path"

    # ── Distribution paths ───────────────────────────────────────────────────
    num_paths: int = 2

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
    gen_fleet_availability: float
    gen_fleet_desc: str
    components: List[ComponentResult]
    distribution_availability: float
    total_availability: float


@dataclass
class FleetMissionResult:
    """Per-group and system-level mission analysis."""
    groups: List[GeneratorGroup]
    k_required: int
    duration_hours: float
    # Per-group single-unit mission success probabilities
    group_mission_probs: List[float]
    # System-level
    system_mission: float
    system_fts_success: float    # k-of-n FTS only (no runtime failure)
    system_run_success: float    # k-of-n run reliability only (perfect start)


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
    sensitivity: Dict[str, float]   # label → min/yr recovered if perfect
    # Fleet summary
    fleet_total_units: int
    fleet_weighted_avg_availability: float


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

    groups = config.gen_groups
    if not groups:
        groups = default_fleet()

    # ── 1. Generation fleet availability ────────────────────────────────────
    fleet_avail_groups = [(g.count, g.availability) for g in groups]
    n_total = sum(g.count for g in groups)
    k_req = max(1, min(config.gen_required, n_total))

    gen_fleet_a = mixed_fleet_kofn_availability(fleet_avail_groups, k_req)

    # Summary metrics for the fleet
    total_units = n_total
    weighted_avg_a = sum(g.count * g.availability for g in groups) / n_total if n_total > 0 else 0.0

    gen_fleet_desc = (
        f"Mixed fleet: {n_total} total units across {len(groups)} group(s), "
        f"{k_req} required"
    )

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

    # ── 3. Per-path total availability ───────────────────────────────────────
    if config.gen_arrangement == "Dedicated per Path":
        path_total_a = gen_fleet_a * dist_a
    else:
        # Shared pool: each path's individual availability is just its dist chain.
        # Gen pool failure is folded into system-level calc below.
        path_total_a = dist_a

    path_results = []
    for i in range(config.num_paths):
        name = ["Path A", "Path B"][i] if config.num_paths == 2 else "Path"
        path_results.append(PathResult(
            path_name=name,
            gen_fleet_availability=gen_fleet_a,
            gen_fleet_desc=gen_fleet_desc,
            components=dist_components,
            distribution_availability=dist_a,
            total_availability=path_total_a,
        ))

    # ── 4. System availability ────────────────────────────────────────────────
    ccf_unavail_contrib = None
    indep_unavail_contrib = None

    if config.num_paths == 1:
        system_a = gen_fleet_a * dist_a
        ccf_applied = False

    else:  # 2-path dual-cord system
        if config.gen_arrangement == "Dedicated per Path":
            u_path = 1.0 - path_total_a
            if config.enable_ccf:
                u_sys = ccf_unavailability(u_path, u_path, config.ccf_beta)
                indep_unavail_contrib = (1.0 - config.ccf_beta) * u_path * u_path
                ccf_unavail_contrib = config.ccf_beta * u_path
                ccf_applied = True
            else:
                u_sys = u_path * u_path
                ccf_applied = False
            system_a = 1.0 - u_sys

        else:  # Shared pool
            u_gen = 1.0 - gen_fleet_a
            u_dist = 1.0 - dist_a
            if config.enable_ccf:
                u_both_dist = ccf_unavailability(u_dist, u_dist, config.ccf_beta)
                indep_unavail_contrib = (1.0 - config.ccf_beta) * u_dist * u_dist
                ccf_unavail_contrib = config.ccf_beta * u_dist
                ccf_applied = True
            else:
                u_both_dist = u_dist * u_dist
                ccf_applied = False
            u_sys = u_gen + gen_fleet_a * u_both_dist
            system_a = max(0.0, 1.0 - u_sys)

    system_u = 1.0 - system_a

    # ── 5. Mission analysis ───────────────────────────────────────────────────
    t = config.mission_duration_hours
    mission_groups_params = [(g.count, g.fts_probability, g.lambda_run) for g in groups]
    system_mission = mixed_fleet_mission_prob(mission_groups_params, k_req, t)

    # FTS-only and run-only contributions
    fts_only_groups = [(g.count, g.fts_probability, 0.0) for g in groups]   # lambda=0 → R(t)=1
    system_fts_success = mixed_fleet_mission_prob(fts_only_groups, k_req, t)

    run_only_groups = [(g.count, 0.0, g.lambda_run) for g in groups]        # FTS=0 → start always
    system_run_success = mixed_fleet_mission_prob(run_only_groups, k_req, t)

    group_mission_probs = [
        (1.0 - g.fts_probability) * mission_reliability(g.lambda_run, t)
        for g in groups
    ]

    fleet_mission = FleetMissionResult(
        groups=groups,
        k_required=k_req,
        duration_hours=t,
        group_mission_probs=group_mission_probs,
        system_mission=system_mission,
        system_fts_success=system_fts_success,
        system_run_success=system_run_success,
    )

    # ── 6. Sensitivity ────────────────────────────────────────────────────────
    sensitivity: Dict[str, float] = {}

    def _sys_avail_with_fleet(fleet_a_override: float) -> float:
        """System availability if gen fleet availability = fleet_a_override."""
        if config.num_paths == 1:
            return fleet_a_override * dist_a
        if config.gen_arrangement == "Dedicated per Path":
            path_a = fleet_a_override * dist_a
            u_p = 1.0 - path_a
            if config.enable_ccf:
                u_s = ccf_unavailability(u_p, u_p, config.ccf_beta)
            else:
                u_s = u_p * u_p
            return 1.0 - u_s
        else:
            u_g = 1.0 - fleet_a_override
            u_d = 1.0 - dist_a
            if config.enable_ccf:
                u_b = ccf_unavailability(u_d, u_d, config.ccf_beta)
            else:
                u_b = u_d * u_d
            return max(0.0, 1.0 - (u_g + fleet_a_override * u_b))

    def _sys_avail_with_dist(dist_a_override: float) -> float:
        """System availability if distribution path availability = dist_a_override."""
        if config.num_paths == 1:
            return gen_fleet_a * dist_a_override
        if config.gen_arrangement == "Dedicated per Path":
            path_a = gen_fleet_a * dist_a_override
            u_p = 1.0 - path_a
            if config.enable_ccf:
                u_s = ccf_unavailability(u_p, u_p, config.ccf_beta)
            else:
                u_s = u_p * u_p
            return 1.0 - u_s
        else:
            u_g = 1.0 - gen_fleet_a
            u_d = 1.0 - dist_a_override
            if config.enable_ccf:
                u_b = ccf_unavailability(u_d, u_d, config.ccf_beta)
            else:
                u_b = u_d * u_d
            return max(0.0, 1.0 - (u_g + gen_fleet_a * u_b))

    # Gen fleet: perfect fleet (all groups at 100%)
    perfect_fleet_a = _sys_avail_with_fleet(1.0)
    fleet_delta_min = annual_downtime_minutes(system_a) - annual_downtime_minutes(perfect_fleet_a)
    sensitivity[f"Generator Fleet ({n_total} units, {k_req} required)"] = fleet_delta_min

    # Per-group sensitivity: what if this group were doubled in availability?
    for i, grp in enumerate(groups):
        if grp.count == 0:
            continue
        # Replace this group with perfect-availability version
        perfect_groups = [(g.count, 1.0 if j == i else g.availability)
                          for j, g in enumerate(groups)]
        perf_fleet_a = mixed_fleet_kofn_availability(perfect_groups, k_req)
        perf_sys_a = _sys_avail_with_fleet(perf_fleet_a)
        delta = annual_downtime_minutes(system_a) - annual_downtime_minutes(perf_sys_a)
        if abs(delta) > 1e-9:
            sensitivity[f"  → {grp.name} (×{grp.count} units, A={grp.availability*100:.4f}%)"] = delta

    # Distribution components
    for comp in dist_components:
        if comp.availability >= 1.0:
            continue
        # Perfect this component
        perfect_comps_avails = [
            1.0 if c.label == comp.label else c.availability
            for c in dist_components
        ]
        perfect_dist_a = series_availability(perfect_comps_avails)
        perf_sys_a = _sys_avail_with_dist(perfect_dist_a)
        delta = annual_downtime_minutes(system_a) - annual_downtime_minutes(perf_sys_a)
        if abs(delta) > 1e-9:
            sensitivity[comp.label] = delta

    # CCF sensitivity
    if config.enable_ccf and config.num_paths > 1:
        if config.gen_arrangement == "Dedicated per Path":
            u_p = 1.0 - path_total_a
            a_no_ccf = 1.0 - u_p * u_p
        else:
            u_d = 1.0 - dist_a
            u_g = 1.0 - gen_fleet_a
            a_no_ccf = max(0.0, 1.0 - (u_g + gen_fleet_a * u_d * u_d))
        ccf_delta = annual_downtime_minutes(system_a) - annual_downtime_minutes(a_no_ccf)
        if abs(ccf_delta) > 1e-9:
            sensitivity[f"CCF Beta Factor (b = {config.ccf_beta:.3f})"] = ccf_delta

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
      "gen_fleet_scale"         → scale ALL gen MTBFs by the given multiplier
      "gen_fts_all"             → set ALL groups' FTS probability to the given value
      "gen_group_mtbf_{i}"      → set group i's MTBF to the value
      "ccf_beta"                → vary CCF beta
      "<comp_key>"              → vary that component's MTBF (MTTR held constant)
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
