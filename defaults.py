"""
Default (placeholder) component reliability parameters.

Data provenance
---------------
  SPECIFIED  — value comes from a named public source (IEEE 493, Vertiv, NREL, etc.)
  PLACEHOLDER — engineering estimate or no strong public data available;
                MUST be replaced with OEM / CMMS / OREDA data before use in a
                final project RAM study.

The paper basis is:
  "Full RAM Analysis Framework for a Tier IV Data Center Site"
  with references to IEEE 493, Uptime Institute Tier Standard, NREL EDG study,
  Vertiv APM2 guide spec, and ABB DPA UPS white paper.
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CompDef:
    """Reliability definition for one electrical component."""
    display_name: str
    mtbf_hours: float          # Always a float; placeholder if is_placeholder=True
    mttr_hours: float
    is_placeholder: bool       # True  → warn user to replace
    source: str
    notes: str

    @property
    def availability(self) -> float:
        from reliability import component_availability
        return component_availability(self.mtbf_hours, self.mttr_hours)

    @property
    def failure_rate_per_1e6h(self) -> float:
        return 1_000_000.0 / self.mtbf_hours if self.mtbf_hours > 0 else float("inf")

    @property
    def quality_tag(self) -> str:
        return "⚠ Placeholder" if self.is_placeholder else "✓ Public data"


@dataclass
class GenDef:
    """Reliability definition for a prime-mover type."""
    display_name: str
    mtbf_hours: float          # Continuous-running MTBF (hours between failures)
    mttr_hours: float          # Mean time to repair (hours)
    fts_probability: float     # Fail-to-start probability per demand (mission analysis)
    is_placeholder: bool
    source: str
    notes: str

    @property
    def availability(self) -> float:
        from reliability import component_availability
        return component_availability(self.mtbf_hours, self.mttr_hours)

    @property
    def lambda_run(self) -> float:
        return 1.0 / self.mtbf_hours if self.mtbf_hours > 0 else float("inf")

    @property
    def quality_tag(self) -> str:
        return "⚠ Placeholder" if self.is_placeholder else "✓ Public data"


# ---------------------------------------------------------------------------
# Prime-mover defaults
# All MTBF values for CONTINUOUS operation are engineering estimates.
# The NREL study covers standby EDGs; continuous-run MTBF must come from
# OEM service data or plant CMMS.
# ---------------------------------------------------------------------------

GEN_DEFAULTS: dict[str, GenDef] = {
    "Diesel Generator": GenDef(
        display_name="Diesel Generator",
        mtbf_hours=4_380,      # ~0.5 yr continuous — PLACEHOLDER
        mttr_hours=33.9,       # NREL 2020 mean repair time for representative EDG fleet
        fts_probability=0.0013,# NREL 2020 well-maintained fleet: 0.13% per demand
        is_placeholder=True,
        source="MTBF: engineering estimate (PLACEHOLDER). "
               "MTTR/FTS: NREL 2020 (NREL/TP-5D00-76553) — standby EDG data.",
        notes=(
            "Continuous-run MTBF is an engineering estimate. "
            "NREL MTTR (33.9 h mean, 14.7 h median) and FTS (0.13%) are for "
            "well-maintained standby EDGs and are used here as screening values only. "
            "Replace all three parameters with OEM service data for your specific "
            "prime mover model and PM regime."
        ),
    ),
    "Gas Turbine": GenDef(
        display_name="Gas Turbine",
        mtbf_hours=8_760,      # ~1 yr — PLACEHOLDER
        mttr_hours=48.0,       # PLACEHOLDER
        fts_probability=0.002, # PLACEHOLDER
        is_placeholder=True,
        source="PLACEHOLDER — engineering estimate. Use OEM / OREDA data.",
        notes=(
            "Gas turbine continuous-run MTBF and repair times are not available "
            "in strong open public sources. Replace with OEM service statistics "
            "or OREDA offshore/onshore turbine data."
        ),
    ),
    "Natural Gas Engine": GenDef(
        display_name="Natural Gas Engine",
        mtbf_hours=6_000,      # PLACEHOLDER
        mttr_hours=24.0,       # PLACEHOLDER
        fts_probability=0.002, # PLACEHOLDER
        is_placeholder=True,
        source="PLACEHOLDER — engineering estimate. Use OEM / OREDA data.",
        notes=(
            "NG reciprocating engine. Actual MTBF depends heavily on fuel quality, "
            "load factor, and PM interval. Replace with OEM service data."
        ),
    ),
    "Microturbine": GenDef(
        display_name="Microturbine",
        mtbf_hours=10_000,     # PLACEHOLDER
        mttr_hours=24.0,       # PLACEHOLDER
        fts_probability=0.001, # PLACEHOLDER
        is_placeholder=True,
        source="PLACEHOLDER — engineering estimate. Use OEM data.",
        notes=(
            "Microturbine CHP/generation unit. Manufacturer-reported MTBF varies "
            "widely by model and operating profile. Replace with OEM data."
        ),
    ),
    "Fuel Cell (SOFC/PAFC)": GenDef(
        display_name="Fuel Cell (SOFC / PAFC)",
        mtbf_hours=20_000,     # PLACEHOLDER
        mttr_hours=48.0,       # PLACEHOLDER
        fts_probability=0.001, # PLACEHOLDER
        is_placeholder=True,
        source="PLACEHOLDER — engineering estimate. Use OEM data.",
        notes=(
            "Fuel cell stack degradation is a wear-out phenomenon; constant-hazard "
            "MTBF is a rough screen only. Replace with manufacturer service data."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Electrical component defaults
# Keys match the identifiers used in models.py / app.py.
# ---------------------------------------------------------------------------

COMP_DEFAULTS: dict[str, CompDef] = {

    # ── Generation bus / paralleling ────────────────────────────────────────

    "paralleling_switchgear": CompDef(
        display_name="Paralleling Switchgear / Gen Bus",
        # λ = 0.70 / 10^6 h is the IEEE 493 switch/disconnect proxy;
        # ATS-specific public values are not openly available.
        mtbf_hours=1_428_571,
        mttr_hours=4.0,
        is_placeholder=True,
        source="PLACEHOLDER — IEEE 493 ATS/switch proxy (λ = 0.70 / 10⁶ h). "
               "Replace with OEM paralleling switchgear data.",
        notes=(
            "Paralleling switchgear controls and bus bars. The ATS/switch screening "
            "rate (λ = 0.70 / 10⁶ h) from IEEE 493 is used as a proxy because "
            "dedicated paralleling gear MTBF is not available in open sources. "
            "Final model should use OEM service statistics."
        ),
    ),

    "gen_breaker": CompDef(
        display_name="Generator Output Breaker (LV)",
        # IEEE 493 LV breaker at 480 V: λ = 0.0027 / yr → 0.31 / 10^6 h
        mtbf_hours=3_225_806,
        mttr_hours=4.0,
        is_placeholder=False,
        source="IEEE 493 public extract — LV circuit breaker at 480 V; "
               "λ = 0.0027 failures/year (= 0.31 / 10⁶ h), repair = 4.0 h.",
        notes="High-confidence IEEE 493 value for a low-voltage air circuit breaker.",
    ),

    # ── Transfer / path switching ────────────────────────────────────────────

    "ats_transfer_switch": CompDef(
        display_name="ATS / Transfer Switch",
        # λ = 0.0061 / yr (= 0.70 / 10^6 h) from IEEE 493 switch/disconnect
        mtbf_hours=1_428_571,
        mttr_hours=3.6,
        is_placeholder=True,
        source="PLACEHOLDER — IEEE 493 switch/disconnect proxy (λ = 0.70 / 10⁶ h). "
               "ATS-specific open values are not available; use OEM data.",
        notes=(
            "Transfer switch / automatic transfer switch. IEEE 493 provides a "
            "switch/disconnect screening value. ATS-specific OEM data (demand-failure "
            "probability and time-based MTBF) should replace this for the final model."
        ),
    ),

    # ── MV distribution ──────────────────────────────────────────────────────

    "mv_breaker": CompDef(
        display_name="MV Breaker / MV Switching Element",
        # λ = 0.0036 / yr → 0.41 / 10^6 h, repair = 2.1 h (IEEE 493)
        mtbf_hours=2_439_024,
        mttr_hours=2.1,
        is_placeholder=False,
        source="IEEE 493 public extract — MV breaker; "
               "λ = 0.0036 failures/year (= 0.41 / 10⁶ h), repair = 2.1 h.",
        notes="High-confidence IEEE 493 value for a medium-voltage circuit breaker.",
    ),

    "mv_bus_section": CompDef(
        display_name="MV Bus Section / MV Busduct",
        mtbf_hours=5_000_000,  # PLACEHOLDER — passive component
        mttr_hours=8.0,
        is_placeholder=True,
        source="PLACEHOLDER — IEEE 493 bus data exists but not extracted here.",
        notes=(
            "MV bus sections and bus ducts are passive; failures are rare and often "
            "damage-event driven. IEEE 493 has bus-duct failure data. "
            "Replace with IEEE 493 values for final model."
        ),
    ),

    # ── Transformer ──────────────────────────────────────────────────────────

    "transformer": CompDef(
        display_name="Step-Down Transformer (MV → LV)",
        mtbf_hours=1_000_000,  # PLACEHOLDER
        mttr_hours=168.0,      # ~1 week; major winding failure needs parts/rewind
        is_placeholder=True,
        source="PLACEHOLDER — IEEE 493 power transformer data exists; "
               "use kVA/voltage-class-specific values.",
        notes=(
            "Distribution transformer reliability varies by voltage class, kVA rating, "
            "and age. IEEE 493 provides equipment data. MTTR is long for winding "
            "failures; short for fuse/auxiliary faults. Use kVA-class-specific data."
        ),
    ),

    # ── LV switchgear ────────────────────────────────────────────────────────

    "lv_bus_section": CompDef(
        display_name="LV Bus Section / LV Busway",
        mtbf_hours=5_000_000,  # PLACEHOLDER — passive, very high MTBF
        mttr_hours=4.0,
        is_placeholder=True,
        source="PLACEHOLDER — IEEE 493 bus data.",
        notes="LV bus/busway is passive; bus failures are rare. Use IEEE 493.",
    ),

    "lv_breaker": CompDef(
        display_name="LV Breaker (480 V)",
        # IEEE 493: λ = 0.0027 / yr → 0.31 / 10^6 h, repair = 4.0 h
        mtbf_hours=3_225_806,
        mttr_hours=4.0,
        is_placeholder=False,
        source="IEEE 493 public extract — LV breaker at 480 V; "
               "λ = 0.0027 failures/year (= 0.31 / 10⁶ h), repair = 4.0 h.",
        notes="High-confidence IEEE 493 value.",
    ),

    # ── UPS ──────────────────────────────────────────────────────────────────

    "ups_module": CompDef(
        display_name="Modular UPS Power Module",
        # Vertiv Liebert APM2 Guide Spec: MTBF ≥ 170,000 h
        mtbf_hours=170_000,
        mttr_hours=0.5,        # Hot-swap modular replacement
        is_placeholder=False,
        source="Vertiv Liebert APM2 UL Guide Specifications (official) — "
               "MTBF ≥ 170,000 h. MTTR = hot-swap module replacement time.",
        notes=(
            "Official Vertiv APM2 MTBF for a single power module. "
            "MTTR of 0.5 h assumes a trained tech with the spare module on site. "
            "ABB DPA white paper provides corroborating system-level MTBF comparisons "
            "for 4-module configurations under N/N, N+1-common-batt, and N+1-sep-batt."
        ),
    ),

    "ups_battery_string": CompDef(
        display_name="UPS Battery String (VRLA)",
        mtbf_hours=100_000,    # PLACEHOLDER — simplified constant-hazard screen
        mttr_hours=2.0,
        is_placeholder=True,
        source="PLACEHOLDER — IEEE 1188 governs VRLA maintenance & replacement; "
               "constant-hazard MTBF is not appropriate for wear-out batteries.",
        notes=(
            "VRLA batteries are wear-out items. A constant-hazard MTBF model is "
            "inappropriate for final design assurance. Model by age, impedance test "
            "coverage, and replacement interval per IEEE 1188. "
            "This placeholder is for screening only."
        ),
    ),

    "ups_static_switch": CompDef(
        display_name="UPS Static Transfer Switch (STS)",
        mtbf_hours=200_000,    # PLACEHOLDER
        mttr_hours=2.0,
        is_placeholder=True,
        source="PLACEHOLDER — obtain from UPS OEM.",
        notes="STS within the UPS frame. Demand-failure probability should also be modeled.",
    ),

    # ── Distribution ─────────────────────────────────────────────────────────

    "pdu_rpp": CompDef(
        display_name="PDU / RPP",
        mtbf_hours=200_000,    # PLACEHOLDER
        mttr_hours=1.0,
        is_placeholder=True,
        source="PLACEHOLDER — not available in strong open public sources. "
               "Use OEM data or Quanterion ROADS / FRACAS history.",
        notes=(
            "Floor-level Power Distribution Unit or Remote Power Panel. "
            "MTBF is not in open IEEE 493 or similar sources. "
            "Assume spare board/device on site for 1 h MTTR screen."
        ),
    ),

    "rack_pdu": CompDef(
        display_name="Rack PDU",
        mtbf_hours=500_000,    # PLACEHOLDER
        mttr_hours=0.5,
        is_placeholder=True,
        source="PLACEHOLDER — obtain from rack PDU OEM.",
        notes="Rack-level monitored/switched PDU. Passive versions have high MTBF.",
    ),

    "it_psu": CompDef(
        display_name="IT Power Supply Unit (per cord)",
        mtbf_hours=300_000,    # PLACEHOLDER
        mttr_hours=0.5,
        is_placeholder=True,
        source="PLACEHOLDER — obtain server OEM PSU data.",
        notes=(
            "Individual IT redundant PSU. Only one PSU per server is in each "
            "path for a dual-corded load. Hot-swap assumed."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Ordered component lists for UI display
# ---------------------------------------------------------------------------

# The logical order components appear in one distribution path (upstream → downstream)
PATH_COMPONENT_ORDER = [
    "paralleling_switchgear",
    "gen_breaker",
    "mv_breaker",
    "mv_bus_section",
    "ats_transfer_switch",
    "transformer",
    "lv_bus_section",
    "lv_breaker",
    # UPS system (k-of-n) is handled separately in models.py
    "ups_module",
    "ups_battery_string",
    "ups_static_switch",
    "pdu_rpp",
    "rack_pdu",
    "it_psu",
]

GEN_TYPE_LIST = list(GEN_DEFAULTS.keys())
