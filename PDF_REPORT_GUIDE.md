# PDF Report — User Guide

A one-click export that turns the current RAM analysis run into a
self-contained, audit-ready PDF.

## Why it exists

CSV exports are great for re-importing into another tool, but they don't
preserve formatting, charts, or any sense of "this is the final write-up
for a specific configuration." The PDF report fills that gap — you can
email it, archive it on a project drive, or hand it to a peer reviewer
without them needing to open the app.

## What it produces

A single PDF (typically 5–8 pages) with five sections, in order:

| # | Section | What's in it |
|---|---|---|
| 1 | Configuration | Every input that defined the run — topology settings, generator fleet table, electrical-component values with MTBF/MTTR/source/confidence |
| 2 | Results | System availability, per-path breakdown, CCF contribution split (if applicable), generator mission success analysis |
| 3 | Sensitivity | Tornado chart + ranked table showing which components drive the most downtime |
| 4 | Calculation Trace | Step-by-step audit log: which formula was applied at each step, what the inputs were, what the running result became |
| 5 | Limitations | Explicit list of what this model does NOT account for |

The first page has a navy headline block with the six numbers you'd put
on a slide: availability, downtime, nines, mission probability, fleet
composition, topology.

## How to use it

1. Configure the model however you want (set generator fleet, toggle
   components, override MTBFs, set CCF beta, etc.).
2. Let the app recalculate (any change triggers a rerun — wait for the
   results to refresh).
3. Click the **Audit & QA** tab.
4. Scroll to the bottom, find the **Export Audit Package** section.
5. Click **Generate Full PDF Report**. Wait 1–3 seconds for the spinner.
6. Click **Download PDF Report** to save it. The filename includes
   today's date.

## Important: re-generate after changing settings

The PDF is built from a *snapshot* of the result at the moment you
click Generate. If you change a setting and immediately click Download
(without clicking Generate again), you'll get the **old** PDF. The
button shows a "built at HH:MM:SS" timestamp so you can tell.

If in doubt, click Generate again — it takes a second or two.

## What this PDF does NOT do

- **It does not capture every chart in the app.** The waterfall, per-path
  bar charts, scenario comparison, and methodology screen are not in the
  PDF. The PDF focuses on the data you'd need to defend the numbers.
- **It does not run any new calculations.** It serializes the existing
  `SystemResult` object — same numbers you see in the UI, same calc
  trace. If the UI shows a stale result, the PDF will be stale too.
- **It does not model anything beyond electrical reliability.** All
  limitations of the underlying RAM model still apply (see Section 5 of
  the PDF for the full list — maintenance unavailability, cooling, BMS,
  battery wear-out, etc.).
- **It does not include the methodology / formula reference.** Those
  derivations live in the Methodology tab. Open that tab if a reviewer
  asks "where does this formula come from?"

## What's in each PDF section, in detail

**Section 1.1 — Topology Settings:** Number of paths, generator
arrangement (dedicated vs. shared pool), k-of-n, CCF on/off and beta,
mission duration, and the full list of active distribution components.

**Section 1.2 — Generator Fleet:** One row per group. Name, unit count,
continuous MTBF, MTTR, fail-to-start probability, fail-to-load probability,
provenance note. Replace the placeholders with site/OEM data before
relying on the result.

**Section 1.3 — Electrical Component Values:** One row per active
component. Status column tells you whether the value is `[Specified]`
(from a published source like IEEE 493, Eaton, Vertiv, NREL),
`[Placeholder]` (engineering estimate — replace before final design),
or `[USER OVERRIDE]` (you typed in a value).

**Section 1.4 — Quality summary:** One sentence telling you what
fraction of your inputs are from specified sources.

**Section 2.1 — Per-path breakdown:** Generator-fleet availability,
distribution availability, and total path availability for each path.

**Section 2.2 — CCF contribution:** Only shown if CCF is enabled. Splits
the system unavailability between independent dual-path failures and
common-cause failures, with percentage shares. Useful for showing a
reviewer that CCF is — or isn't — dominating your total risk.

**Section 2.3 — Mission Success:** System-level mission probability over
the chosen mission duration (default 96 hr). Broken down into the
start-component (FTS + FTLR) and run-component (lambda·t exponential).

**Section 2.4 — Per-Group Mission Probabilities:** Single-unit success
for each generator group. Useful when comparing tech types in a mixed
fleet.

**Section 3 — Sensitivity:** A horizontal "tornado" chart with the top
~15 contributors, plus the full sensitivity table sorted by impact.
Each row shows the annual downtime that would be eliminated if that
component were perfectly reliable. The longer the bar, the bigger the
lever for improvement.

**Section 4 — Calculation Trace:** Every formula application in order.
For a 2N + CCF system, that's typically 8–12 rows: generator-fleet k-of-n
convolution → series multiply through the distribution chain → final
2N + CCF combination. The Result column shows the running availability
after each step.

**Section 5 — Limitations:** Ten standard items covering everything
outside the model's scope. If a reviewer asks "what about cooling?" or
"what about maintenance crews?" — point them here.

## Limitations of THIS export module (separate from model limits)

- **Fonts:** Uses ReportLab's default Helvetica family. Emojis used in
  the app UI (✓, ⚠, 🔴) are replaced with bracketed equivalents like
  `[OK]`, `[!]`, `[H]` so they don't render as missing-glyph boxes.
- **Chart:** Only the tornado chart is rendered into the PDF. Other
  charts (waterfall, donut, etc.) remain interactive in the app only.
- **Page count:** Hard-coded section breaks (one PageBreak between
  major sections). The trace and component tables can spill onto extra
  pages if you have a large configuration — that's expected.

## Troubleshooting

- **"PDF generation failed: No module named 'reportlab'"** — run
  `pip install reportlab matplotlib` and restart the app.
- **PDF opens but a column looks crushed** — likely a very long source
  string or component name. The wrap should handle it, but if not,
  export the underlying assumptions register CSV and view there.
- **Download button shows the wrong content** — click Generate again
  (the displayed timestamp will update).
- **Build is slow (> 5 seconds)** — likely matplotlib import on first
  call. Subsequent builds in the same session are fast.

## Files involved

- `pdf_report.py` — the report builder module. Self-contained; takes a
  `SystemResult` + `comp_overrides` dict and returns PDF bytes.
- `app.py` — wires the Generate / Download buttons into the bottom of
  the Audit & QA tab.
- `requirements.txt` — `reportlab>=4.0.0` and `matplotlib>=3.8.0`.
