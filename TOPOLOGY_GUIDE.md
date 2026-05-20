# Topology Modeling Guide

Maps the redundancy patterns shown in the B&M SLD set (2N · 3N/2 · 4N/3 ·
3+1 · Concurrently Maintainable · Fault Tolerant) onto the RAM tool's
two inputs: `num_paths` (N) and `paths_required` (k).

## The two-knob model

Everything below the source layer collapses to two settings in the sidebar:

| Setting | Meaning |
|---|---|
| `num_paths` (N) | Total redundant power paths from source to load |
| `paths_required` (k) | Minimum number of paths that must be UP for the system to be UP |

The system unavailability formula generalises the existing 2-path beta-factor
CCF model to arbitrary N:

```
U_sys = β · U_path  +  (1 - β) · P( < k of n paths up | independent failures )
```

For n=2, k=1 this reduces *exactly* to the previous 2N formula —
`(1-β)·U² + β·U` — so old saved 2N numbers are unchanged.

## Topology -> (N, k) cheat sheet

| Topology | N | k | Failures tolerated |
|---|---|---|---|
| Radial (Tier I/II reference) | 1 | 1 | 0 |
| **2N** (Tier IV dual-cord, page 1) | 2 | 1 | 1 |
| **3N/2** (page 2) | 3 | 2 | 1 |
| **4N/3** (page 3) | 4 | 3 | 1 |
| **3+1** distributed (page 4) | 4 | 3 | 1 (same math as 4N/3) |
| Higher-N distributed | 5, 6 | 4, 5 | 1 |

The "tolerate exactly 1 failure" pattern (k = N-1) is by far the most common
distributed-redundancy choice in data centers. Lower k (e.g., 2-of-4) means
tolerating multiple simultaneous failures — only relevant for very large N.

## Why 4N/3 isn't dramatically worse than 2N in our numbers

Intuitively you'd expect 4N/3 to be much less reliable than 2N because there
are C(4,2)=6 path-pair-failure modes versus C(2,2)=1 in 2N. But at the
default β=0.02, the binomial term is dominated by the CCF term:

- 2N:   U_sys ≈ β·U_path = 0.02·U_path     (independent contribution ~ U_path² is negligible)
- 4N/3: U_sys ≈ β·U_path + 6·(1-β)·U_path²

For a typical per-path U ≈ 10⁻⁵, the binomial term contributes only ~6·10⁻¹⁰
while the CCF term contributes ~2·10⁻⁷. So the four topologies all live in
the same "5-nines neighbourhood" when CCF is the dominant failure mode.

**What this tells the engineering team:** at high-availability designs,
adding more paths buys you very little if β doesn't go down with it. Real
3N/2 and 4N/3 sites should have *lower* β because of more diverse routing,
diverse fuel, and crew procedures — but the model uses the same β for any
N, so you have to set it yourself. Drop the β slider to ~0.005 when
modelling a well-engineered 4N/3 design and the math will reward you for it.

## Concurrently Maintainable (Tier III, page 9)

This is **not** a path-redundancy story — it's a maintenance-state story.
The model represents it as:

- `num_paths = 1`     (the critical load is radial through one UPS)
- `paths_required = 1`
- `power_source_mode = "grid_with_backup"` with 2 backup gens (N+1)

The model captures the reliability of the radial critical path correctly,
including the value of having N+1 gens upstream. **What it does NOT
capture:** the operational benefit of being able to maintain any one
component without dropping the load, because the model has no test/
maintenance-state representation (this limitation is also listed in the
PDF's Section 5).

So the headline number for Concurrently Maintainable looks worse than 2N
(more downtime, fewer nines) because the radial critical path is the
bottleneck. That's directionally correct — Tier III has lower steady-state
availability than Tier IV — but it understates the operational value of
having every component maintainable.

## Fault Tolerant Minimum (Tier IV, page 10)

`num_paths = 2`, `paths_required = 1`, `power_source_mode = grid_with_backup`,
2 backup gens (N+1). This is essentially the same as the "Tier IV — 2N
Dual Path with UPS" preset but with a single utility feed and a backup
fleet, matching the SLD on page 10.

## Numbers from the presets (β tuned per topology — see next section)

| Preset | β | Availability | Annual Downtime |
|---|---|---|---|
| 2N (default, islanded) | 0.020 | 99.999827% | 0.91 min/yr |
| **3N/2** (grid + backup) | **0.015** | 99.999678% | 1.69 min/yr |
| **4N/3** (grid + backup) | **0.010** | 99.999768% | 1.22 min/yr |
| **3+1** (grid + backup) | **0.010** | 99.999768% | 1.22 min/yr (identical to 4N/3 by math) |
| **CM (Tier III)** | n/a (n=1) | 99.979952% | 105 min/yr |
| **FT (Tier IV)** | 0.020 | 99.999595% | 2.13 min/yr |
| N+1 Shared Pool | **0.030** | 99.982340% | 93 min/yr |

Note that 4N/3 (β=0.010) now beats 3N/2 (β=0.015) — exactly as you'd expect
when diversity savings are properly credited. Without the β tuning, 4N/3
looked SLIGHTLY worse than 3N/2 because of the larger independent-failure
binomial; with β tuning it's the other way around.

## CCF beta defaults per topology

The tool sets β per-preset to reflect the typical diversity of each design.
Engineering intuition: β represents "fraction of failures caused by events
that affect every redundant path simultaneously." More diverse routing,
controls, fuel, and crews → lower β.

| Topology | β default | Rationale |
|---|---|---|
| Tier IV 2N (single utility, shared site) | 0.020 | Standard B&M baseline. Shared site events (fire, flood), shared controls (BMS / EPMS), shared maintenance procedures are the typical β-contributors. |
| Tier IV Fault Tolerant Minimum (page 10) | 0.020 | Same as 2N. Dual UPS / dual LV swbd downstream, but single utility → no additional diversity. |
| Tier IV 3N/2 (page 2) | **0.015** | Three independent chains typically implies investment in some routing / control diversity that 2N doesn't have. |
| Tier IV 4N/3 (page 3) | **0.010** | Four chains imply substantial diversity — different substations, different fuel paths, different fire zones often achievable. |
| Tier IV 3+1 (page 4) | **0.010** | Same physical diversity as 4N/3; only the operational philosophy differs. |
| Tier IV N+1 Shared Pool | **0.030** | Shared gen pool = explicit coupling. Higher β to reflect the design choice to share equipment between paths. |
| Tier III Concurrently Maintainable (page 9) | n/a | Single critical path (n=1); CCF doesn't apply at the path layer. |
| Tier III Radial reference | n/a | Same reason. |

**These defaults are starting points for screening, not validated values.**
For a final RAM study you should justify β from a site-specific
dependency analysis: list each shared resource (fuel, fire zone, control
system, maintenance crew), estimate the failure-rate contribution from
common causes for each, and sum to derive β. IEEE 61508 and IEC TR 62380
both have worked examples.

## CCF beta ladder in the sensitivity panel

Instead of a single "what if β were 0" entry, the sensitivity panel now
shows three rungs:

1. **β halved**  (your current β × 0.5) — realistic short-term improvement
2. **β quartered**  (× 0.25) — aggressive design diversification
3. **β = 0**  (perfect independence) — theoretical ceiling

These appear as three rows in the tornado chart, so you can see the
diminishing-returns curve directly. For a default 2N at β=0.02:

```
CCF beta = 0           (perfect independence)  ->  0.91 min/yr recovered
CCF beta quartered     (β = 0.005)             ->  0.68 min/yr recovered
CCF beta halved        (β = 0.010)             ->  0.45 min/yr recovered
```

Reading this: cutting β by 50% recovers half the available CCF-driven
downtime; cutting by 75% recovers ~75%. The full theoretical recovery (β=0)
is rarely achievable in a single physical site — site events you can't
control put a floor on β even with maximum design diversification.

The new presets are pre-configured with `power_source_mode = grid_with_backup`
because that matches the SLDs (each chain has a utility feed AND a backup
gen). If you want to compare them on an islanded basis, switch the Power
Source radio in the sidebar back to "Islanded" — the math automatically
falls back to fleet-only source.

## What this model still does NOT capture

These limitations are in addition to the model-wide ones listed in PDF
Section 5.

1. **Single β across all path counts.** Real 4N/3 designs may have lower β
   than 2N because of diverse routing; the model uses your single β value
   for every N. Adjust it manually when comparing designs.
2. **No per-load-group reliability.** Distributed designs (3N/2, 4N/3) put
   loads on specific path-pairs. The model treats this as "any k of n must
   be up" which is the correct system-wide RAM math, but doesn't distinguish
   between "load group AB lost a cord" vs "load group CD lost a cord."
3. **Same per-path component values across all paths.** All N paths use
   the same MTBF/MTTR/UPS-arrangement. Mixed designs (e.g., one path with
   N+1 UPS and another with N+2) aren't representable.
4. **Source assumed shared at the same beta.** In grid_with_backup mode,
   the grid is treated as shared upstream of all paths; if you have N
   independent utility feeds you should reduce β to reflect that diversity.
5. **No maintenance-state model.** Tier III's "concurrently maintainable"
   property — the value of being able to swap any single component without
   dropping load — is not in the math. Headline availability is the same
   as a radial design with N+1 gens.

## How to use the presets

In the main window (not the sidebar), under "Topology Compare" tab, expand
the "Load a preset topology template" panel. Select one of the ten presets
and click "Calculate preset and save to comparison list." This drops the
preset's result into the scenario comparison table so you can stack
multiple topologies side-by-side.

To customize a preset further: load it once, then tweak any sidebar value
to see the new result. The sidebar's "N" selector lets you pick any path
count from 1 to 6, and the "Paths required (k)" input appears below to
let you set k directly.

## Files involved

- `reliability.py` — new `kofn_with_ccf(n, k, u_path, beta)` helper.
- `models.py` — `TopologyConfig.paths_required` field; `calculate_system`
  uses `kofn_with_ccf` uniformly for N ≥ 2.
- `app.py` — sidebar `num_paths` widget allows 1-6; new `paths_required`
  input; five new entries in `TOPOLOGY_PRESETS`.
- `pdf_report.py` — Section 1.1 shows the topology shorthand (e.g. "3N/2")
  and explicitly the k value; headline metrics row updated.
- `PDF_REPORT_GUIDE.md`, `GRID_MODE_GUIDE.md` — companion docs (separate).
