"""
PDF report builder for the RAM analysis tool.

Takes a `SystemResult` (from `models.calculate_system`) plus the user's
component overrides, and produces a multi-section PDF report covering:

  1. Configuration  — exact inputs used (topology, fleet, component values)
  2. Results        — system availability, path breakdown, mission analysis
  3. Sensitivity    — tornado chart + ranked table of contributors
  4. Calculation    — full step-by-step trace from result.calc_trace
  5. Limitations    — what this model does NOT account for

The function is pure: no Streamlit, no disk I/O.  It returns the PDF as
bytes, suitable for `st.download_button(data=...)`.

Limitations of THIS report module (separate from model limitations):
  - Tornado chart uses matplotlib (Agg backend, no display required).
  - Default ReportLab fonts (Helvetica) do NOT contain emoji glyphs;
    `_clean()` strips/replaces emoji-class characters so nothing renders
    as a missing-glyph box.
  - The report shows the configuration at calculation time only.  If the
    user changes a setting after running but before exporting, click
    "Run / refresh" first so the displayed result matches the PDF.
"""

from __future__ import annotations

import io
import html
import datetime
import re
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless backend — no display needed
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, Image, PageBreak,
)

from defaults import COMP_DEFAULTS
from reliability import component_availability


# ---------------------------------------------------------------------------
# Text cleaning + formatting helpers
# ---------------------------------------------------------------------------

# Replace common UI emojis with bracketed text equivalents that Helvetica
# can render.  Everything outside the basic + Latin Unicode blocks is then
# stripped so the default font never shows a missing-glyph box.
_EMOJI_REPLACEMENTS = {
    "✓": "[OK]",
    "✅": "[OK]",
    "⚠": "[!]",
    "⚠️": "[!]",
    "⛔": "[X]",
    "🔴": "[H]",
    "🟠": "[M]",
    "🟡": "[L]",
    "📥": "",
    "📄": "",
    "⚡": "",
    "⏸": "",
    "▶": "",
    "→": "->",
    "←": "<-",
    "–": "-",
    "—": "-",
}

_NON_LATIN_RE = re.compile(r"[^\x00-\x7F -ſ]")


def _clean(s: Optional[str]) -> str:
    """Strip emojis / non-Latin chars so ReportLab default fonts render cleanly."""
    if s is None:
        return ""
    s = str(s)
    for emoji, txt in _EMOJI_REPLACEMENTS.items():
        s = s.replace(emoji, txt)
    return _NON_LATIN_RE.sub("", s).strip()


def _p(text: str, style) -> Paragraph:
    """Cleaned + HTML-escaped Paragraph.  Safe for arbitrary user-data fields."""
    return Paragraph(html.escape(_clean(text)), style)


def _p_markup(text: str, style) -> Paragraph:
    """Cleaned Paragraph that PRESERVES simple ReportLab markup (<b>, <i>, <br/>)."""
    # Escape, then unescape the small whitelist we want to keep.
    cleaned = _clean(text)
    escaped = html.escape(cleaned)
    escaped = (
        escaped
        .replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
        .replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
        .replace("&lt;br/&gt;", "<br/>").replace("&lt;br /&gt;", "<br/>")
    )
    return Paragraph(escaped, style)


def _fmt_avail(a: float) -> str:
    return f"{a * 100:.6f}%"


def _fmt_downtime(minutes: float) -> str:
    if minutes < 1:
        return f"{minutes * 60:.1f} sec/yr"
    if minutes < 120:
        return f"{minutes:.2f} min/yr"
    return f"{minutes / 60:.2f} hr/yr"


def _fmt_nines(n: float) -> str:
    return f"{n:.2f} nines"


def _topology_shorthand(config) -> str:
    """Short topology label: '2N', '3N/2', '4N/3', etc."""
    k = config.paths_required if config.paths_required > 0 else 1
    n = config.num_paths
    if n == 1:
        return "Radial (single path)"
    if n == 2 and k == 1:
        return "2N"
    if n == 2 and k == 2:
        return "Series (2 of 2 required)"
    if k == n - 1:
        return f"{n}N/{k}"
    if k == n:
        return f"{n} paths, all required"
    return f"{k}-of-{n}"


# ---------------------------------------------------------------------------
# Table styles
# ---------------------------------------------------------------------------

NAVY = colors.HexColor("#1f4e79")
NAVY_LIGHT = colors.HexColor("#2e75b6")
GREY_LIGHT = colors.HexColor("#f0f0f0")
GREY_ALT = colors.HexColor("#f7f7f7")


def _kv_style() -> TableStyle:
    """Two-column key/value table — bold label on the left, value on the right."""
    return TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), GREY_LIGHT),
        ("FONTNAME",   (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",   (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("GRID",       (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
    ])


def _header_style() -> TableStyle:
    """Table with a navy header row + alternating row backgrounds."""
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("GRID",       (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY_ALT]),
    ])


# ---------------------------------------------------------------------------
# Page header / footer (called by ReportLab on every page)
# ---------------------------------------------------------------------------

def _draw_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#888888"))
    page_w, _ = letter
    canvas.drawString(0.6 * inch, 0.4 * inch, "RAM Analysis Report")
    canvas.drawRightString(page_w - 0.6 * inch, 0.4 * inch, f"Page {doc.page}")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Active component key resolver (mirrors the logic in app.py)
# ---------------------------------------------------------------------------

def _active_component_keys(config) -> list[str]:
    keys: list[str] = []
    if config.include_paralleling_switchgear:
        keys.append("paralleling_switchgear")
    if config.include_gen_breaker:
        keys.append("gen_breaker")
    if config.include_mv_breaker:
        keys.append("mv_breaker")
    if config.include_mv_bus:
        keys.append("mv_bus_section")
    if config.include_ats:
        keys.append("ats_transfer_switch")
    if config.include_transformer:
        keys.append("transformer")
    if config.include_lv_bus:
        keys.append("lv_bus_section")
    if config.include_lv_breaker:
        keys.append("lv_breaker")
    if config.include_ups:
        keys.append("ups_module")
        if config.include_ups_battery:
            keys.append("ups_battery_string")
        if config.include_ups_sts:
            keys.append("ups_static_switch")
    if config.include_pdu:
        keys.append("pdu_rpp")
    if config.include_rack_pdu:
        keys.append("rack_pdu")
    if config.include_it_psu:
        keys.append("it_psu")
    return keys


# ---------------------------------------------------------------------------
# Tornado chart (matplotlib, returned as a PNG buffer for ReportLab Image)
# ---------------------------------------------------------------------------

def _make_tornado_image(sensitivity: dict, top_n: int = 15) -> Optional[io.BytesIO]:
    if not sensitivity:
        return None

    items = sorted(sensitivity.items(), key=lambda x: abs(x[1]), reverse=True)[:top_n]
    # Reverse so the largest bar is at the top of the chart (barh stacks bottom-up).
    items = list(reversed(items))

    # Clean + truncate long labels so they don't crush the plot area.
    def _label(s: str) -> str:
        s = _clean(s).lstrip()
        if s.startswith("->"):
            s = "  " + s  # preserve sub-item indent visually
        return s if len(s) <= 55 else s[:52] + "..."

    labels = [_label(k) for k, _ in items]
    values = [v for _, v in items]

    height = max(3.5, 0.42 * len(items) + 1.0)
    fig, ax = plt.subplots(figsize=(7.5, height))
    bar_colors = [NAVY.hexval()[2:] for _ in values]  # all navy
    ax.barh(labels, values, color="#1f4e79", edgecolor="white")
    ax.set_xlabel("Annual downtime recovered if perfectly reliable (min/yr)")
    ax.set_title("Sensitivity — Top Contributors to System Downtime", loc="left")
    ax.grid(axis="x", alpha=0.3, linestyle="--", linewidth=0.5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="y", labelsize=8)
    ax.tick_params(axis="x", labelsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_pdf_report(result, comp_overrides: Optional[dict] = None) -> bytes:
    """
    Build a full PDF audit report for the given SystemResult.

    Parameters
    ----------
    result         : SystemResult (from models.calculate_system)
    comp_overrides : Dict[str, Tuple[float, float]] of user-overridden component
                     MTBF/MTTR values.  Defaults to empty.

    Returns
    -------
    PDF bytes — feed directly to `st.download_button(data=...)`.
    """
    if comp_overrides is None:
        comp_overrides = {}

    config = result.config
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Styles ───────────────────────────────────────────────────────────────
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1c", parent=styles["Heading1"],
                        textColor=NAVY, spaceBefore=4, spaceAfter=10,
                        fontSize=18)
    h2 = ParagraphStyle("h2c", parent=styles["Heading2"],
                        textColor=NAVY_LIGHT, spaceBefore=14, spaceAfter=8,
                        fontSize=13)
    h3 = ParagraphStyle("h3c", parent=styles["Heading3"],
                        spaceBefore=10, spaceAfter=4, fontSize=11)
    body = ParagraphStyle("body", parent=styles["BodyText"],
                          fontSize=9, leading=12)
    body_small = ParagraphStyle("body_small", parent=styles["BodyText"],
                                fontSize=8, leading=10)
    caption = ParagraphStyle("caption", parent=styles["Italic"],
                             fontSize=8, leading=11,
                             textColor=colors.HexColor("#666666"),
                             spaceAfter=8)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=4)

    # ── Document scaffolding ─────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.5 * inch, bottomMargin=0.6 * inch,
        title="RAM Analysis Report",
        author="Data Center Electrical RAM Tool",
    )
    story = []

    # =========================================================================
    # TITLE BLOCK + HEADLINE METRICS
    # =========================================================================

    story.append(Paragraph("RAM Analysis Report", h1))
    story.append(Paragraph(
        "Data center electrical reliability — behind-the-meter, islanded system",
        caption,
    ))
    story.append(Paragraph(f"Generated: {now_str}", caption))
    story.append(Spacer(1, 6))

    mode_label = (
        "Islanded (behind-the-meter, gens only)"
        if result.power_source_mode == "islanded"
        else "Grid-connected with backup gens"
    )
    headline_data = [
        ["System Availability",       _fmt_avail(result.system_availability)],
        ["Annual Downtime",           _fmt_downtime(result.annual_downtime_min)],
        ["Reliability (nines)",       _fmt_nines(result.nines)],
        ["Power Source Mode",         mode_label],
        ["Mission Success Probability",
         f"{result.fleet_mission.system_mission * 100:.4f}%  "
         f"(over {result.fleet_mission.duration_hours:.0f} h)"],
        ["Generator Fleet",
         f"{result.fleet_total_units} units, {result.fleet_mission.k_required} required"],
        ["Topology",
         _topology_shorthand(config) + f", {config.gen_arrangement}"],
        ["Common-Cause Failure Applied",
         "Yes" if result.ccf_applied else "No"],
    ]
    if result.power_source_mode == "grid_with_backup":
        headline_data.insert(4, [
            "Grid Reliability",
            f"MTBF={result.grid_mtbf_hours:,.0f} h, "
            f"MTTR={result.grid_mttr_hours:.2f} h "
            f"(A_grid={result.grid_availability * 100:.4f}%)",
        ])
    headline_rows = [[_p(k, body), _p_markup(f"<b>{html.escape(v)}</b>", body)]
                     for k, v in headline_data]
    headline = Table(headline_rows, colWidths=[2.7 * inch, 4.5 * inch])
    headline.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), NAVY),
        ("TEXTCOLOR",  (0, 0), (0, -1), colors.white),
        ("FONTSIZE",   (0, 0), (-1, -1), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("GRID",       (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(headline)
    story.append(Spacer(1, 14))

    # =========================================================================
    # SECTION 1 — CONFIGURATION
    # =========================================================================

    story.append(Paragraph("1. Configuration (Inputs Used)", h2))
    story.append(Paragraph(
        "Exact values that produced this result.  Reproducing every value in "
        "this section reproduces the result exactly.",
        caption,
    ))

    # 1.1 Topology + active components
    story.append(Paragraph("1.1 Topology Settings", h3))

    boolean_fields = [
        ("include_paralleling_switchgear", "Paralleling Switchgear"),
        ("include_gen_breaker",            "Gen Output Breaker"),
        ("include_mv_breaker",             "MV Breaker"),
        ("include_mv_bus",                 "MV Bus"),
        ("include_ats",                    "ATS"),
        ("include_transformer",            "Step-Down Transformer"),
        ("include_lv_bus",                 "LV Bus"),
        ("include_lv_breaker",             "LV Breaker"),
        ("include_ups",
         f"UPS ({config.ups_modules_required}-of-{config.ups_modules_per_path} modules)"),
        ("include_ups_battery",            "UPS Battery String"),
        ("include_ups_sts",                "UPS Static Switch"),
        ("include_pdu",
         f"PDU/RPP ({config.pdus_required}-of-{config.pdus_per_path})"),
        ("include_rack_pdu",               "Rack PDU"),
        ("include_it_psu",                 "IT PSU"),
    ]
    active_labels = [label for attr, label in boolean_fields
                     if getattr(config, attr, False)]

    # Topology shorthand label (2N, 3N/2, 4N/3, etc.)
    k_paths = config.paths_required if config.paths_required > 0 else 1
    if config.num_paths == 1:
        topo_shorthand = "Radial (single path)"
    elif config.num_paths == 2 and k_paths == 1:
        topo_shorthand = "2N (dual-cord)"
    elif config.num_paths == 2 and k_paths == 2:
        topo_shorthand = "Series (both paths required)"
    elif k_paths == config.num_paths - 1:
        topo_shorthand = f"{config.num_paths}N/{k_paths} (tolerates 1 path failure)"
    elif k_paths == config.num_paths:
        topo_shorthand = f"{config.num_paths} paths, all required"
    else:
        topo_shorthand = (
            f"{k_paths}-of-{config.num_paths} "
            f"(tolerates {config.num_paths - k_paths} path failures)"
        )

    topo_rows = [
        ["Power source mode",
         "Islanded" if config.power_source_mode == "islanded"
         else "Grid-connected with backup"],
    ]
    if config.power_source_mode == "grid_with_backup":
        topo_rows.append(["Grid MTBF (MTTF)", f"{config.grid_mtbf_hours:,.0f} hours"])
        topo_rows.append(["Grid MTTR",        f"{config.grid_mttr_hours:.2f} hours"])
        topo_rows.append([
            "Derived grid availability",
            f"{(config.grid_mtbf_hours / (config.grid_mtbf_hours + config.grid_mttr_hours)) * 100:.6f}%",
        ])
    topo_rows.extend([
        ["Path topology",                topo_shorthand],
        ["Number of distribution paths", str(config.num_paths)],
        ["Paths required (k)",           str(k_paths)],
        ["Generator arrangement",        config.gen_arrangement],
        ["Generators required (k)",      str(config.gen_required)],
        ["CCF enabled",                  "Yes" if config.enable_ccf else "No"],
        ["CCF beta factor",              f"{config.ccf_beta:.4f}"],
        ["Mission duration",             f"{config.mission_duration_hours:.0f} hours"
                                          + (" (also used as assumed grid outage duration)"
                                             if config.power_source_mode == "grid_with_backup"
                                             else "")],
    ])
    topo_rows_wrapped = [[_p(k, body), _p(v, body)] for k, v in topo_rows]
    topo_rows_wrapped.append([
        _p("Active components", body),
        _p(", ".join(active_labels) or "(none)", body),
    ])

    topo_tbl = Table(topo_rows_wrapped, colWidths=[2.2 * inch, 5.0 * inch])
    topo_tbl.setStyle(_kv_style())
    story.append(topo_tbl)

    # 1.2 Generator fleet
    story.append(Paragraph("1.2 Generator Fleet", h3))
    gen_header = ["Group Name", "Count", "MTBF (h)", "MTTR (h)", "FTS", "FTLR", "Source"]
    gen_rows: list[list] = [gen_header]
    for g in config.gen_groups:
        gen_rows.append([
            _p(g.name, body),
            str(g.count),
            f"{g.mtbf_hours:,.0f}",
            f"{g.mttr_hours:.1f}",
            f"{g.fts_probability:.5f}",
            f"{g.ftlr_probability:.5f}",
            _p(g.source or "", body_small),
        ])
    gen_tbl = Table(
        gen_rows,
        colWidths=[1.2 * inch, 0.5 * inch, 0.8 * inch, 0.6 * inch,
                   0.7 * inch, 0.7 * inch, 2.7 * inch],
    )
    gen_tbl.setStyle(_header_style())
    story.append(gen_tbl)

    # 1.3 Component values
    story.append(Paragraph("1.3 Electrical Component Values", h3))
    story.append(Paragraph(
        "MTBF, MTTR, and the resulting per-component availability for every "
        "active component.  Items marked <b>[USER]</b> use a value you overrode; "
        "<b>[Placeholder]</b> means the built-in default is an engineering "
        "estimate that should be replaced with OEM or site data.",
        caption,
    ))

    active_keys = _active_component_keys(config)
    comp_header = ["Component", "MTBF (h)", "MTTR (h)", "Avail.",
                   "Status / Source"]
    comp_rows: list[list] = [comp_header]

    n_placeholder = 0
    n_specified = 0
    for key in active_keys:
        defn = COMP_DEFAULTS[key]
        if key in comp_overrides:
            mtbf, mttr = comp_overrides[key]
            status = "[USER OVERRIDE]"
            src = (status + " (original source: " +
                   _clean(defn.source[:80]) + ")")
            n_specified += 1
        else:
            mtbf, mttr = defn.mtbf_hours, defn.mttr_hours
            if defn.is_placeholder:
                tag = "[Placeholder]"
                n_placeholder += 1
            else:
                tag = "[Specified]"
                n_specified += 1
            src = f"{tag} ({defn.confidence}) {_clean(defn.source[:80])}"

        avail = component_availability(mtbf, mttr)
        comp_rows.append([
            _p(defn.display_name, body),
            f"{mtbf:,.0f}",
            f"{mttr:.2f}",
            f"{avail * 100:.6f}%",
            _p(src, body_small),
        ])
    comp_tbl = Table(
        comp_rows,
        colWidths=[1.8 * inch, 0.85 * inch, 0.7 * inch, 0.95 * inch, 2.7 * inch],
    )
    comp_tbl.setStyle(_header_style())
    story.append(comp_tbl)

    # 1.4 Quality summary box
    n_total_comp = max(1, n_placeholder + n_specified)
    pct_specified = n_specified / n_total_comp * 100
    quality_msg = (
        f"<b>Data quality:</b> {n_specified} of {n_total_comp} component "
        f"inputs ({pct_specified:.0f}%) are from specified sources or user "
        f"overrides; {n_placeholder} remain as built-in placeholders."
    )
    story.append(Spacer(1, 6))
    story.append(_p_markup(quality_msg, body))

    story.append(PageBreak())

    # =========================================================================
    # SECTION 2 — RESULTS
    # =========================================================================

    story.append(Paragraph("2. Results", h2))

    # 2.0 Power source breakdown (only in grid mode)
    if result.power_source_mode == "grid_with_backup":
        story.append(Paragraph("2.0 Power Source — Grid + Backup", h3))
        story.append(Paragraph(
            "Source is available when the grid is up <b>OR</b> the grid is down "
            "AND the backup fleet successfully carries the load for the assumed "
            "outage duration ({:.0f} hours). Combined source availability is "
            "higher than either input alone because they are redundant."
            .format(result.fleet_mission.duration_hours),
            caption,
        ))
        psrc_rows = [
            ["Element", "Availability", "Annual downtime equivalent"],
            ["Grid feed (alone)",
             _fmt_avail(result.grid_availability),
             _fmt_downtime(
                 (1.0 - result.grid_availability) * 525_600
             )],
            ["Backup fleet (continuous, alone)",
             _fmt_avail(result.path_results[0].gen_fleet_availability),
             _fmt_downtime(
                 (1.0 - result.path_results[0].gen_fleet_availability) * 525_600
             )],
            ["Backup mission success (over {:.0f}h)".format(
                 result.fleet_mission.duration_hours),
             _fmt_avail(result.fleet_mission.system_mission),
             "n/a (per-outage metric)"],
            ["Combined source (grid + backup)",
             _fmt_avail(result.source_availability),
             _fmt_downtime(
                 (1.0 - result.source_availability) * 525_600
             )],
        ]
        psrc_rows_wrapped = [
            [_p(r[0], body), r[1], r[2]] if i > 0 else r
            for i, r in enumerate(psrc_rows)
        ]
        psrc_tbl = Table(
            psrc_rows_wrapped,
            colWidths=[3.5 * inch, 1.7 * inch, 2.0 * inch],
        )
        psrc_tbl.setStyle(_header_style())
        story.append(psrc_tbl)
        story.append(Spacer(1, 10))

    # 2.1 Path-by-path breakdown
    story.append(Paragraph("2.1 Per-Path Availability Breakdown", h3))
    path_rows: list[list] = [
        ["Path", "Generator Fleet", "Distribution Chain", "Total Path"]
    ]
    for p in result.path_results:
        path_rows.append([
            p.path_name,
            f"{p.gen_fleet_availability * 100:.6f}%",
            f"{p.distribution_availability * 100:.6f}%",
            f"{p.total_availability * 100:.6f}%",
        ])
    path_tbl = Table(
        path_rows,
        colWidths=[1.5 * inch, 1.9 * inch, 1.9 * inch, 1.9 * inch],
    )
    path_tbl.setStyle(_header_style())
    story.append(path_tbl)

    # 2.2 CCF split
    if result.ccf_applied and (result.ccf_unavailability_contribution is not None):
        story.append(Paragraph("2.2 Common-Cause Failure Contribution", h3))
        u_ccf = result.ccf_unavailability_contribution
        u_indep = result.independent_unavailability_contribution or 0.0
        u_total = u_ccf + u_indep
        share_ccf = (u_ccf / u_total * 100) if u_total > 0 else 0.0
        share_indep = (u_indep / u_total * 100) if u_total > 0 else 0.0
        ccf_rows = [
            ["Source of Unavailability", "Unavailability", "Share"],
            ["Independent failures (both paths fail unrelated)",
             f"{u_indep:.4e}", f"{share_indep:.1f}%"],
            [f"CCF (beta = {config.ccf_beta:.4f})",
             f"{u_ccf:.4e}", f"{share_ccf:.1f}%"],
            ["Total system unavailability",
             f"{u_total:.4e}", "100.0%"],
        ]
        ccf_rows_wrapped = [
            [_p(r[0], body) if i > 0 else r[0], r[1], r[2]]
            for i, r in enumerate(ccf_rows)
        ]
        ccf_tbl = Table(
            ccf_rows_wrapped,
            colWidths=[3.6 * inch, 1.8 * inch, 1.0 * inch],
        )
        ccf_tbl.setStyle(_header_style())
        story.append(ccf_tbl)

    # 2.3 Mission analysis
    story.append(Paragraph("2.3 Generator Mission Success", h3))
    fm = result.fleet_mission
    mission_rows = [
        ["System mission success (overall)",
         f"{fm.system_mission * 100:.4f}%"],
        ["  - Start / load only (FTS + FTLR component)",
         f"{fm.system_fts_success * 100:.4f}%"],
        ["  - Run reliability only (perfect start assumed)",
         f"{fm.system_run_success * 100:.4f}%"],
        ["Mission duration",
         f"{fm.duration_hours:.0f} hours"],
        ["Fleet k-of-n",
         f"{fm.k_required} required of {sum(g.count for g in fm.groups)} units"],
    ]
    mission_rows_wrapped = [[_p(k, body), _p(v, body)] for k, v in mission_rows]
    mission_tbl = Table(
        mission_rows_wrapped,
        colWidths=[3.5 * inch, 3.7 * inch],
    )
    mission_tbl.setStyle(_kv_style())
    story.append(mission_tbl)

    # 2.4 Per-group mission probabilities (table — small, useful for audit)
    story.append(Paragraph("2.4 Per-Group Single-Unit Mission Probabilities", h3))
    grp_rows = [["Group", "Single-Unit Mission", "Start Component", "Run Component"]]
    for g, m, sp, rp in zip(fm.groups, fm.group_mission_probs,
                            fm.group_start_probs, fm.group_run_probs):
        grp_rows.append([
            _p(g.name, body),
            f"{m * 100:.4f}%",
            f"{sp * 100:.4f}%",
            f"{rp * 100:.4f}%",
        ])
    grp_tbl = Table(
        grp_rows,
        colWidths=[2.5 * inch, 1.7 * inch, 1.5 * inch, 1.5 * inch],
    )
    grp_tbl.setStyle(_header_style())
    story.append(grp_tbl)

    story.append(PageBreak())

    # =========================================================================
    # SECTION 3 — SENSITIVITY
    # =========================================================================

    story.append(Paragraph("3. Sensitivity Analysis", h2))
    story.append(Paragraph(
        "For each contributor, the chart and table below show how much annual "
        "downtime would be eliminated if that item were made perfectly reliable "
        "(MTBF -> infinity, FTS/FTLR -> 0, etc.).  Larger values = bigger lever "
        "on system availability.",
        caption,
    ))

    if result.sensitivity:
        tornado_buf = _make_tornado_image(result.sensitivity)
        if tornado_buf is not None:
            img = Image(tornado_buf, width=7.0 * inch, height=4.5 * inch,
                        kind="proportional")
            img.hAlign = "CENTER"
            story.append(img)
            story.append(Spacer(1, 8))

        sens_items = sorted(result.sensitivity.items(),
                            key=lambda x: -abs(x[1]))
        sens_rows = [["Contributor", "Downtime Recovered (min/yr)"]]
        for label, delta in sens_items:
            sens_rows.append([_p(label, body), f"{delta:.4f}"])
        sens_tbl = Table(sens_rows, colWidths=[5.0 * inch, 2.2 * inch])
        sens_tbl.setStyle(_header_style())
        story.append(sens_tbl)
    else:
        story.append(Paragraph(
            "(No sensitivity items returned — all active components are at "
            "their availability ceiling for this configuration.)",
            body,
        ))

    story.append(PageBreak())

    # =========================================================================
    # SECTION 4 — CALCULATION TRACE
    # =========================================================================

    story.append(Paragraph("4. Calculation Trace", h2))
    story.append(Paragraph(
        "Step-by-step computation log.  Each row shows the step name, the "
        "method / formula applied, the key inputs at that step, and the "
        "running result.  Use this for peer review of the math.",
        caption,
    ))

    trace_header = ["Step", "Method / Formula", "Key Inputs", "Result"]
    trace_rows: list[list] = [trace_header]
    for step in result.calc_trace:
        inputs_str = "; ".join(f"{k}: {v}"
                               for k, v in step.get("inputs", {}).items())
        method_html = (
            html.escape(_clean(step["method"]))
            + "<br/><i>"
            + html.escape(_clean(step["formula"]))
            + "</i>"
        )
        trace_rows.append([
            _p(step["step"], body),
            Paragraph(method_html, body_small),
            _p(inputs_str[:400], body_small),
            f"{step['result']:.8f}",
        ])
    # Final synthesis row
    trace_rows.append([
        _p_markup("<b>FINAL — System Availability</b>", body),
        Paragraph(
            "Combination of the steps above.<br/><i>see Section 2 for full breakdown</i>",
            body_small,
        ),
        Paragraph(
            f"Annual downtime: {result.annual_downtime_min:.4f} min/yr"
            f"<br/>Reliability: {result.nines:.3f} nines",
            body_small,
        ),
        f"{result.system_availability:.8f}",
    ])
    trace_tbl = Table(
        trace_rows,
        colWidths=[1.4 * inch, 2.3 * inch, 2.5 * inch, 1.0 * inch],
    )
    trace_tbl.setStyle(_header_style())
    story.append(trace_tbl)

    story.append(PageBreak())

    # =========================================================================
    # SECTION 5 — LIMITATIONS
    # =========================================================================

    story.append(Paragraph("5. Limitations & Out-of-Scope Items", h2))
    story.append(Paragraph(
        "What this RAM analysis does NOT account for.  Treat the headline "
        "availability number as an upper bound on electrical-system "
        "availability only — total facility availability will be lower once "
        "the items below are considered.",
        caption,
    ))

    limitations = [
        "Maintenance-induced unavailability is NOT modeled (Tier IV "
        "performance is often consumed here).",
        "Control / BMS / EPMS / fire-interface dependencies are excluded "
        "(this is optimistic).",
        "Battery wear-out and aging are not modeled (constant MTBF is a "
        "screening approximation only).",
        "Utility feeder reliability is excluded -- add as a series element "
        "if utility-dependent.",
        "Cooling system reliability is excluded from this electrical-only "
        "model.",
        "Repair queue effects (multiple simultaneous failures, shared "
        "spares) are not modeled.",
        "Common-cause beta factor is an engineering assumption -- justify "
        "from site-specific dependency analysis (shared controls, fuel, "
        "procedures, maintenance crew).",
        "All generator continuous-run MTBFs are placeholders -- replace "
        "with OEM service data before final design decisions.",
        "Items flagged [Placeholder] in Section 1.3 are engineering "
        "estimates, NOT from published sources.",
        "Demand-failure probabilities (ATS transfer, STS transfer) are "
        "captured only via continuous MTBF; a fully rigorous study would "
        "treat them separately.",
    ]
    for item in limitations:
        story.append(_p_markup(f"&bull;&nbsp; {item}", bullet))

    story.append(Spacer(1, 14))
    story.append(_p_markup(
        "<i>This report was generated automatically from the Streamlit RAM "
        "analysis tool.  For methodology references and formula derivations, "
        "see the Methodology tab in the app.</i>",
        caption,
    ))

    # ── Build ────────────────────────────────────────────────────────────────
    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buf.getvalue()
