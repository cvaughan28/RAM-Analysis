"""
Simplified single-line diagram (SLD) builder.

Renders the current TopologyConfig with standard IEEE-style symbols
(generator-circle, transformer two-coil, breaker square, bus bar, UPS,
battery, PDU, load) instead of the graphviz labeled-box RBD format.

Output is a matplotlib Figure suitable for `st.pyplot()`.

This is intentionally a "simplified" SLD -- not CAD-grade. Symbol set is
small enough to render automatically without hand-tuning, and the layout
is one vertical chain per power path.
"""

from __future__ import annotations

import io
import matplotlib
matplotlib.use("Agg")  # headless backend
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle, Polygon, FancyBboxPatch
from matplotlib.lines import Line2D

from defaults import COMP_DEFAULTS
from reliability import component_availability, kofn_availability


# ─── Palette (matches diagrams.py RBD colors) ──────────────────────────────

COLOR_SPECIFIED   = "#28a745"
COLOR_PLACEHOLDER = "#f0ad4e"
COLOR_OVERRIDE    = "#0d6efd"
COLOR_BOTTLENECK  = "#dc3545"
COLOR_LINE        = "#1a1a1a"
COLOR_BUS         = "#1a1a1a"

LINE_W   = 1.4   # ordinary wire
BUS_W    = 3.5   # bus bar
SYM_LW   = 1.6   # symbol outline


# ─── Symbol drawing primitives ─────────────────────────────────────────────

def _draw_utility(ax, x, y, label="UTILITY GRID"):
    """Lightning-bolt-style utility input symbol."""
    # Stylized bolt drawn from polygon
    bolt = Polygon(
        [(x - 0.1, y + 0.35), (x + 0.05, y + 0.05),
         (x - 0.05, y + 0.05), (x + 0.1, y - 0.35),
         (x + 0.0, y - 0.05), (x + 0.07, y - 0.05)],
        closed=False, fill=True, facecolor="#f0c419",
        edgecolor=COLOR_LINE, linewidth=1.2,
    )
    ax.add_patch(bolt)
    ax.text(x, y + 0.55, label, ha="center", va="bottom",
            fontsize=9, fontweight="bold")


def _draw_generator(ax, x, y, count=1, radius=0.22, color=COLOR_LINE):
    """Generator: circle with G; count annotation if cluster."""
    c = Circle((x, y), radius, fill=False, linewidth=SYM_LW, edgecolor=color)
    ax.add_patch(c)
    ax.text(x, y, "G", ha="center", va="center",
            fontsize=11, fontweight="bold", color=color)
    if count > 1:
        ax.text(x + radius + 0.07, y, f"×{count}",
                ha="left", va="center", fontsize=10,
                fontweight="bold", color="#555")


def _draw_transformer(ax, x, y, color=COLOR_LINE):
    """Two-winding transformer: two overlapping circles."""
    r = 0.13
    upper = Circle((x, y + 0.08), r, fill=False, linewidth=SYM_LW, edgecolor=color)
    lower = Circle((x, y - 0.08), r, fill=False, linewidth=SYM_LW, edgecolor=color)
    ax.add_patch(upper)
    ax.add_patch(lower)


def _draw_breaker(ax, x, y, size=0.18, color=COLOR_LINE):
    """Circuit breaker: small square."""
    rect = Rectangle((x - size / 2, y - size / 2), size, size,
                     fill=False, linewidth=SYM_LW, edgecolor=color)
    ax.add_patch(rect)


def _draw_ats(ax, x, y, color=COLOR_LINE):
    """ATS: rectangle with a slash inside (transfer-switch convention)."""
    w, h = 0.28, 0.32
    rect = Rectangle((x - w / 2, y - h / 2), w, h,
                     fill=False, linewidth=SYM_LW, edgecolor=color)
    ax.add_patch(rect)
    # Slash to indicate transfer
    ax.plot([x - w / 2 + 0.04, x + w / 2 - 0.04],
            [y - h / 2 + 0.04, y + h / 2 - 0.04],
            linewidth=SYM_LW, color=color)


def _draw_bus(ax, x_start, x_end, y, color=COLOR_BUS):
    """Bus bar: thick horizontal line."""
    ax.plot([x_start, x_end], [y, y],
            linewidth=BUS_W, color=color, solid_capstyle="butt")


def _draw_wire(ax, x1, y1, x2, y2, color=COLOR_LINE):
    """Single-line wire (any orientation)."""
    ax.plot([x1, x2], [y1, y2], linewidth=LINE_W, color=color)


def _draw_ups(ax, x, y, w=0.7, h=0.4, color=COLOR_LINE,
              modules_req=None, modules_inst=None):
    """UPS: labeled rectangle. k-of-n caption below."""
    rect = FancyBboxPatch(
        (x - w / 2, y - h / 2), w, h,
        boxstyle="round,pad=0.02",
        fill=False, linewidth=SYM_LW, edgecolor=color,
    )
    ax.add_patch(rect)
    ax.text(x, y, "UPS", ha="center", va="center",
            fontsize=10, fontweight="bold", color=color)
    if modules_req is not None and modules_inst is not None:
        ax.text(x, y - h / 2 - 0.15,
                f"{modules_req}-of-{modules_inst}",
                ha="center", va="top", fontsize=7, color="#555")


def _draw_battery(ax, x, y, color=COLOR_LINE):
    """Battery: 3-cell alternating long/short pattern."""
    cell_h = 0.07
    for i in range(3):
        y0 = y - 0.12 + i * cell_h * 1.6
        # Long line (positive terminal)
        ax.plot([x - 0.18, x + 0.18], [y0, y0],
                linewidth=2.2, color=color, solid_capstyle="butt")
        # Short line (negative terminal)
        ax.plot([x - 0.10, x + 0.10], [y0 + cell_h * 0.7, y0 + cell_h * 0.7],
                linewidth=1.2, color=color, solid_capstyle="butt")


def _draw_pdu(ax, x, y, w=0.6, h=0.32, color=COLOR_LINE,
              pdus_req=None, pdus_inst=None):
    """PDU: labeled rectangle. k-of-n caption below."""
    rect = FancyBboxPatch(
        (x - w / 2, y - h / 2), w, h,
        boxstyle="round,pad=0.02",
        fill=False, linewidth=SYM_LW, edgecolor=color,
    )
    ax.add_patch(rect)
    ax.text(x, y, "PDU", ha="center", va="center",
            fontsize=9, fontweight="bold", color=color)
    if pdus_req is not None and pdus_inst is not None and pdus_inst > 1:
        ax.text(x, y - h / 2 - 0.13,
                f"{pdus_req}-of-{pdus_inst}",
                ha="center", va="top", fontsize=7, color="#555")


def _draw_load(ax, x, y, label="LOAD", subtitle=None):
    """Load: down-pointing triangle."""
    tri = Polygon(
        [(x - 0.35, y + 0.25), (x + 0.35, y + 0.25), (x, y - 0.35)],
        fill=False, linewidth=SYM_LW + 0.4, edgecolor=COLOR_LINE,
    )
    ax.add_patch(tri)
    ax.text(x, y - 0.55, label, ha="center", va="top",
            fontsize=10, fontweight="bold")
    if subtitle:
        ax.text(x, y - 0.75, subtitle, ha="center", va="top",
                fontsize=8, color="#555")


# ─── Annotation formatting ─────────────────────────────────────────────────

def _format_annot(avail: float, mode: str) -> str:
    if mode == "availability":
        return f"A={avail * 100:.4f}%"
    elif mode == "downtime":
        dt = (1.0 - avail) * 525_960
        if dt < 1:
            return f"DT={dt * 60:.1f} s/yr"
        elif dt < 120:
            return f"DT={dt:.2f} min/yr"
        elif dt < 24 * 60:
            return f"DT={dt / 60:.2f} hr/yr"
        return f"DT={dt / (24 * 60):.2f} d/yr"
    else:
        return f"U={(1.0 - avail) * 1e6:.2f} ppm"


def _annotate_block(ax, x, y, text, color="#333", dx=0.50, fontsize=7):
    """Small reliability number label next to a symbol."""
    ax.text(x + dx, y, text, ha="left", va="center",
            fontsize=fontsize, color=color, family="monospace")


# ─── Component color resolver ──────────────────────────────────────────────

def _comp_color(comp_key: str, comp_overrides: dict,
                bottleneck_match: bool = False) -> str:
    if bottleneck_match:
        return COLOR_BOTTLENECK
    defn = COMP_DEFAULTS[comp_key]
    if comp_key in comp_overrides:
        return COLOR_OVERRIDE
    return COLOR_PLACEHOLDER if defn.is_placeholder else COLOR_SPECIFIED


def _find_bottleneck_label(sensitivity: dict):
    """Identify the top distribution-component contributor (skip beta/source rows)."""
    candidates = []
    for label, delta in sensitivity.items():
        lower = label.lower()
        if any(kw in lower for kw in ("ccf beta", "source ", "  -> ", "grid feed")):
            continue
        candidates.append((label, abs(delta)))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


# ─── Main entry point ──────────────────────────────────────────────────────

def build_sld(result, comp_overrides: dict = None,
              annotation: str = "availability",
              highlight_bottleneck: bool = True):
    """Build the SLD figure for the current TopologyConfig.

    Returns a matplotlib Figure object. Caller is responsible for
    `plt.close(fig)` after use to avoid memory accumulation.
    """
    if comp_overrides is None:
        comp_overrides = {}
    config = result.config
    n_paths = config.num_paths

    bottleneck = (_find_bottleneck_label(result.sensitivity)
                  if highlight_bottleneck else None)

    def _is_bottleneck(comp_key: str) -> bool:
        if bottleneck is None:
            return False
        defn = COMP_DEFAULTS[comp_key]
        bn = bottleneck.lower()
        # Match by display_name or by k-of-n group name
        if defn.display_name.lower() in bn or bn in defn.display_name.lower():
            return True
        if comp_key == "ups_module" and "ups system" in bn:
            return True
        if comp_key == "pdu_rpp" and "pdu" in bn:
            return True
        return False

    # ── Column positions ─────────────────────────────────────────────────
    path_spacing = 3.5
    path_x = [(i - (n_paths - 1) / 2.0) * path_spacing for i in range(n_paths)]

    # ── Y-coordinate ladder (top -> bottom) ───────────────────────────────
    y_utility   = 14.5
    y_top_bus   = 13.2
    y_ats       = 12.0
    y_gen       = 10.5
    y_gen_brk   = 9.4
    y_xfmr      = 8.3
    y_mv_bus    = 7.2
    y_lv_brk    = 6.3
    y_lv_bus    = 5.4
    y_ups       = 4.3
    y_batt      = 3.2
    y_pdu       = 2.2
    y_bot_bus   = 1.0
    y_load      = -0.2

    # ── Figure size ──────────────────────────────────────────────────────
    fig_w = max(11.0, n_paths * 3.5 + 2.5)
    fig_h = 13.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # ── 1. Utility input + top bus ───────────────────────────────────────
    if config.power_source_mode == "grid_with_backup":
        _draw_utility(ax, 0, y_utility, "UTILITY GRID")
        _draw_wire(ax, 0, y_utility - 0.4, 0, y_top_bus)
        # Grid annotation
        ax.text(0.5, y_utility, _format_annot(result.grid_availability, annotation),
                ha="left", va="center", fontsize=8,
                color="#555", family="monospace")
    else:
        # Islanded: skip utility row, top bus is fed by gens only (no upstream)
        ax.text(0, y_utility, "ISLANDED — no utility connection",
                ha="center", va="center", fontsize=9, style="italic", color="#888")

    # Top bus extent
    if n_paths > 1:
        bus_left  = path_x[0] - 0.8
        bus_right = path_x[-1] + 0.8
    else:
        bus_left, bus_right = -0.7, 0.7
    _draw_bus(ax, bus_left, bus_right, y_top_bus)

    # ── 2. Per-path chain ────────────────────────────────────────────────
    for i, x in enumerate(path_x):
        # Path label at the very top
        ax.text(x, y_top_bus + 0.35, f"Path {chr(ord('A') + i)}",
                ha="center", va="bottom", fontsize=9, fontweight="bold",
                color="#444")

        # Top bus → ATS
        _draw_wire(ax, x, y_top_bus, x, y_ats + 0.18)

        # ATS (if enabled)
        if config.include_ats:
            color = _comp_color("ats_transfer_switch", comp_overrides,
                                _is_bottleneck("ats_transfer_switch"))
            _draw_ats(ax, x, y_ats, color=color)
            ax.text(x + 0.22, y_ats, "ATS", ha="left", va="center",
                    fontsize=8, color=color)
            # Reliability annotation
            defn = COMP_DEFAULTS["ats_transfer_switch"]
            mtbf, mttr = comp_overrides.get(
                "ats_transfer_switch", (defn.mtbf_hours, defn.mttr_hours))
            _annotate_block(ax, x, y_ats - 0.30,
                            _format_annot(component_availability(mtbf, mttr), annotation),
                            color="#555", dx=-0.3)
            _draw_wire(ax, x, y_ats - 0.18, x, y_gen + 0.30)
        else:
            _draw_wire(ax, x, y_top_bus, x, y_gen + 0.30)

        # Backup gen cluster (drawn to the SIDE of the main chain)
        gen_count = sum(g.count for g in config.gen_groups)
        gen_x = x - 0.95   # offset to the left of the main vertical
        # Connect to main chain via short horizontal stub
        ax.plot([gen_x + 0.22, x], [y_gen, y_gen],
                linewidth=LINE_W, color=COLOR_LINE)
        _draw_generator(ax, gen_x, y_gen, count=gen_count)
        # Annotation: mission probability + count summary
        ax.text(gen_x, y_gen - 0.35,
                f"Mission({config.mission_duration_hours:.0f}h)\n"
                f"= {result.fleet_mission.system_mission * 100:.2f}%",
                ha="center", va="top", fontsize=7, color="#555",
                family="monospace")

        # Gen output breaker
        if config.include_gen_breaker:
            color = _comp_color("gen_breaker", comp_overrides,
                                _is_bottleneck("gen_breaker"))
            _draw_breaker(ax, x, y_gen_brk, color=color)
            defn = COMP_DEFAULTS["gen_breaker"]
            mtbf, mttr = comp_overrides.get("gen_breaker", (defn.mtbf_hours, defn.mttr_hours))
            _annotate_block(ax, x, y_gen_brk,
                            _format_annot(component_availability(mtbf, mttr), annotation))
            _draw_wire(ax, x, y_gen + 0.20, x, y_gen_brk + 0.10)
            _draw_wire(ax, x, y_gen_brk - 0.10, x, y_xfmr + 0.25)
        else:
            _draw_wire(ax, x, y_gen + 0.20, x, y_xfmr + 0.25)

        # Transformer (if MV distribution)
        if config.include_transformer:
            color = _comp_color("transformer", comp_overrides,
                                _is_bottleneck("transformer"))
            _draw_transformer(ax, x, y_xfmr, color=color)
            defn = COMP_DEFAULTS["transformer"]
            mtbf, mttr = comp_overrides.get("transformer", (defn.mtbf_hours, defn.mttr_hours))
            _annotate_block(ax, x, y_xfmr,
                            _format_annot(component_availability(mtbf, mttr), annotation))
            _draw_wire(ax, x, y_xfmr - 0.25, x, y_mv_bus + 0.05)
        else:
            _draw_wire(ax, x, y_xfmr + 0.25, x, y_mv_bus + 0.05)

        # LV bus (per-path short bus segment)
        if config.include_lv_bus:
            _draw_bus(ax, x - 0.4, x + 0.4, y_mv_bus, color=COLOR_LINE)
            defn = COMP_DEFAULTS["lv_bus_section"]
            mtbf, mttr = comp_overrides.get("lv_bus_section", (defn.mtbf_hours, defn.mttr_hours))
            _annotate_block(ax, x, y_mv_bus,
                            _format_annot(component_availability(mtbf, mttr), annotation))
            _draw_wire(ax, x, y_mv_bus, x, y_lv_brk + 0.10)

        # LV main breaker
        if config.include_lv_breaker:
            color = _comp_color("lv_breaker", comp_overrides,
                                _is_bottleneck("lv_breaker"))
            _draw_breaker(ax, x, y_lv_brk, color=color)
            defn = COMP_DEFAULTS["lv_breaker"]
            mtbf, mttr = comp_overrides.get("lv_breaker", (defn.mtbf_hours, defn.mttr_hours))
            _annotate_block(ax, x, y_lv_brk,
                            _format_annot(component_availability(mtbf, mttr), annotation))
            _draw_wire(ax, x, y_lv_brk - 0.10, x, y_ups + 0.25)
        else:
            _draw_wire(ax, x, y_mv_bus, x, y_ups + 0.25)

        # UPS
        if config.include_ups:
            color = _comp_color("ups_module", comp_overrides,
                                _is_bottleneck("ups_module"))
            _draw_ups(ax, x, y_ups, color=color,
                      modules_req=config.ups_modules_required,
                      modules_inst=config.ups_modules_per_path)
            defn = COMP_DEFAULTS["ups_module"]
            mtbf, mttr = comp_overrides.get("ups_module", (defn.mtbf_hours, defn.mttr_hours))
            unit_a = component_availability(mtbf, mttr)
            ups_sys_a = kofn_availability(config.ups_modules_per_path,
                                          config.ups_modules_required, unit_a)
            _annotate_block(ax, x, y_ups,
                            _format_annot(ups_sys_a, annotation))
            _draw_wire(ax, x, y_ups - 0.20, x, y_batt + 0.20)

            # Battery
            if config.include_ups_battery:
                color = _comp_color("ups_battery_string", comp_overrides,
                                    _is_bottleneck("ups_battery_string"))
                _draw_battery(ax, x, y_batt, color=color)
                defn = COMP_DEFAULTS["ups_battery_string"]
                mtbf, mttr = comp_overrides.get("ups_battery_string",
                                                (defn.mtbf_hours, defn.mttr_hours))
                _annotate_block(ax, x, y_batt,
                                _format_annot(component_availability(mtbf, mttr), annotation))
                _draw_wire(ax, x, y_batt - 0.20, x, y_pdu + 0.20)
            else:
                _draw_wire(ax, x, y_ups - 0.20, x, y_pdu + 0.20)

        # PDU
        if config.include_pdu:
            color = _comp_color("pdu_rpp", comp_overrides,
                                _is_bottleneck("pdu_rpp"))
            _draw_pdu(ax, x, y_pdu, color=color,
                      pdus_req=config.pdus_required,
                      pdus_inst=config.pdus_per_path)
            defn = COMP_DEFAULTS["pdu_rpp"]
            mtbf, mttr = comp_overrides.get("pdu_rpp", (defn.mtbf_hours, defn.mttr_hours))
            unit_a = component_availability(mtbf, mttr)
            if config.pdus_per_path > 1:
                pdu_sys_a = kofn_availability(config.pdus_per_path,
                                              config.pdus_required, unit_a)
            else:
                pdu_sys_a = unit_a
            _annotate_block(ax, x, y_pdu,
                            _format_annot(pdu_sys_a, annotation))
            _draw_wire(ax, x, y_pdu - 0.18, x, y_bot_bus)
        else:
            _draw_wire(ax, x, y_ups - 0.20, x, y_bot_bus)

    # ── 3. Bottom bus + load ─────────────────────────────────────────────
    _draw_bus(ax, bus_left, bus_right, y_bot_bus)
    _draw_wire(ax, 0, y_bot_bus, 0, y_load + 0.30)

    k = config.paths_required if config.paths_required > 0 else 1
    load_sub = (f"{k}-of-{n_paths} paths required" if n_paths > 1
                else "single path")
    _draw_load(ax, 0, y_load, label="LOADS", subtitle=load_sub)

    # ── 4. System availability annotation at the load ───────────────────
    ax.text(1.3, y_load + 0.0,
            f"SYSTEM\n{_format_annot(result.system_availability, annotation)}",
            ha="left", va="center", fontsize=9, fontweight="bold",
            color="#1f4e79",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#e9f4ff",
                      edgecolor="#1f4e79", linewidth=1.0))

    # ── 5. Legend ────────────────────────────────────────────────────────
    legend_elements = [
        Line2D([0], [0], color=COLOR_SPECIFIED, lw=2.5, label="Specified"),
        Line2D([0], [0], color=COLOR_PLACEHOLDER, lw=2.5, label="Placeholder"),
        Line2D([0], [0], color=COLOR_OVERRIDE, lw=2.5, label="User override"),
        Line2D([0], [0], color=COLOR_BOTTLENECK, lw=2.5, label="Top bottleneck"),
    ]
    ax.legend(handles=legend_elements, loc="lower left",
              fontsize=8, frameon=True, framealpha=0.9, ncol=4,
              bbox_to_anchor=(0.0, -0.02))

    # ── 6. Final axis config ─────────────────────────────────────────────
    x_margin = 1.5
    ax.set_xlim(bus_left - x_margin, bus_right + x_margin + 1.5)
    ax.set_ylim(-1.5, y_utility + 1.2)
    ax.set_aspect("equal")
    ax.axis("off")

    plt.tight_layout()
    return fig


def sld_to_png_bytes(fig) -> bytes:
    """Convert a matplotlib figure to PNG bytes."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    return buf.getvalue()
