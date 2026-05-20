# Grid-Connected Mode — Modeling Guide

This guide covers the **grid-connected with backup generators** power-source
mode added to the RAM tool — what it models, the math it uses, the default
values it ships with, and (importantly) what it does NOT capture.

## When to use grid-connected mode

Use it when the data center's primary source of power is the utility grid
and the on-site generators are **standby** (only run during grid outages).
This is the standard configuration for most enterprise and colocation
data centers.

Use **islanded mode** (the default) when the site has no utility
connection or is intended to be fully self-supplied (e.g., behind-the-meter
hyperscale sites with on-site CHP or fuel cells running continuously).

## What the user provides

Two numbers, in the sidebar's *Power Source* section:

| Input | Default | Meaning |
|---|---|---|
| Grid MTBF / MTTF (hours) | 8,760 | Mean time between grid outages |
| Grid MTTR (hours)        | 2     | Mean restoration time per outage |

Both are entered as hours so they're consistent with every other reliability
input in the tool. The sidebar shows the derived `A_grid` so you can see at
a glance whether your inputs imply 4 nines, 3 nines, etc.

## The math (one-paragraph version)

The grid is treated as a single component with availability
`A_grid = MTTF / (MTTF + MTTR)`. The combined upstream source feeding the
distribution chain is:

```
A_source = A_grid + (1 - A_grid) × P_backup_mission
```

In plain English: the source is up when the grid is up **or** when the grid
is down and the backup fleet successfully carries the load for the assumed
outage duration. `P_backup_mission` reuses the existing k-of-n mission
probability calculation evaluated at the user's `mission_duration_hours`
setting (which is interpreted as "assumed worst-case grid outage length"
in grid mode).

Once `A_source` is computed, the rest of the system calculation is the
same as in islanded mode — `A_source` slots into the position previously
occupied by `A_fleet`, and CCF / k-of-n / 2N path-level math is unchanged.

## Default value rationale (and how to replace them)

| Value | Source | When to override |
|---|---|---|
| MTBF = 8,760 h (1 outage/yr) | Engineering screening value. US urban grid SAIFI ≈ 1.3/yr (IEEE 1366 nationwide) → MTBF ≈ 6,700 h; well-served metro feeders are routinely 8,000–10,000 h. | Replace with utility-supplied SAIFI for your service territory. For sites with dual feeds from separate substations the effective MTBF can be 20,000+ h. |
| MTTR = 2 h | IEEE 1366 CAIDI (excluding major events) is ~135 min nationwide; urban averages around 2 h. | Rural service territory: 4–8 h. Sites in regions with frequent major weather events: budget 12–24 h. |

In any final design study you should pull the utility's own SAIDI/SAIFI/
CAIDI report and convert: `MTBF (hours) = 8760 / SAIFI` and
`MTTR (hours) = CAIDI / 60`.

## Reading the results

A new **Power Source** subsection appears at the top of the Results tab
when grid mode is on. It shows four numbers:

1. **Grid Availability** — your `A_grid` directly from MTBF/MTTR.
2. **Grid Annual Outage** — converted to min/yr or hr/yr for intuition.
3. **Backup Fleet (alone)** — what continuous availability the backup
   fleet would give if it were the only source (same number you'd see in
   islanded mode).
4. **Combined Source** — `A_source`, the value that feeds the
   distribution chain in the system calculation.

Combined source will always be **better** than either input alone — that's
the whole point of redundancy. If yours isn't, something's misconfigured.

The PDF report grows a new Section 2.0 covering the same breakdown plus
an annual-downtime-equivalent column for each.

## Sensitivity in grid mode

The sensitivity panel adds a "Grid feed alone" row alongside the existing
"Source (Grid + Backup) — any one perfectly reliable" row. In the
simple OR-redundancy model these two carry the **same** numeric delta —
that's a mathematical property of the model (making either input perfectly
reliable yields a perfect combined source). They're reported separately so
you can see that the grid feed is participating in the redundancy.

The per-group generator sensitivity rows still appear and are approximate
in grid mode — they apply the "what if this group were perfect" proxy via
the group's continuous availability, not via its mission probability.
Sufficient for screening; for a final design, sweep `gen_group_mtbf_i`
explicitly with the Sensitivity tab's sweep tool.

## What this mode does NOT model

These limitations are **on top of** the model-wide limitations already
listed in Section 5 of the PDF report.

1. **No CCF between grid and backup.** Real common-cause events (regional
   storms damaging both substation and fuel deliveries; cyber events
   affecting both grid SCADA and gen-set controls) are not captured.
   For high-impact sites, run a "what-if" by knocking down both
   `grid_mtbf_hours` and the gen MTBFs together.
2. **Outage-duration distribution is collapsed to a single value.** The
   model assumes every grid outage is exactly `mission_duration_hours`
   long. In reality a few short outages contribute differently to backup
   stress than a single multi-day outage of the same total duration.
3. **Demand-failure correlation is not modeled.** If a single switching
   event triggers both grid loss AND a gen FTS, that correlation is
   ignored — FTS is treated as independent of the grid event.
4. **Grid quality (voltage sags, harmonics) is excluded.** Only hard
   outages count. Sites that experience frequent sub-cycle dips that
   trigger UPS-only operation are not captured by this model.
5. **The grid is treated as a single source.** If you have dual independent
   utility feeds, you'd need to model that as two-grids-in-parallel
   externally and feed the resulting effective MTBF/MTTR into the tool's
   single grid input. (Future enhancement: support n-grid feeds natively.)
6. **No partial outages.** A brown-out, reduced-capacity feed, or
   curtailment-event reduction is modeled as fully up or fully down only.

## How the grid mode affects the calculation trace

In grid mode you'll see two extra rows in the Audit tab's Calculation Trace
and in the PDF's Section 4:

| Step | Formula | Inputs |
|---|---|---|
| Grid Connection                  | `A_grid = MTBF / (MTBF + MTTR)`           | Grid MTBF, Grid MTTR |
| Combined Source (Grid + Backup)  | `A_source = A_grid + U_grid × P_mission`  | A_grid, U_grid, P_backup_mission, assumed outage duration |

Everything else in the trace is unchanged — the distribution chain and
system-level (2N + CCF or single-path or shared-pool) math operates on
`A_source` instead of `A_fleet`.

## Quick sanity checks

If you flip from islanded to grid_with_backup and see availability go DOWN,
something's off — adding a more reliable source on top of the backup gens
should only ever help. Likely causes:
- Grid MTBF/MTTR inputs make the grid less reliable than `(1 - U_grid) × P_mission`
  — e.g., grid is 95% available and backups carry every outage at >95% mission
  success.
- Mission duration is much longer than the actual MTTR you entered (so the
  backup mission probability is artificially low).

If your combined source > 99.999% but system availability is much lower,
the bottleneck has moved to the distribution chain. Check the sensitivity
tornado for which dist component is now dominant.

## Files involved

- `models.py` — `TopologyConfig` (new fields), `SystemResult` (new fields),
  `PathResult` (new fields), `calculate_system` (new "Power source" step).
- `app.py` — sidebar "Power Source" section, Results tab grid breakdown.
- `pdf_report.py` — headline grid-reliability row, Section 1.1 grid inputs,
  Section 2.0 power-source breakdown table.
- `PDF_REPORT_GUIDE.md` — overall PDF feature doc (separate).
