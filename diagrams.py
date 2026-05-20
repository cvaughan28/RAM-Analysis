"""
System topology diagram builder for the RAM analysis tool.

Produces a graphviz DOT string representing the current configuration with
reliability annotations on each component block. Designed to be rendered via
Streamlit's `st.graphviz_chart()` (no extra dependencies — graphviz binary
ships with Streamlit).

The diagram shows:
  - Source layer: grid + backup (grid_with_backup mode) or fleet only (islanded)
  - Distribution layer: one vertical chain per path, components in series
  - Load layer: single node with k-of-n annotation if multi-path

Each component block carries:
  - Component name (and k-of-n subtitle for grouped components like UPS)
  - User-selectable reliability annotation: availability % | downtime min/yr | unavailability ppm
  - Border color indicating data provenance:
      green  = Specified (published source)
      yellow = Placeholder (engineering estimate)
      blue   = User override (entered via Components tab)
      red    = Top sensitivity contributor (only if highlight_bottleneck=True)
"""

from __future__ import annotations

from typing import Optional, Tuple

from defaults import COMP_DEFAULTS
from reliability import component_availability, kofn_availability


# ─── Palette ────────────────────────────────────────────────────────────────

COLOR_SPECIFIED   = "#28a745"   # green
COLOR_PLACEHOLDER = "#f0ad4e"   # yellow / amber
COLOR_OVERRIDE    = "#0d6efd"   # blue
COLOR_BOTTLENECK  = "#dc3545"   # red
COLOR_SOURCE      = "#212529"   # near-black for source / load nodes
COLOR_BG_SOURCE   = "#e9ecef"   # light grey fill for source nodes
COLOR_BG_LOAD     = "#fff3cd"   # light amber for load node


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_active_components(config) -> list[str]:
    """Return ordered list of active component keys for one path."""
    active = []
    if config.include_paralleling_switchgear: active.append("paralleling_switchgear")
    if config.include_gen_breaker:            active.append("gen_breaker")
    if config.include_mv_breaker:             active.append("mv_breaker")
    if config.include_mv_bus:                 active.append("mv_bus_section")
    if config.include_ats:                    active.append("ats_transfer_switch")
    if config.include_transformer:            active.append("transformer")
    if config.include_lv_bus:                 active.append("lv_bus_section")
    if config.include_lv_breaker:             active.append("lv_breaker")
    if config.include_ups:
        active.append("ups_module")
        if config.include_ups_battery:        active.append("ups_battery_string")
        if config.include_ups_sts:            active.append("ups_static_switch")
    if config.include_pdu:                    active.append("pdu_rpp")
    if config.include_rack_pdu:               active.append("rack_pdu")
    if config.include_it_psu:                 active.append("it_psu")
    return active


def _component_info(
    comp_key: str,
    config,
    comp_overrides: dict,
) -> Tuple[str, str, float]:
    """Return (display_name, border_color, block_availability) for one component.

    For k-of-n groups (UPS, PDU when pdus_per_path>1), returns the k-of-n
    availability and a display name with the k-of-n subtitle baked in.
    """
    defn = COMP_DEFAULTS[comp_key]

    if comp_key in comp_overrides:
        mtbf, mttr = comp_overrides[comp_key]
        border = COLOR_OVERRIDE
    else:
        mtbf, mttr = defn.mtbf_hours, defn.mttr_hours
        border = COLOR_PLACEHOLDER if defn.is_placeholder else COLOR_SPECIFIED

    unit_a = component_availability(mtbf, mttr)

    # k-of-n group handling
    if comp_key == "ups_module" and config.include_ups:
        block_a = kofn_availability(
            config.ups_modules_per_path,
            config.ups_modules_required,
            unit_a,
        )
        name = (f"UPS System"
                f"|{config.ups_modules_required}-of-{config.ups_modules_per_path} modules")
    elif comp_key == "pdu_rpp" and config.include_pdu and config.pdus_per_path > 1:
        block_a = kofn_availability(
            config.pdus_per_path,
            config.pdus_required,
            unit_a,
        )
        name = (f"PDU / RPP Tier"
                f"|{config.pdus_required}-of-{config.pdus_per_path}")
    else:
        block_a = unit_a
        name = defn.display_name

    return name, border, block_a


def _format_annotation(avail: float, annotation: str) -> str:
    """Format the reliability annotation for a single block."""
    if annotation == "availability":
        return f"A = {avail * 100:.4f}%"
    elif annotation == "downtime":
        dt = (1.0 - avail) * 525_960  # min/yr (using 365.25 days)
        if dt < 1:
            return f"DT = {dt * 60:.1f} sec/yr"
        elif dt < 120:
            return f"DT = {dt:.2f} min/yr"
        elif dt < 24 * 60:
            return f"DT = {dt / 60:.2f} hr/yr"
        else:
            return f"DT = {dt / (24 * 60):.2f} d/yr"
    else:  # unavailability
        u_ppm = (1.0 - avail) * 1e6
        if u_ppm < 1:
            return f"U = {u_ppm * 1000:.2f} ppb"
        return f"U = {u_ppm:.2f} ppm"


def _find_bottleneck_label(sensitivity: dict) -> Optional[str]:
    """Identify the top sensitivity contributor that is a distribution component
    (not a CCF beta entry, not the source / grid / fleet entries)."""
    candidates = []
    for label, delta in sensitivity.items():
        lower = label.lower()
        if any(kw in lower for kw in ["ccf beta", "source ", "  -> ", "grid feed"]):
            continue
        candidates.append((label, abs(delta)))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


def _node_label_html(title: str, subtitle: Optional[str], annot: str) -> str:
    """Build a graphviz HTML label with bold title + optional subtitle + annotation."""
    parts = [f"<B>{_escape(title)}</B>"]
    if subtitle:
        parts.append(f'<FONT POINT-SIZE="9" COLOR="#666">{_escape(subtitle)}</FONT>')
    parts.append(f'<FONT POINT-SIZE="10">{_escape(annot)}</FONT>')
    return "<" + "<BR/>".join(parts) + ">"


def _escape(s: str) -> str:
    """Escape characters that have special meaning in graphviz HTML labels."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


# ─── Path naming ────────────────────────────────────────────────────────────

def _path_letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else str(i + 1)


# ─── Main entry point ──────────────────────────────────────────────────────

def build_topology_diagram(
    result,
    comp_overrides: dict,
    annotation: str = "availability",
    highlight_bottleneck: bool = False,
) -> str:
    """Build a graphviz DOT string for the topology diagram.

    Parameters
    ----------
    result : SystemResult
        Output of `calculate_system()`.
    comp_overrides : dict
        User overrides {component_key: (mtbf, mttr)} from session state.
    annotation : str
        "availability" (default) | "downtime" | "unavailability"
    highlight_bottleneck : bool
        If True, the top sensitivity contributor is outlined in red.

    Returns
    -------
    A DOT-format graph string, ready for `st.graphviz_chart()`.
    """
    if comp_overrides is None:
        comp_overrides = {}

    config = result.config
    bottleneck = (_find_bottleneck_label(result.sensitivity)
                  if highlight_bottleneck else None)

    lines: list[str] = []
    lines.append("digraph SystemDiagram {")
    lines.append("  rankdir=TB;")
    lines.append("  bgcolor=transparent;")
    lines.append("  pad=0.2;")
    lines.append("  nodesep=0.35;")
    lines.append("  ranksep=0.45;")
    lines.append('  node [shape=box, style="filled,rounded", '
                 'fillcolor=white, fontname="Helvetica", fontsize=10, '
                 'penwidth=2.0, margin="0.15,0.1"];')
    lines.append('  edge [arrowhead=none, color="#888", penwidth=1.2];')
    lines.append("")

    # ── 1. Source layer ────────────────────────────────────────────────────
    if config.power_source_mode == "grid_with_backup":
        # Grid node
        grid_subtitle = (
            f"MTBF={config.grid_mtbf_hours:,.0f} h, MTTR={config.grid_mttr_hours:.1f} h"
        )
        grid_annot = _format_annotation(result.grid_availability, annotation)
        lines.append(
            f'  grid [label={_node_label_html("Utility Grid", grid_subtitle, grid_annot)},'
            f' color="{COLOR_SOURCE}", fillcolor="{COLOR_BG_SOURCE}"];'
        )

        # Backup fleet node
        n_per_path = sum(g.count for g in config.gen_groups)
        backup_subtitle = (
            f"{n_per_path} gens installed / {config.gen_required} required"
        )
        # In grid mode the backup's contribution = mission probability, not continuous A.
        backup_annot = (
            f"Mission({config.mission_duration_hours:.0f}h) = "
            f"{result.fleet_mission.system_mission * 100:.2f}%"
        )
        lines.append(
            f'  backup [label={_node_label_html("Backup Fleet", backup_subtitle, backup_annot)},'
            f' color="{COLOR_SOURCE}", fillcolor="{COLOR_BG_SOURCE}"];'
        )

        # Combined source node (OR of grid + backup mission)
        src_annot = _format_annotation(result.source_availability, annotation)
        lines.append(
            f'  source [label={_node_label_html("Combined Source", "Grid OR Backup-on-mission", src_annot)},'
            f' color="{COLOR_SOURCE}", fillcolor="{COLOR_BG_SOURCE}"];'
        )

        lines.append("  {rank=same; grid; backup;}")
        lines.append("  grid -> source;")
        lines.append("  backup -> source;")
        source_node = "source"
    else:
        # Islanded: single fleet node IS the source
        n_total = sum(g.count for g in config.gen_groups)
        fleet_subtitle = f"{n_total} gens / {config.gen_required} required"
        fleet_annot = _format_annotation(
            result.path_results[0].gen_fleet_availability, annotation
        )
        lines.append(
            f'  source [label={_node_label_html("Generator Fleet", fleet_subtitle, fleet_annot)},'
            f' color="{COLOR_SOURCE}", fillcolor="{COLOR_BG_SOURCE}"];'
        )
        source_node = "source"

    lines.append("")

    # ── 2. Per-path distribution chains ────────────────────────────────────
    active_comps = _get_active_components(config)

    for path_i in range(config.num_paths):
        plet = _path_letter(path_i)
        lines.append(f"  // ─── Path {plet} ───")
        prev_node = source_node
        for comp_key in active_comps:
            name, border, block_a = _component_info(comp_key, config, comp_overrides)

            # Split "Name|Subtitle" into title + subtitle
            if "|" in name:
                title, subtitle = name.split("|", 1)
            else:
                title, subtitle = name, None

            annot = _format_annotation(block_a, annotation)

            # Bottleneck highlight check (match by title)
            color = border
            if bottleneck is not None:
                # The sensitivity dict uses the full display_name; compare loosely.
                bn_lower = bottleneck.strip().lower()
                if (title.lower() in bn_lower) or (bn_lower in title.lower()):
                    color = COLOR_BOTTLENECK

            node_id = f"{comp_key}_{plet}"
            lines.append(
                f'  {node_id} [label={_node_label_html(title, subtitle, annot)},'
                f' color="{color}"];'
            )
            lines.append(f"  {prev_node} -> {node_id};")
            prev_node = node_id

        # Connect last component in this path to the load
        lines.append(f"  {prev_node} -> load;")
        lines.append("")

    # ── 3. Load node ───────────────────────────────────────────────────────
    if config.num_paths > 1:
        k = result.config.paths_required if result.config.paths_required > 0 else 1
        load_subtitle = f"{k}-of-{config.num_paths} paths required"
    else:
        load_subtitle = "single path"
    sys_annot = _format_annotation(result.system_availability, annotation)
    lines.append(
        f'  load [label={_node_label_html("Load", load_subtitle, sys_annot)},'
        f' color="{COLOR_SOURCE}", fillcolor="{COLOR_BG_LOAD}", shape=octagon];'
    )

    lines.append("}")
    return "\n".join(lines)
