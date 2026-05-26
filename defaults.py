"""
Default (placeholder) component reliability parameters.

Data provenance
---------------
  SPECIFIED  — value comes from a named public source (IEEE 493, Vertiv, NREL, etc.)
  PLACEHOLDER — engineering estimate or no strong public data available;
                MUST be replaced with OEM / CMMS / OREDA data before use in a
                final project RAM study.

Data source hierarchy (descending preference):
  Tier A — site CMMS / service tickets / EPMS / DCIM / load-bank test results
  Tier B — OEM model-specific data sheets, service advisories, field-service stats
  Tier C — standards & field studies: IEEE 493, NREL, NRC/INL, OREDA, ABB, Vertiv
  Tier D — engineering judgment / assumption (requires owner + expiry plan)

References:
  Eaton app paper (IEEE 493 installed-base): MV drawout breaker 2,459,000 h MTBF
  Vertiv demonstrated STS MTBF: >22,000,000 h
  ABB DPA UPS white paper: system-level MTBF for 4-module N+1 configurations
  NRC/INL EDG Performance (2022 update): FTS, FTLR, FTR>1h rates
  NREL/TP-5D00-76553: EDG mission reliability + repair times
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
    confidence: str = "Low"    # "High", "Medium", "Low"

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

    @property
    def source_type(self) -> str:
        """Classify source for assumptions register."""
        s = self.source.upper()
        if "PLACEHOLDER" in s or "ASSUMPTION" in s:
            return "Assumption"
        if "IEEE" in s:
            return "IEEE Standard"
        if "VERTIV" in s or "ABB" in s or "EATON" in s or "OEM" in s:
            return "OEM"
        if "NREL" in s or "NRC" in s or "OREDA" in s:
            return "Public Study"
        return "Other"


@dataclass
class GenDef:
    """Reliability definition for a prime-mover type."""
    display_name: str
    mtbf_hours: float          # Continuous-running MTBF (hours between failures)
    mttr_hours: float          # Mean time to repair (hours)
    fts_probability: float     # Fail-to-start probability per demand
    ftlr_probability: float    # Fail-to-load/run (early carry-load failure) per demand
    is_placeholder: bool
    source: str
    notes: str
    confidence: str = "Low"    # "High", "Medium", "Low"

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

    @property
    def source_type(self) -> str:
        s = self.source.upper()
        if "PLACEHOLDER" in s:
            return "Assumption"
        if "NREL" in s or "NRC" in s or "INL" in s:
            return "Public Study"
        if "OEM" in s:
            return "OEM"
        return "Other"


# ---------------------------------------------------------------------------
# UPS system-level architecture reference values (ABB DPA white paper)
# These are SYSTEM-level MTBFs for complete configured architectures,
# NOT per-module values. Use for audit cross-check against the k-of-n model.
# ---------------------------------------------------------------------------

UPS_SYSTEM_REFS = {
    "ABB_DPA_4mod_N1_sep_batt": {
        "label": "Modular UPS 4 modules N+1, separate batteries (ABB DPA)",
        "mtbf_hours": 1_248_000,
        "config": "4 modules, k=3 required, separate battery strings",
        "lambda_per_1e6h": 0.801,
        "mttr_hours": 0.5,
        "source": "ABB DPA UPS white paper — System reliability analysis "
                  "(library.e.abb.com), system-level MTBF for full configured architecture.",
        "confidence": "High",
        "notes": (
            "System-level MTBF — not a per-module value. Use as validation cross-check "
            "against the per-module k-of-n model. Separate battery architecture "
            "materially outperforms common-battery configuration."
        ),
    },
    "ABB_DPA_4mod_N1_common_batt": {
        "label": "Modular UPS 4 modules N+1, common battery (ABB DPA)",
        "mtbf_hours": 746_000,
        "config": "4 modules, k=3 required, shared common battery",
        "lambda_per_1e6h": 1.34,
        "mttr_hours": 0.5,
        "source": "ABB DPA UPS white paper — System reliability analysis "
                  "(library.e.abb.com), system-level MTBF for full configured architecture.",
        "confidence": "High",
        "notes": (
            "System-level MTBF — not a per-module value. Common battery architecture "
            "has ~40% lower system MTBF than separate battery for same module count. "
            "ABB paper uses this to illustrate why battery architecture matters."
        ),
    },
}


# ---------------------------------------------------------------------------
# Prime-mover defaults
# ---------------------------------------------------------------------------

GEN_DEFAULTS: dict[str, GenDef] = {
    "Diesel Generator": GenDef(
        display_name="Diesel Generator",
        mtbf_hours=4_380,          # ~0.5 yr continuous — PLACEHOLDER
        mttr_hours=33.9,           # NREL 2020 mean repair time
        fts_probability=0.0013,    # NREL 2020: 0.13% per demand (well-maintained)
        ftlr_probability=0.00331,  # NRC/INL 2022 EPS EDG mean: 0.331% per demand
        is_placeholder=True,
        confidence="Medium",
        source=(
            "Continuous MTBF: engineering estimate (PLACEHOLDER). "
            "MTTR: NREL 2020 (NREL/TP-5D00-76553) — 33.9 h mean, 14.7 h median. "
            "FTS: NREL 2020 — 0.13% well-maintained standby EDG fleet. "
            "FTLR: NRC/INL 2022 (nrcoe.inl.gov/publicdocs/CompPerf/edg-2022.pdf) "
            "— 0.331% per demand NRC EPS EDG mean for fail-to-load/run."
        ),
        notes=(
            "Continuous-run MTBF is an engineering estimate — replace with OEM service data. "
            "NREL MTTR (33.9 h mean) and FTS (0.13%) are for well-maintained standby EDGs. "
            "NRC/INL 2022 mean for EPS EDG FTS is 0.222%; 0.13% represents a well-maintained fleet. "
            "FTLR (0.331%) is the NRC/INL 2022 mean for fail-to-load or early carry-load failure. "
            "Run failure rate after first hour: NRC/INL mean FTR>1h = 1.18E-3 per run-hour "
            "(1,180 failures/10^6 run-h) — this is captured in lambda_run (=1/MTBF). "
            "Replace all parameters with OEM service data for your specific model and PM regime."
        ),
    ),
    "Gas Turbine": GenDef(
        display_name="Gas Turbine",
        mtbf_hours=8_760,
        mttr_hours=48.0,
        fts_probability=0.002,
        ftlr_probability=0.002,
        is_placeholder=True,
        confidence="Low",
        source="PLACEHOLDER — engineering estimate. Use OEM / OREDA turbine data.",
        notes=(
            "Gas turbine continuous-run MTBF and repair times are not available "
            "in strong open public sources for data-center-duty machines. "
            "Replace with OEM service statistics or OREDA onshore/offshore turbine data. "
            "FTLR is an engineering placeholder — obtain from OEM service statistics."
        ),
    ),
    "Natural Gas Engine": GenDef(
        display_name="Natural Gas Engine",
        mtbf_hours=6_000,
        mttr_hours=24.0,
        fts_probability=0.002,
        ftlr_probability=0.002,
        is_placeholder=True,
        confidence="Low",
        source="PLACEHOLDER — engineering estimate. Use OEM / OREDA data.",
        notes=(
            "NG reciprocating engine. Actual MTBF depends heavily on fuel quality, "
            "load factor, and PM interval. FTLR is an engineering placeholder. "
            "Replace all parameters with OEM service data."
        ),
    ),
    "Microturbine": GenDef(
        display_name="Microturbine",
        mtbf_hours=10_000,
        mttr_hours=24.0,
        fts_probability=0.001,
        ftlr_probability=0.001,
        is_placeholder=True,
        confidence="Low",
        source="PLACEHOLDER — engineering estimate. Use OEM data.",
        notes=(
            "Microturbine CHP/generation unit. Manufacturer-reported MTBF varies "
            "widely by model and operating profile. FTLR is a placeholder. "
            "Replace with OEM data."
        ),
    ),
    "Fuel Cell (SOFC/PAFC)": GenDef(
        display_name="Fuel Cell (SOFC / PAFC)",
        mtbf_hours=20_000,
        mttr_hours=48.0,
        fts_probability=0.001,
        ftlr_probability=0.001,
        is_placeholder=True,
        confidence="Low",
        source="PLACEHOLDER — engineering estimate. Use OEM data.",
        notes=(
            "Fuel cell stack degradation is a wear-out phenomenon; constant-hazard "
            "MTBF is a rough screen only. FTLR is a placeholder. "
            "Replace with manufacturer service data."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Electrical component defaults
# ---------------------------------------------------------------------------

COMP_DEFAULTS: dict[str, CompDef] = {

    # ── Generation bus / paralleling ────────────────────────────────────────

    "paralleling_switchgear": CompDef(
        display_name="Paralleling Switchgear / Gen Bus",
        mtbf_hours=1_428_571,
        mttr_hours=4.0,
        is_placeholder=True,
        confidence="Low",
        source=(
            "PLACEHOLDER — IEEE 493 ATS/switch proxy (lambda = 0.70 / 10^6 h). "
            "Replace with OEM paralleling switchgear data."
        ),
        notes=(
            "Paralleling switchgear controls and bus bars. The ATS/switch screening "
            "rate (lambda = 0.70 / 10^6 h) from IEEE 493 is used as a proxy because "
            "dedicated paralleling gear MTBF is not available in open sources. "
            "Final model should use OEM service statistics."
        ),
    ),

    "gen_breaker": CompDef(
        display_name="Generator Output Breaker (LV)",
        mtbf_hours=3_225_806,
        mttr_hours=4.0,
        is_placeholder=False,
        confidence="High",
        source=(
            "IEEE 493 public extract — LV circuit breaker at 480 V; "
            "lambda = 0.0027 failures/year (= 0.31 / 10^6 h), repair = 4.0 h."
        ),
        notes="High-confidence IEEE 493 value for a low-voltage air circuit breaker.",
    ),

    # ── Transfer / path switching ────────────────────────────────────────────

    "ats_transfer_switch": CompDef(
        display_name="ATS / Transfer Switch",
        # IEEE 3006.8-2018 Table 2, aggregated data for "Switch, automatic transfer":
        # lambda = 0.03502 failures/year -> MTBF = 8760/0.03502 = 250,143 h
        # MTTR = 7.89 h.  Inherent availability = 0.999968.
        mtbf_hours=250_143,
        mttr_hours=7.89,
        is_placeholder=False,
        confidence="Medium",
        source=(
            "IEEE 3006.8-2018 Table 2, aggregated 'Switch, automatic transfer' "
            "data — lambda = 0.03502 failures/year, MTTR = 7.89 h. "
            "Replace with installed-model OEM data for final design assurance."
        ),
        notes=(
            "Open-transition ATS, 3-phase 4-wire, aggregated industry data. "
            "IEEE 3006.8 is the consolidated update to IEEE 493 reliability tables. "
            "OEM-specific demand-failure rates can vary substantially — for "
            "modern microprocessor-controlled ATSes with stocked spares, actual "
            "availability is often better than this published mean."
        ),
    ),

    # ── MV distribution ──────────────────────────────────────────────────────

    "mv_breaker": CompDef(
        display_name="MV Breaker / MV Switching Element",
        # Eaton app paper citing IEEE 493 installed-base for metalclad drawout breaker:
        # lambda = 0.406 / 10^6 h  →  MTBF = 1 / 0.406e-6 = 2,463,054 h
        # Doc cites 2,459,000 h directly; using that rounded figure.
        mtbf_hours=2_459_000,
        mttr_hours=2.1,
        is_placeholder=False,
        confidence="High",
        source=(
            "Eaton application paper citing IEEE 493 installed-base data for metalclad "
            "drawout circuit breakers — 2,459,000 h MTBF (lambda = 0.406 / 10^6 h), "
            "repair = 2.1 h (replace); full restoration 83.1 h for major faults. "
            "For Tier IV hot-spare modeling: 2-4 h restoration assumption. "
            "Ref: eaton.com medium-voltage-circuit-breakers-reliability ap083006en.pdf"
        ),
        notes=(
            "High-confidence installed-base figure from Eaton/IEEE 493. "
            "Use 2.1 h for breaker swap; extend MTTR for switchgear cell rebuild "
            "(83.1 h in full repair scenario). "
            "With hot-spare cell strategy, 2-4 h restoration is realistic for Tier IV."
        ),
    ),

    "mv_bus_section": CompDef(
        display_name="MV Bus Section / MV Busduct",
        mtbf_hours=5_000_000,
        mttr_hours=8.0,
        is_placeholder=True,
        confidence="Low",
        source="PLACEHOLDER — IEEE 493 bus data exists but not extracted here.",
        notes=(
            "MV bus sections and bus ducts are passive; failures are rare and often "
            "damage-event driven. IEEE 493 has bus-duct failure data. "
            "Replace with IEEE 493 values for final model."
        ),
    ),

    # ── Transformer ──────────────────────────────────────────────────────────

    "transformer": CompDef(
        display_name="Step-Down Transformer (MV -> LV)",
        # IEEE 493 Gold Book Table 10-15 / IEEE 3006.8-2018, 5 MVA substation
        # transformer, liquid-filled, 35 kV primary / 480 V secondary:
        # lambda = 0.011 failures/year -> MTBF = 8760/0.011 = 796,364 h
        # MTTR = 5.0 h.  Inherent availability = 0.999994.
        mtbf_hours=796_364,
        mttr_hours=5.0,
        is_placeholder=False,
        confidence="High",
        source=(
            "IEEE 493 Gold Book Table 10-15 / IEEE 3006.8-2018 — 5 MVA "
            "substation transformer, liquid-filled, 35 kV primary delta / "
            "480 V secondary wye. lambda = 0.011 failures/year, MTTR = 5 h. "
            "Major-fault repair (winding rewind) can extend MTTR to 168+ h; "
            "the 5 h figure reflects typical fuse/auxiliary fault repair."
        ),
        notes=(
            "Reliability varies by voltage class, kVA rating, type "
            "(dry vs liquid), and age. For different sizing/configuration "
            "consult IEEE 493 for the relevant table row. Dry-type 75 kVA "
            "step-downs (480->120V) have much higher MTBF (~3.3M h per IEEE)."
        ),
    ),

    # ── LV switchgear ────────────────────────────────────────────────────────

    "lv_bus_section": CompDef(
        display_name="LV Bus Section / LV Switchboard",
        # IEEE 3006.8-2018 Table 2, "Distribution panel / Switchboard > 225 A"
        # (covers the typical 480V 1200A switchboard):
        # lambda = 0.004327 failures/year -> MTBF = 8760/0.004327 = 2,024,500 h
        # MTTR = 16.0 h.  Inherent availability = 0.999992.
        mtbf_hours=2_024_500,
        mttr_hours=16.0,
        is_placeholder=False,
        confidence="Medium",
        source=(
            "IEEE 3006.8-2018 Table 2 — 'Distribution panel / Switchboard > 225 A' "
            "covering typical 480V 1200A switchboard configuration. "
            "lambda = 0.004327 failures/year, MTTR = 16 h. "
            "Longer MTTR reflects bus-section repair complexity (de-energized "
            "work, sectionalizing). For purely passive busway runs, MTTR can be "
            "shorter; for switchgear-cell repairs, longer."
        ),
        notes=(
            "Covers LV bus sections and LV switchboards. Drawout-type 3000A "
            "switchgear (which is more complex) is closer to lambda = 0.00949/yr "
            "with MTTR ~7.3 h per IEEE 3006.8 (use paralleling_switchgear key if "
            "you want to model that separately). LV busway is more reliable than "
            "switchboard but the difference is second-order at this MTBF level."
        ),
    ),

    "lv_breaker": CompDef(
        display_name="LV Breaker (480 V)",
        mtbf_hours=3_225_806,
        mttr_hours=4.0,
        is_placeholder=False,
        confidence="High",
        source=(
            "IEEE 493 public extract — LV breaker at 480 V; "
            "lambda = 0.0027 failures/year (= 0.31 / 10^6 h), repair = 4.0 h."
        ),
        notes="High-confidence IEEE 493 value.",
    ),

    # ── UPS ──────────────────────────────────────────────────────────────────

    "ups_module": CompDef(
        display_name="Modular UPS Power Module (per module)",
        mtbf_hours=170_000,
        mttr_hours=0.5,
        is_placeholder=False,
        confidence="High",
        source=(
            "Vertiv Liebert APM2 UL Guide Specifications (official) — "
            "MTBF >= 170,000 h per module. MTTR = hot-swap module replacement time. "
            "Validation reference: ABB DPA white paper gives system-level MTBF of "
            "1,248,000 h for 4-module N+1 separate-battery configuration and "
            "746,000 h for N+1 common-battery (see UPS_SYSTEM_REFS in defaults.py)."
        ),
        notes=(
            "Per-module MTBF used in k-of-n model. "
            "ABB DPA system-level values (1,248,000 h / 746,000 h) serve as audit "
            "cross-checks for the 4-module N+1 k-of-n result. "
            "Battery architecture (separate vs. common) materially affects system MTBF; "
            "model battery strings separately. "
            "MTTR of 0.5 h assumes trained tech with spare module on site."
        ),
    ),

    "ups_battery_string": CompDef(
        display_name="UPS Battery String (VRLA)",
        mtbf_hours=100_000,
        mttr_hours=2.0,
        is_placeholder=True,
        confidence="Low",
        source=(
            "PLACEHOLDER — IEEE 1188 governs VRLA maintenance & replacement; "
            "constant-hazard MTBF is not appropriate for wear-out batteries. "
            "Revised study recommends: separate vs common battery architecture "
            "materially changes UPS system MTBF (see ABB DPA reference)."
        ),
        notes=(
            "VRLA batteries are wear-out items. A constant-hazard MTBF model is "
            "inappropriate for final design assurance. Model by age, impedance test "
            "coverage, and replacement interval per IEEE 1188. "
            "This placeholder is for screening only. "
            "Block replacement MTTR: 4-8 h; full-string planned replacement: 12-24 h."
        ),
    ),

    "ups_static_switch": CompDef(
        display_name="UPS Static Transfer Switch (STS)",
        # Vertiv demonstrated >22,000,000 h MTBF
        # lambda < 0.045 / 10^6 h  =>  MTBF > 22,222,222 h
        mtbf_hours=22_222_222,
        mttr_hours=0.5,
        is_placeholder=False,
        confidence="High",
        source=(
            "Vertiv — demonstrated MTBF >22,000,000 h (lambda <0.045 / 10^6 h). "
            "MTTR = 0.5 h (module swap assumption)."
        ),
        notes=(
            "High-confidence OEM-demonstrated value from Vertiv. "
            "STS failure contribution to system unavailability is negligible "
            "at this MTBF level. Demand-failure probability should also be assessed "
            "separately for transfer events."
        ),
    ),

    # ── Distribution ─────────────────────────────────────────────────────────

    "pdu_rpp": CompDef(
        display_name="PDU / RPP",
        mtbf_hours=200_000,
        mttr_hours=1.0,
        is_placeholder=True,
        confidence="Low",
        source=(
            "PLACEHOLDER — not available in strong open public sources. "
            "Use OEM data or Quanterion ROADS / FRACAS history. "
            "Revised study: retain 200,000 h as screening assumption only; "
            "obtain OEM/site data to replace."
        ),
        notes=(
            "Floor-level Power Distribution Unit or Remote Power Panel. "
            "MTBF is not in open IEEE 493 or similar sources. "
            "Assume spare board/device on site for 1-2 h MTTR screen."
        ),
    ),

    "rack_pdu": CompDef(
        display_name="Rack PDU",
        mtbf_hours=500_000,
        mttr_hours=0.5,
        is_placeholder=True,
        confidence="Low",
        source="PLACEHOLDER — obtain from rack PDU OEM.",
        notes="Rack-level monitored/switched PDU. Passive versions have high MTBF.",
    ),

    "it_psu": CompDef(
        display_name="IT Power Supply Unit (per cord)",
        mtbf_hours=300_000,
        mttr_hours=0.5,
        is_placeholder=True,
        confidence="Low",
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

PATH_COMPONENT_ORDER = [
    "paralleling_switchgear",
    "gen_breaker",
    "mv_breaker",
    "mv_bus_section",
    "ats_transfer_switch",
    "transformer",
    "lv_bus_section",
    "lv_breaker",
    "ups_module",
    "ups_battery_string",
    "ups_static_switch",
    "pdu_rpp",
    "rack_pdu",
    "it_psu",
]

GEN_TYPE_LIST = list(GEN_DEFAULTS.keys())
