"""
Core reliability mathematics for RAM analysis.
All functions are pure — no UI, no I/O.

References:
  - IEEE 493: series/parallel RBD methods for power systems
  - Beta-factor CCF model (IEC 61508 / IEC TR 62380)
  - NREL 2020 EDG study (NREL/TP-5D00-76553): generator mission modeling
  - NRC/INL 2022 EDG Performance: FTS, FTLR, FTR>1h rates

Mission model (revised per NRC/INL 2022):
  P_mission = (1 - FTS) * (1 - FTLR) * exp(-lambda_run * t)
  where:
    FTS  = fail-to-start probability per demand
    FTLR = fail-to-load / early carry-load failure probability per demand
    lambda_run = run-failure rate after first hour (failures per hour)
    t    = mission duration (hours)
"""

import math
from typing import List


# ---------------------------------------------------------------------------
# Single-component
# ---------------------------------------------------------------------------

def component_availability(mtbf_h: float, mttr_h: float) -> float:
    """Inherent (steady-state) availability: A = MTBF / (MTBF + MTTR)."""
    if mtbf_h <= 0:
        return 0.0
    return mtbf_h / (mtbf_h + mttr_h)


def component_unavailability(mtbf_h: float, mttr_h: float) -> float:
    return 1.0 - component_availability(mtbf_h, mttr_h)


def failure_rate_per_million_hours(mtbf_h: float) -> float:
    return 1_000_000.0 / mtbf_h if mtbf_h > 0 else float("inf")


# ---------------------------------------------------------------------------
# Series / parallel / k-of-n
# ---------------------------------------------------------------------------

def series_availability(availabilities: List[float]) -> float:
    """All-series system: all components must work. A_s = prod(A_i)."""
    result = 1.0
    for a in availabilities:
        result *= a
    return result


def parallel_availability(availabilities: List[float]) -> float:
    """Any-one-of-n parallel: system works if at least one works."""
    result = 1.0
    for a in availabilities:
        result *= (1.0 - a)
    return 1.0 - result


def kofn_availability(n: int, k: int, a: float) -> float:
    """
    k-of-n identical active components: system requires at least k of n working.

    Uses the binomial CDF:
        A_sys = sum_{i=k}^{n} C(n,i) * a^i * (1-a)^(n-i)

    Special cases:
      k <= 0  -> always available (1.0)
      k >  n  -> never available (0.0)
      n == 1  -> equals a
    """
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if n == 1:
        return a
    q = 1.0 - a
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (a ** i) * (q ** (n - i))
    return total


# ---------------------------------------------------------------------------
# Two-path system with common-cause failure (beta-factor model)
# ---------------------------------------------------------------------------

def ccf_unavailability(unavail_a: float, unavail_b: float, beta: float) -> float:
    """
    Two-path system unavailability with common-cause failures (beta-factor model).

    U_sys = (1 - beta) * U_A * U_B  +  beta * max(U_A, U_B)

    The first term is independent coincident failure; the second is CCF
    affecting both paths simultaneously.
    """
    independent = (1.0 - beta) * unavail_a * unavail_b
    common = beta * max(unavail_a, unavail_b)
    return independent + common


def kofn_with_ccf(n: int, k: int, u_path: float, beta: float) -> float:
    """
    k-of-n parallel system unavailability with beta-factor CCF.

    Generalizes ``ccf_unavailability`` (which is the 2-path, k=1 case) to
    arbitrary N paths with k-of-n redundancy.  Matches the same beta-factor
    convention:

      U_sys = beta * U_path  +  (1 - beta) * P(< k of n paths up | independent)

    Math
    ----
    Path unavailability splits into two mutually exclusive modes:
      - CCF mode (probability mass beta * U_path):
            a single common-cause event kills all n paths simultaneously.
      - Independent mode (residual probability mass 1 - beta * ...):
            each path fails independently with unavailability U_path; the
            system fails iff fewer than k paths survive (binomial sum).

    The 2-path k=1 case reduces exactly to::
        U_sys = (1 - beta) * U^2 + beta * U
    matching the existing ``ccf_unavailability(u, u, beta)`` formula.

    Topology mapping
    ----------------
      - 2N    (k=1, n=2): tolerate 1 failure of 2 paths
      - 3N/2  (k=2, n=3): tolerate 1 failure of 3 paths
      - 4N/3  (k=3, n=4): tolerate 1 failure of 4 paths
      - 3+1   (k=3, n=4): same math as 4N/3 (operational philosophy differs)

    Parameters
    ----------
    n        : number of redundant paths (>= 1)
    k        : minimum paths required for system to be UP (1 <= k <= n)
    u_path   : per-path unavailability (identical for all n paths)
    beta     : common-cause factor (0 to 1; typical 0.01 - 0.10)

    Returns
    -------
    System unavailability.
    """
    if n <= 0 or k <= 0:
        return 0.0
    if k > n:
        return 1.0
    if n == 1:
        return u_path
    a_path = 1.0 - u_path
    # P(< k of n paths up) under independent failures
    p_indep_fail = 1.0 - kofn_availability(n, k, a_path)
    # CCF mode contributes beta * u_path; independent mode contributes (1-beta) * p_indep_fail
    return beta * u_path + (1.0 - beta) * p_indep_fail


# ---------------------------------------------------------------------------
# Generator / prime-mover mission model (revised: includes FTLR)
# ---------------------------------------------------------------------------

def mission_reliability(lambda_per_hour: float, t_hours: float) -> float:
    """
    Time-based reliability for constant-hazard component:
        R(t) = exp(-lambda * t)
    """
    return math.exp(-lambda_per_hour * t_hours)


def generator_mission_prob(
    fts: float,
    lambda_run: float,
    t_hours: float,
    ftlr: float = 0.0,
) -> float:
    """
    Single-generator mission success for a STANDBY (demand + run) model.

    Revised formula (NRC/INL 2022):
        P = (1 - FTS) * (1 - FTLR) * exp(-lambda_run * t)

    where:
        FTS       = fail-to-start probability per demand
        FTLR      = fail-to-load / early carry-load failure per demand
        lambda_run= run-failure rate after first hour (failures per run-hour)
        t         = mission duration in hours

    For a continuously-running islanded generator the FTS and FTLR terms are
    not applicable to normal operation; use this only for extended-outage
    mission analysis or start-after-maintenance scenarios.
    """
    return (1.0 - fts) * (1.0 - ftlr) * mission_reliability(lambda_run, t_hours)


def kofn_mission_prob(
    n: int,
    k: int,
    fts: float,
    lambda_run: float,
    t_hours: float,
    ftlr: float = 0.0,
) -> float:
    """k-of-n mission success for identical generators (standby model)."""
    p_single = generator_mission_prob(fts, lambda_run, t_hours, ftlr)
    return kofn_availability(n, k, p_single)


# ---------------------------------------------------------------------------
# Mixed-fleet k-of-n (non-identical groups, arbitrary counts)
# ---------------------------------------------------------------------------

def mixed_fleet_kofn_availability(
    groups: List[tuple],   # List of (count: int, availability: float)
    k: int,
) -> float:
    """
    k-of-n availability for a mixed fleet of non-identical generator groups.

    Each group i contributes count_i independent units each with availability a_i.
    The number of working units follows the convolution of Binomial(n_i, a_i) RVs.

    Method: numpy convolution of per-group PMFs — O(n_total^2) but vectorised,
            handles fleets of several hundred units in milliseconds.

    Parameters
    ----------
    groups : list of (count, availability) tuples, one per homogeneous group
    k      : minimum number of units required for the system to work

    Returns
    -------
    P(total working units >= k)
    """
    import numpy as np

    if k <= 0:
        return 1.0
    n_total = sum(int(c) for c, _ in groups)
    if n_total == 0:
        return 0.0
    if k > n_total:
        return 0.0

    # Build the joint PMF iteratively via convolution.
    pmf = np.array([1.0])

    for count, avail in groups:
        count = int(count)
        if count <= 0:
            continue
        avail = float(np.clip(avail, 0.0, 1.0))
        q = 1.0 - avail
        group_pmf = np.array([
            math.comb(count, i) * (avail ** i) * (q ** (count - i))
            for i in range(count + 1)
        ])
        pmf = np.convolve(pmf, group_pmf)

    k = min(k, len(pmf) - 1)
    return float(np.sum(pmf[k:]))


def mixed_fleet_mission_prob_integrated(
    groups: List[tuple],   # List of (count, fts, ftlr, lambda_run)
    k: int,
    grid_mttr_hours: float,
) -> float:
    """
    Mixed-fleet mission success probability INTEGRATED over an exponentially-
    distributed grid outage duration with mean = grid_mttr_hours.

    For exponential T ~ Exp(1/MTTR), the expected per-gen run-success factor is:
        E[ exp(-lambda * T) ] = 1 / (1 + lambda * MTTR)

    The per-demand start/load factors (1 - FTS) and (1 - FTLR) are unchanged
    (they fire once per outage event regardless of how long the outage lasts).

    This is the "Option A" analytical alternative to using a single fixed
    mission duration -- it eliminates the arbitrary mission_duration_hours
    input by integrating over the actual outage-duration distribution.

    Parameters
    ----------
    groups          : list of (count, fts, ftlr, lambda_run_per_hour) per group
    k               : minimum gens required for the system to succeed
    grid_mttr_hours : mean grid outage duration (= grid MTTR)

    Returns
    -------
    Probability that at least k gens complete their mission, averaged over
    the distribution of grid outage durations.
    """
    mission_groups = [
        (count, (1.0 - fts) * (1.0 - ftlr) / (1.0 + lam * grid_mttr_hours))
        for count, fts, ftlr, lam in groups
    ]
    return mixed_fleet_kofn_availability(mission_groups, k)


def mixed_fleet_mission_prob(
    groups: List[tuple],   # List of (count: int, fts: float, ftlr: float, lambda_run: float)
    k: int,
    t_hours: float,
) -> float:
    """
    k-of-n mission success for a mixed generator fleet (demand + run model).

    Revised formula per NRC/INL 2022:
        p_mission_i = (1 - FTS_i) * (1 - FTLR_i) * exp(-lambda_i * t)

    The system succeeds if at least k generators complete the mission.

    Parameters
    ----------
    groups  : list of (count, fts_probability, ftlr_probability, lambda_run_per_hour)
    k       : minimum generators required
    t_hours : mission duration

    Notes
    -----
    Caller must pass 4-tuples. To isolate FTS contribution only, pass ftlr=0.0.
    To isolate run contribution only, pass fts=0.0 and ftlr=0.0.
    """
    mission_groups = [
        (count, (1.0 - fts) * (1.0 - ftlr) * mission_reliability(lam, t_hours))
        for count, fts, ftlr, lam in groups
    ]
    return mixed_fleet_kofn_availability(mission_groups, k)


# ---------------------------------------------------------------------------
# Downtime conversion helpers
# ---------------------------------------------------------------------------

HOURS_PER_YEAR = 8_766.0        # 365.25 * 24
MINUTES_PER_YEAR = 525_960.0    # 365.25 * 24 * 60


def annual_downtime_minutes(availability: float) -> float:
    return (1.0 - availability) * MINUTES_PER_YEAR


def annual_downtime_hours(availability: float) -> float:
    return (1.0 - availability) * HOURS_PER_YEAR


# ---------------------------------------------------------------------------
# Sensitivity / importance helpers
# ---------------------------------------------------------------------------

def delta_availability(
    baseline_avail: float,
    perturbed_avail: float,
) -> float:
    """Signed change: perturbed - baseline."""
    return perturbed_avail - baseline_avail


def availability_to_nines(a: float) -> float:
    """Number of 9s: e.g. 0.9999 -> 4.0."""
    if a <= 0:
        return 0.0
    if a >= 1:
        return float("inf")
    return -math.log10(1.0 - a)
