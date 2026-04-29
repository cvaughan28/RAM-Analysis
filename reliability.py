"""
Core reliability mathematics for RAM analysis.
All functions are pure — no UI, no I/O.

References:
  - IEEE 493: series/parallel RBD methods for power systems
  - Beta-factor CCF model (IEC 61508 / IEC TR 62380)
  - NREL 2020 EDG study: generator mission modeling
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
    """All-series system: all components must work. A_s = ∏ A_i."""
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
      k <= 0  → always available (1.0)
      k >  n  → never available (0.0)
      n == 1  → equals a
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

    U_sys = (1 - β) · U_A · U_B   +   β · max(U_A, U_B)

    The first term is independent coincident failure; the second is CCF
    affecting both paths simultaneously.
    """
    independent = (1.0 - beta) * unavail_a * unavail_b
    common = beta * max(unavail_a, unavail_b)
    return independent + common


# ---------------------------------------------------------------------------
# Generator / prime-mover mission model
# ---------------------------------------------------------------------------

def mission_reliability(lambda_per_hour: float, t_hours: float) -> float:
    """
    Time-based reliability for constant-hazard component:
        R(t) = exp(-λ · t)
    """
    return math.exp(-lambda_per_hour * t_hours)


def generator_mission_prob(fts: float, lambda_run: float, t_hours: float) -> float:
    """
    Single-generator mission success for a STANDBY (demand + run) model:
        P = (1 − FTS) · R(t)  =  (1 − FTS) · exp(−λ_run · t)

    For a continuously-running islanded generator the FTS term is not
    applicable to normal operation; use this only for extended-outage
    mission analysis or start-after-maintenance scenarios.
    """
    return (1.0 - fts) * mission_reliability(lambda_run, t_hours)


def kofn_mission_prob(n: int, k: int, fts: float, lambda_run: float, t_hours: float) -> float:
    """k-of-n mission success for identical generators (standby model)."""
    p_single = generator_mission_prob(fts, lambda_run, t_hours)
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

    Method: numpy convolution of per-group PMFs — O(n_total²) but vectorised,
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
    # pmf[j] = P(exactly j units working across all groups processed so far).
    pmf = np.array([1.0])

    for count, avail in groups:
        count = int(count)
        if count <= 0:
            continue
        avail = float(np.clip(avail, 0.0, 1.0))
        q = 1.0 - avail
        # Binomial PMF for this group
        group_pmf = np.array([
            math.comb(count, i) * (avail ** i) * (q ** (count - i))
            for i in range(count + 1)
        ])
        pmf = np.convolve(pmf, group_pmf)

    # P(system works) = P(working units >= k)
    k = min(k, len(pmf) - 1)
    return float(np.sum(pmf[k:]))


def mixed_fleet_mission_prob(
    groups: List[tuple],   # List of (count: int, fts: float, lambda_run: float)
    k: int,
    t_hours: float,
) -> float:
    """
    k-of-n mission success for a mixed generator fleet (demand + run model).

    For each generator in group i:
        p_mission_i = (1 − FTS_i) × exp(−λ_i × t)

    The system succeeds if at least k generators complete the mission.

    Parameters
    ----------
    groups  : list of (count, fts_probability, lambda_run_per_hour)
    k       : minimum generators required
    t_hours : mission duration
    """
    mission_groups = [
        (count, (1.0 - fts) * mission_reliability(lam, t_hours))
        for count, fts, lam in groups
    ]
    return mixed_fleet_kofn_availability(mission_groups, k)


# ---------------------------------------------------------------------------
# Downtime conversion helpers
# ---------------------------------------------------------------------------

HOURS_PER_YEAR = 8_766.0        # 365.25 × 24
MINUTES_PER_YEAR = 525_960.0    # 365.25 × 24 × 60


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
    """Signed change: perturbed − baseline."""
    return perturbed_avail - baseline_avail


def availability_to_nines(a: float) -> float:
    """Number of 9s: e.g. 0.9999 → 4.0."""
    if a <= 0:
        return 0.0
    if a >= 1:
        return float("inf")
    return -math.log10(1.0 - a)
