"""Academic, human-readable rendering of one preserved building record.

The report is deliberately a view over preserved evidence.  It does not run
the model or derive new physical results.  It may format units and labels,
classify the recorded QA evidence, and compare duplicate evidence fields so a
contradiction cannot be published silently.
"""
from __future__ import annotations

from dataclasses import dataclass
import html
import math
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

DASH = "—"


class EvidenceIdentityError(RuntimeError):
    """The requested building is not the building named by the evidence."""


@dataclass(frozen=True)
class MetricSpec:
    """Display and scientific-meaning contract for a reported metric."""

    label: str
    source: tuple[str, ...]
    unit: str
    period: str
    denominator: str
    precision: int
    description: str
    missing: str = DASH


# Results and carbon are the primary sources.  Summary copies are checked
# below, never used as fallbacks for these metrics.
METRICS: dict[str, MetricSpec] = {
    "heating": MetricSpec(
        "Space heating", ("results", "space_heating_kwh"), "kWh",
        "simulation period", "none", 1,
        "Final site energy reported for space heating."),
    "cooling": MetricSpec(
        "Space cooling", ("results", "cooling_kwh"), "kWh",
        "simulation period", "none", 1,
        "Final site energy reported for space cooling."),
    "dhw": MetricSpec(
        "Domestic hot water", ("results", "dhw_kwh"), "kWh",
        "simulation period", "none", 1,
        "Final site energy reported for domestic hot water."),
    "total": MetricSpec(
        "Total site energy", ("results", "total_site_kwh"), "kWh",
        "simulation period", "none", 1,
        "Whole-building final site energy."),
    "heating_res_eui": MetricSpec(
        "Space heating", ("results", "space_heating_kwh_m2"), "kWh/m²",
        "simulation period", "geometric residential floor area", 2,
        "Space-heating energy divided by geometric residential floor area."),
    "cooling_res_eui": MetricSpec(
        "Space cooling", ("results", "cooling_kwh_m2"), "kWh/m²",
        "simulation period", "geometric residential floor area", 2,
        "Space-cooling energy divided by geometric residential floor area."),
    "dhw_res_eui": MetricSpec(
        "Domestic hot water", ("results", "dhw_kwh_m2"), "kWh/m²",
        "simulation period", "geometric residential floor area", 2,
        "DHW energy divided by geometric residential floor area."),
    "total_res_eui": MetricSpec(
        "Total site energy", ("results", "total_site_kwh_m2"), "kWh/m²",
        "simulation period", "geometric residential floor area", 2,
        "Whole-site energy divided by geometric residential floor area; a "
        "comparison-oriented accounting ratio, not a residential end-use intensity."),
    "heating_cond_eui": MetricSpec(
        "Space heating", ("results", "space_heating_kwh_m2_conditioned"), "kWh/m²",
        "simulation period", "total conditioned floor area", 2,
        "Space-heating energy divided by total conditioned floor area."),
    "cooling_cond_eui": MetricSpec(
        "Space cooling", ("results", "cooling_kwh_m2_conditioned"), "kWh/m²",
        "simulation period", "total conditioned floor area", 2,
        "Space-cooling energy divided by total conditioned floor area."),
    "total_cond_eui": MetricSpec(
        "Total site energy", ("results", "total_site_kwh_m2_conditioned"), "kWh/m²",
        "simulation period", "total conditioned floor area", 2,
        "Whole-site energy divided by total conditioned floor area."),
    "residential_upper": MetricSpec(
        "Residential-attributed upper bound", ("results", "residential_site_kwh"),
        "kWh", "simulation period", "none", 1,
        "Total site energy after subtracting commercial lighting and equipment. "
        "HVAC and DHW are not separated by space use, so this is an upper bound."),
    "commercial_attributed": MetricSpec(
        "Commercial-attributed lighting and equipment",
        ("results", "terciario_site_kwh"), "kWh", "simulation period", "none", 1,
        "Only lighting and equipment tagged to commercial space types."),
    "hvac_carbon": MetricSpec(
        "HVAC operational emissions", ("carbon", "hvac_co2_t_yr"), "tCO₂",
        "simulation period", "none", 2,
        "Operational emissions associated with recorded HVAC final energy."),
    "site_carbon": MetricSpec(
        "Whole-site operational emissions", ("carbon", "total_site_co2_t_yr"),
        "tCO₂", "simulation period", "none", 2,
        "Operational emissions from final electricity and natural-gas energy; "
        "embodied carbon is excluded."),
}

_SUMMARY_MIRRORS = {
    spec.source[1]: spec.source
    for spec in METRICS.values()
    if spec.source[0] in {"results", "carbon"}
}

_CITATIONS = (
    (
        "EnergyPlus Input Output Reference, version 25.2",
        "https://bigladdersoftware.com/epx/docs/25-2/"
        "input-output-reference/index.html",
        "ABUPS/output-meter and end-use-subcategory terminology. The cited "
        "documentation is explicitly versioned; the report separately states "
        "the engine version read from the preserved log. Big Ladder provides "
        "the HTML rendering of the EnergyPlus release documentation.",
    ),
    (
        "EnergyPlus Essentials, version 25.2",
        "https://energyplus.readthedocs.io/en/v25.2.0/"
        "essentials/essentials.html",
        "General interpretation of Warning, Severe and Fatal diagnostics. The "
        "project-specific classified exception in this report does not replace "
        "EnergyPlus guidance.",
    ),
    (
        "Código Técnico de la Edificación, DB-HE (2017 edition)",
        "https://www.codigotecnico.org/pdf/Documentos/HE/DBHE_201706.pdf",
        "Reference DHW demand of 28 L/person/day at 60 °C. The model's 50 °C "
        "setpoint is a model implementation and is not asserted to be a direct "
        "CTE-equivalent temperature.",
    ),
    (
        "IDAE, La bomba de calor (2023, V11)",
        "https://www.idae.es/sites/default/files/documentos/publicaciones_idae/"
        "Guias_IDAE_La_Bomba_de_calor_2023_V11.pdf",
        "Operational conversion factors used by the preserved model: "
        "electricity 0.331 and natural gas 0.252 kgCO₂/kWh_final.",
    ),
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _finite(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _num(value: Any, digits: int = 2, unit: str = "") -> str:
    number = _finite(value)
    if number is None:
        return DASH
    text = f"{number:,.{digits}f}"
    return f'<span class="quantity">{text}<span class="unit">&thinsp;{_esc(unit)}</span></span>' if unit else text


def _integer(value: Any) -> str:
    return _num(value, 0)


def _yes_no(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return DASH


def _text(value: Any) -> str:
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, (list, tuple)):
        return _esc(", ".join(str(item) for item in value)) if value else DASH
    if isinstance(value, Mapping):
        return DASH
    return _esc(value)


def _basename(value: Any) -> str:
    if value is None or value == "":
        return DASH
    return _esc(Path(str(value)).name)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _value(document: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = document
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _metric(document: Mapping[str, Any], key: str, *, with_unit: bool = False) -> str:
    spec = METRICS[key]
    value = _value(document, spec.source)
    return _num(value, spec.precision, spec.unit if with_unit else "")


def _equivalent(left: Any, right: Any) -> bool:
    if type(left) is not type(right) and not (
        _finite(left) is not None and _finite(right) is not None
    ):
        return False
    a, b = _finite(left), _finite(right)
    if a is not None and b is not None:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    if (
        isinstance(left, (int, float)) and not isinstance(left, bool)
        and isinstance(right, (int, float)) and not isinstance(right, bool)
    ):
        # Two identically malformed copies are not a disagreement; the primary
        # field is still rejected by the completeness gate below.
        return (
            math.isnan(float(left)) and math.isnan(float(right))
        ) or left == right
    return left == right


# `model_builder` labels the lowest storey `Space 0 - Ground (commercial or
# buffer zone)` while it is building the geometry, before any use policy is
# applied, so the label identifies the ground storey without asserting what it
# ended up as.
_GROUND_SPACE_LABEL = "Ground (commercial or buffer zone)"


@dataclass(frozen=True)
class GroundStorey:
    """What the finished model does with the ground storey.

    Two preserved stages can disagree about it.  `layers.ground` is
    `apply_residential_ground`, which runs first.  `layers.mixed_use` is
    `apply_mixed_use_storeys`, which runs after it and re-types the lowest
    excess dwelling storeys back to conditioned Terciario - no dwelling
    thermostat, out of the floor-area basis.  Where the storey it took is the
    ground one, the second stage is what EnergyPlus actually simulated, and
    `summary.ground_use` is the input policy that led into it rather than the
    outcome.

    Reading only the first stage told 1 654 of the 24 983 finished Valencia
    buildings - 6.62 % - that their ground floor was a dwelling with its own
    thermostat, counted in the residential denominator, when the OSM handed to
    EnergyPlus says commercial, no thermostat, excluded.
    """

    initial: str
    converted: bool
    space_type: Any
    thermostat: Any
    in_floor_area_basis: Any
    conflict: str | None = None


def _ground_storey(stages: Mapping[str, Any],
                   summary: Mapping[str, Any]) -> GroundStorey:
    ground = _mapping(stages.get("ground"))
    mixed = _mapping(stages.get("mixed_use"))
    initial = str(summary.get("ground_use") or "")

    def as_recorded(conflict: str | None = None) -> GroundStorey:
        return GroundStorey(
            initial, False,
            ground.get("ground_space_type"),
            ground.get("ground_thermostat"),
            ground.get("in_floor_area_basis"),
            conflict,
        )

    if (_finite(mixed.get("storeys_converted")) or 0) <= 0:
        # Nothing was re-typed after the ground stage, so the ground stage is
        # the final word.  A ledger too old to carry `mixed_use` lands here as
        # well: a stage that is absent is not a stage that disagrees.
        return as_recorded()

    spaces = mixed.get("converted_spaces")
    if isinstance(spaces, (list, tuple)):
        if not any(_GROUND_SPACE_LABEL in str(name) for name in spaces):
            return as_recorded()  # a storey was converted, but not this one
        return GroundStorey(
            initial, True,
            mixed.get("space_type"),
            False,
            mixed.get("in_floor_area_basis"),
        )

    if initial.lower() != "residential":
        # The ground storey was already commercial; whichever storey the
        # mixed-use stage took, it does not change that.
        return as_recorded()

    # A conversion happened and the record does not say which storey it took,
    # while the earlier stage claims a dwelling ground.  The engine converts
    # upward from the lowest dwelling storey, so a residential ground would
    # have been first - but that is inferred from today's ordering rather than
    # read from this run's own evidence, and inferring it is how this project
    # has had to retract published statements before.  Say so instead.
    return as_recorded(
        "layers.ground records a residential ground storey while "
        "layers.mixed_use converted a storey without recording which one"
    )


def _evidence_conflicts(document: Mapping[str, Any]) -> list[str]:
    summary = _mapping(document.get("summary"))
    conflicts: list[str] = []
    for key, path in sorted(_SUMMARY_MIRRORS.items()):
        primary = _value(document, path)
        copied = summary.get(key)
        if primary is None or copied is None:
            continue
        if not _equivalent(primary, copied):
            conflicts.append(
                f"{'.'.join(path)} disagrees with summary.{key}"
            )
    profile = _mapping(summary.get("verified_profile"))
    metadata = _mapping(document.get("_metadata"))
    artifact_profile = _mapping(metadata.get("verified_profile"))
    left = profile.get("fingerprint")
    right = artifact_profile.get("fingerprint")
    if left and right and left != right:
        conflicts.append(
            "summary.verified_profile.fingerprint disagrees with "
            "verified_profile.json"
        )
    ground = _ground_storey(_mapping(document.get("layers")), summary)
    if ground.conflict:
        conflicts.append(ground.conflict)
    return conflicts


@dataclass(frozen=True)
class Period:
    kind: str
    label: str
    energy_unit: str
    carbon_unit: str
    complete: bool


def _period(row: Mapping[str, Any], stages: Mapping[str, Any]) -> Period:
    mode = row.get("run_mode")
    event = _mapping(stages.get("microclimate"))
    if mode == "microclimate_event" or event:
        days = row.get("event_days") or event.get("days")
        window = row.get("event_window") or event.get("window")
        parts = ["Microclimate event"]
        if window:
            parts.append(str(window))
        if _finite(days) is not None:
            parts.append(f"{int(days)} days")
        complete = bool(window) and _finite(days) is not None
        return Period(
            "event", " · ".join(parts) if complete else "Event period not fully recorded",
            "kWh/event", "tCO₂/event period", complete,
        )
    # The stock runner's ledger contract predates the explicit run_mode stamp:
    # absent mode means annual. A row is still required; without it the period
    # is not evidence-backed for this report.
    if row and (mode in (None, "", "annual")):
        return Period(
            "annual", "Annual simulation", "kWh/year", "tCO₂/year", True,
        )
    return Period(
        "unknown", "Period not recorded", "kWh / period not recorded",
        "tCO₂ / period not recorded", False,
    )


@dataclass(frozen=True)
class QaDecision:
    key: str
    label: str
    symbol: str
    explanation: str


def _qa_decision(
    summary: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    period: Period,
    conflicts: list[str],
    results: Mapping[str, Any],
    checks: Any,
) -> QaDecision:
    status = row.get("status")
    qa = summary.get("qa_all_passed")
    fatal = _finite(summary.get("fatals"))
    unexplained = _finite(summary.get("severes_unexplained"))
    benign = _finite(summary.get("severes_benign_shading_ems"))

    if conflicts:
        return QaDecision(
            "conflict", "EVIDENCE CONFLICT — DO NOT CITE", "✕",
            "Duplicate preserved evidence fields disagree. Resolve the "
            "conflict before citing any result.",
        )
    if (
        (status and status != "ok")
        or qa is False
        or (fatal is not None and fatal > 0)
        or (unexplained is not None and unexplained > 0)
    ):
        return QaDecision(
            "failed", "FAILED / DO NOT USE", "✕",
            "The run failed, a QA check failed, or an unexplained Severe/Fatal "
            "diagnostic was recorded.",
        )
    required = (
        results.get("total_site_kwh"),
        results.get("total_site_kwh_m2"),
        summary.get("res_area_m2"),
        summary.get("severes"),
        summary.get("severes_benign_shading_ems"),
        summary.get("severes_unexplained"),
        summary.get("fatals"),
    )
    if status == "ok" and (
        not period.complete
        or any(_finite(value) is None for value in required)
        or not isinstance(checks, list)
        or not checks
    ):
        return QaDecision(
            "incomplete", "INCOMPLETE EVIDENCE — DO NOT CITE", "!",
            "The record appears successful but lacks a core result, denominator "
            "or simulation-period statement.",
        )
    if status == "ok" and qa is True and (benign or 0) > 0:
        return QaDecision(
            "classified", "ACCEPTED WITH CLASSIFIED DIAGNOSTICS", "!",
            "All recorded QA checks passed and no unexplained Severe/Fatal "
            "diagnostic remains, but project-classified Severe messages are present.",
        )
    severes = _finite(summary.get("severes"))
    if status == "ok" and qa is True and (severes or 0) == 0 and (fatal or 0) == 0:
        return QaDecision(
            "pass", "PASS", "✓",
            "The result succeeded, all recorded QA checks passed, and no "
            "Severe or Fatal diagnostic was recorded.",
        )
    return QaDecision(
        "not-assessed", "NOT ASSESSED", "?",
        "The preserved record does not contain enough QA evidence for an "
        "overall assessment.",
    )


def _rows(
    pairs: Iterable[tuple[str, str]],
    *,
    caption: str,
    table_class: str = "definition-table",
) -> str:
    body = "".join(
        f'<tr><th scope="row">{_esc(label)}</th><td>{value}</td></tr>'
        for label, value in pairs
    )
    return (
        f'<table class="{_esc(table_class)}"><caption>{_esc(caption)}</caption>'
        f"<tbody>{body}</tbody></table>"
    )


def _section(
    number: str,
    title: str,
    note: str,
    body: str,
    *,
    section_id: str,
    class_name: str = "",
) -> str:
    classes = f' class="{_esc(class_name)}"' if class_name else ""
    return (
        f'<section id="{_esc(section_id)}"{classes}>'
        f'<h2><span class="section-number">{_esc(number)}</span> {_esc(title)}</h2>'
        f'<p class="section-note">{_esc(note)}</p>{body}</section>'
    )


def _table_scroll(table: str, label: str) -> str:
    return (
        f'<div class="table-scroll" role="region" aria-label="{_esc(label)}" '
        f'tabindex="0">{table}</div>'
    )


# The two strings `deep_building._BENIGN_SEVERE_MARKERS` requires, quoted here
# rather than imported: `building_report` deliberately imports nothing from the
# engine, which is what lets it be edited while a stock run is in flight.  If
# the engine's markers ever change, this sentence has to be updated with them.
_CLASSIFIED_SEVERE_MARKERS = (
    "Missing shade or blind layer in window construction",
    "EMS Actuator cannot be set",
)


def _classified_severe_note(summary: Mapping[str, Any]) -> str:
    """Say what the classified-Severe exception did on THIS building.

    The previous wording described the exception in the abstract and printed
    the same paragraph on every report, including buildings where nothing was
    classified, so a reader could not tell whether it had been applied here or
    how far.  Naming the count and the pattern makes the claim checkable
    against the preserved `eplusout.err`, and keeps it from being read as
    "EnergyPlus accepted these": EnergyPlus still reports them as Severe.
    """
    classified = _finite(summary.get("severes_benign_shading_ems"))
    if classified is None:
        extent = (
            "The number of project-classified Severe messages was not recorded "
            "for this building, so the extent of the exception cannot be "
            "stated here."
        )
    elif classified <= 0:
        extent = (
            "No Severe message on this building was classified under that "
            "exception."
        )
    else:
        noun = "message" if classified == 1 else "messages"
        pattern = " and ".join(
            f"&#8220;{_esc(marker)}&#8221;" for marker in _CLASSIFIED_SEVERE_MARKERS
        )
        extent = (
            f"On this building {_integer(classified)} Severe {noun} matched "
            f"that pattern, which a message block satisfies only by carrying "
            f"both {pattern}. EnergyPlus still reports them as Severe; the "
            "classification records why they were accepted here and does not "
            "change their EnergyPlus severity."
        )
    return (
        '<p class="prose"><strong>Classified Severe messages.</strong> '
        "EnergyPlus generally recommends correcting Severe diagnostics [2]. "
        "This project's accepted classification is a measured, project-specific "
        "exception for one recorded shading-control EMS message pattern; it is "
        "not a general redefinition of EnergyPlus severity and does not extend "
        f"to any other Severe message. {extent} Any Fatal or unexplained "
        "Severe makes the result unusable.</p>"
    )


def _qa_table(checks: Any) -> str:
    if not isinstance(checks, list) or not checks:
        return '<p class="missing-evidence">No individual QA checks were preserved.</p>'
    definitions = {
        # `run_simulation.py` compares `stats["res_area_m2"]` against the
        # EnergyPlus tabular value "Net Conditioned Building Area", and
        # `res_area_m2` follows the floor-area basis: it counts the ground
        # storey when that storey is a dwelling and drops it when the cadastral
        # mixed-use allocation converts it.  Calling the row "Conditioned floor
        # area" read as a contradiction of the total conditioned floor area
        # printed in the geometry section, which counts every conditioned
        # storey including one held outside the basis.
        "conditioned_area_m2": (
            "Residential floor area (EnergyPlus net conditioned basis)",
            "Cross-check of the model's residential floor area against the "
            "EnergyPlus Net Conditioned Building Area. Both sides follow the "
            "same floor-area basis, so a conditioned storey held outside that "
            "basis - a ground storey converted to the commercial regime, for "
            "example - is excluded from both, and this figure is smaller than "
            "the total conditioned floor area by that amount.",
            "±0.5%",
        ),
        "glazing_area_m2": (
            "Exterior opening area",
            "Cross-check of model opening area and EnergyPlus glazing area.",
            "±2%",
        ),
        "zone_count": (
            "Thermal-zone count",
            "Cross-check of model and EnergyPlus thermal-zone counts.",
            "Exact match",
        ),
        "unmet_hours": (
            "Occupied setpoint-not-met hours",
            "Recorded EnergyPlus comfort diagnostic against the project limit.",
            "≤500 h",
        ),
        "plausible_band_total_site_kwh_m2": (
            "Total site-energy plausibility band",
            "Method plausibility screen; this is not a model-to-EnergyPlus cross-check.",
            "Inside the recorded accepted interval",
        ),
        "event_days_simulated": (
            "Simulated event duration",
            "Cross-check of requested and simulated event days.",
            "Exact match",
        ),
    }
    body: list[str] = []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        key = str(check.get("check") or "unrecorded_check")
        label, description, criterion = definitions.get(
            key,
            (
                key.replace("_", " ").capitalize(),
                "Preserved project QA check.",
                _criterion(check.get("tolerance")),
            ),
        )
        if key == "unmet_hours":
            tolerance = _finite(check.get("tolerance"))
            if tolerance is not None:
                criterion = f"≤{tolerance:,.0f} h"
        passed = check.get("passed")
        result = (
            '<span class="result result--pass" aria-label="Passed">✓ PASS</span>'
            if passed is True
            else '<span class="result result--fail" aria-label="Failed">✕ FAIL</span>'
            if passed is False
            else '<span class="result result--unknown">? NOT ASSESSED</span>'
        )
        expected = _qa_value(check.get("model"), key)
        observed = _qa_value(check.get("eplus"), key)
        body.append(
            "<tr>"
            f'<th scope="row"><span class="check-name">{_esc(label)}</span>'
            f'<span class="check-description">{_esc(description)}</span>'
            f'<code class="technical-key">Recorded key: {_esc(key)}</code></th>'
            f'<td class="numeric">{expected}</td>'
            f'<td class="numeric">{observed}</td>'
            f"<td>{_esc(criterion)}</td><td>{result}</td></tr>"
        )
    if not body:
        return '<p class="missing-evidence">No valid individual QA checks were preserved.</p>'
    table = (
        '<table class="data-table qa-table"><caption>Recorded building-level '
        'quality-assurance checks and their acceptance criteria</caption>'
        "<thead><tr>"
        '<th scope="col">Check</th><th scope="col">Expected/model value</th>'
        '<th scope="col">Observed value or accepted interval</th>'
        '<th scope="col">Acceptance criterion</th><th scope="col">Result</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )
    return _table_scroll(table, "Building quality-assurance checks")


def _criterion(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "Recorded project criterion"
    if 0 < number < 1:
        return f"±{number * 100:g}%"
    if number == 0:
        return "Exact match"
    return f"≤{number:g}"


def _qa_value(value: Any, key: str) -> str:
    number = _finite(value)
    if number is not None:
        unit = "m²" if key in {"conditioned_area_m2", "glazing_area_m2"} else (
            "h" if key == "unmet_hours" else ""
        )
        return _num(number, 2 if not float(number).is_integer() else 0, unit)
    return _text(value)


def _method_sections(stages: Mapping[str, Any], summary: Mapping[str, Any],
                     parent_number: str) -> str:
    """Render the method subsections beneath their parent section.

    The parent is section 6 in a successful report and 7 in one that carries a
    recorded failure, so the child numbers cannot be constants: hard-coding
    `7.1`-`7.5` printed "6 Model preparation" above "7.1" and below
    "7 Provenance" on every successful report.
    """
    occupancy = _mapping(stages.get("occupancy"))
    glazing = _mapping(stages.get("ground_glazing"))
    mixed = _mapping(stages.get("mixed_use"))
    top = _mapping(stages.get("partial_top_storey"))
    dhw = _mapping(stages.get("dhw"))
    hvac = _mapping(stages.get("hvac"))
    metering = _mapping(stages.get("terciario_metering"))
    zoning = _mapping(stages.get("zoning"))
    frames = _mapping(stages.get("window_frames"))
    weather = _mapping(stages.get("weather"))

    storey = _ground_storey(stages, summary)
    initial_use = storey.initial.lower()
    if storey.converted:
        ground_text = (
            "The cadastral mixed-use allocation re-typed the ground storey to "
            "the verification anchor's conditioned Terciario regime: no "
            "dwelling thermostat, and excluded from the residential "
            "floor-area denominator. That is the state EnergyPlus simulated. "
            "The initial ground-use policy below is the input that led into "
            "the conversion, not its outcome."
        )
        opening_term = "Commercial ground-storey glazing"
        final_use = "commercial (converted by cadastral mixed-use allocation)"
    elif initial_use == "residential":
        ground_text = (
            "The ground storey is represented as a dwelling space, receives its "
            "own thermostat where recorded, and is included in the residential "
            "floor-area denominator."
        )
        opening_term = "Ground-storey openings"
        final_use = "residential"
    elif initial_use:
        ground_text = (
            "The ground storey is represented with the verification anchor's "
            "Terciario regime: conditioned commercial space, excluded from the "
            "residential floor-area denominator."
        )
        opening_term = "Commercial ground-storey glazing"
        final_use = "commercial"
    else:
        ground_text = "The preserved evidence does not state the ground-storey use."
        opening_term = "Ground-storey openings"
        final_use = ""

    source = str(summary.get("occupants_source") or "")
    if "imputed.cluster_median" in source:
        population_text = (
            "The Padrón value was zero or unavailable; model occupancy was "
            "imputed from the cluster-median people-per-dwelling policy."
        )
    elif source:
        population_text = (
            "The model started from the preserved Padrón resident count. Padrón "
            "is an administrative population record, not real-time occupancy "
            "measurement."
        )
    else:
        population_text = "The occupancy source was not recorded."
    plausibility = _mapping(occupancy.get("plausibility"))
    if str(plausibility.get("status")) == "implausible_dense_capped":
        population_text += (
            " The recorded count exceeded the density floor and was capped "
            "before simulation; both original and applied counts remain visible."
        )

    sections = [
        (
            "1", "Ground-storey use and openings", ground_text,
            [
                ("Initial ground-use policy", _text(summary.get("ground_use"))),
                ("Final modelled ground-storey use", _text(final_use)),
                ("Space type", _text(storey.space_type)),
                ("Own thermostat", _yes_no(storey.thermostat)),
                ("Included in residential area denominator", _yes_no(storey.in_floor_area_basis)),
                (opening_term, _integer(glazing.get("ground_windows"))),
                ("Ground-storey glass area", _num(glazing.get("ground_glass_area_m2"), 1, "m²")),
            ],
        ),
        (
            "2", "Storey-use and partial-storey policy",
            "Storey counts describe different concepts and are therefore shown "
            "separately. A partial upper storey scales recorded internal loads "
            "but does not alter the preserved envelope geometry.",
            [
                ("Total modelled storeys", _integer(summary.get("n_floors_total"))),
                ("Residential levels in model", _integer(mixed.get("residential_storeys"))),
                ("Residential storeys above ground (legacy builder field)", _integer(summary.get("n_floors_residential"))),
                ("Effective residential storeys", _num(summary.get("residential_storeys_effective"), 3)),
                ("Upper-storey occupied fraction", _num(top.get("fraction"), 3)),
                ("Storeys converted to commercial use", _integer(mixed.get("storeys_converted"))),
                ("Raw model identifier", (
                    f'<code>{_esc(top.get("space"))}</code>' if top.get("space") else DASH
                )),
            ],
        ),
        (
            "3", "Occupancy and ventilation", population_text,
            [
                ("Padrón residents", _num(occupancy.get("padron_occupants"), 1)),
                ("Residents applied to model", _num(occupancy.get("occupants"), 1)),
                ("Occupancy evidence source", _text(summary.get("occupants_source"))),
                ("Zero-population policy", _text(summary.get("zero_policy"))),
                ("Residential area per applied person", _num(occupancy.get("m2_per_person"), 1, "m²/person")),
                ("Applied people density", _num(occupancy.get("people_per_m2"), 6, "people/m²")),
                ("Plausibility classification", _text(plausibility.get("status"))),
                ("Plausibility note", _text(plausibility.get("note"))),
            ],
        ),
        (
            "4", "DHW, HVAC and end-use tagging",
            "The CTE reference demand is 28 L/person/day at 60 °C [3]. The "
            "preserved model applies its separately recorded 50 °C setpoint; "
            "the two temperatures are not presented as directly equivalent. "
            "Commercial tags describe meter configuration, not proof of "
            "commercial consumption.",
            [
                ("Reference DHW demand", _num(dhw.get("dhw_litres_per_person_day"), 1, "L/person/day")),
                ("Model DHW setpoint", _num(dhw.get("dhw_setpoint_c"), 1, "°C")),
                ("DHW draw objects", _integer(dhw.get("dhw_equipment_count"))),
                ("HVAC system", _text(hvac.get("hvac_system"))),
                ("HVAC zones", _integer(hvac.get("hvac_zones"))),
                ("Commercial spaces", _integer(metering.get("spaces"))),
                ("Lighting objects tagged for commercial subcategory", _integer(metering.get("tagged_lights"))),
                ("Equipment objects tagged for commercial subcategory", _integer(metering.get("tagged_equipment"))),
            ],
        ),
        (
            "5", "Envelope, zoning and climate",
            "Envelope, zoning and weather settings are listed as recorded model "
            "assumptions. They are not measurements of the completed building.",
            [
                ("Window frame", _text(frames.get("window_frame"))),
                ("Frame width", _num(frames.get("frame_width_m"), 3, "m")),
                ("Thermal-zoning scheme", _text(zoning.get("scheme"))),
                ("Single-zone footprint threshold", _num(zoning.get("single_zone_threshold_m2"), 1, "m²")),
                ("Known zoning limitation", _text(zoning.get("note"))),
                ("Climate package", _text(weather.get("climate") or summary.get("climate"))),
                ("Weather file", _basename(weather.get("epw_file"))),
                ("Climate fingerprint", _code(weather.get("climate_fingerprint") or summary.get("climate_fingerprint"))),
            ],
        ),
    ]
    return "".join(
        f'<article class="method"><h3>{parent_number}.{number} {_esc(title)}</h3>'
        f'<p>{_esc(note)}</p>'
        f'{_rows(rows, caption=title)}</article>'
        for number, title, note, rows in sections
        if any(value != DASH for _, value in rows)
    )


def _code(value: Any, *, short: bool = False) -> str:
    if value is None or value == "":
        return DASH
    text = str(value)
    shown = f"{text[:16]}…" if short and len(text) > 16 else text
    return f"<code>{_esc(shown)}</code>"


def _source_name(value: Any) -> str:
    return _basename(value)


def _reference_list(profile: Mapping[str, Any]) -> str:
    items = []
    for index, (title, url, note) in enumerate(_CITATIONS, start=1):
        items.append(
            f'<li id="ref-{index}"><a href="{_esc(url)}">{_esc(title)}</a>. '
            f"{_esc(note)}</li>"
        )
    profile_id = profile.get("profile_id")
    fingerprint = profile.get("fingerprint")
    verified_on = profile.get("verified_on")
    anchor = profile.get("verified_against")
    items.append(
        '<li id="ref-5"><span class="reference-title">Unpublished '
        'research-group evidence.</span> '
        f"Profile {_code(profile_id)}, verified {_text(verified_on)} against "
        f"{_text(anchor)}; profile fingerprint {_code(fingerprint)}. "
        "No publication or bibliographic metadata is inferred beyond the "
        "preserved record.</li>"
    )
    return f'<ol class="references">{"".join(items)}</ol>'


def _energy_table(document: Mapping[str, Any], period: Period) -> str:
    results = _mapping(document.get("results"))
    rows = [
        ("heating", "space_heating_kwh_m2_conditioned"),
        ("cooling", "cooling_kwh_m2_conditioned"),
        ("dhw", None),
        ("total", "total_site_kwh_m2_conditioned"),
    ]
    body = []
    for absolute_key, conditioned_key in rows:
        spec = METRICS[absolute_key]
        res_key = {
            "heating": "heating_res_eui",
            "cooling": "cooling_res_eui",
            "dhw": "dhw_res_eui",
            "total": "total_res_eui",
        }[absolute_key]
        conditioned = (
            _num(results.get(conditioned_key), 2) if conditioned_key else DASH
        )
        body.append(
            "<tr>"
            f'<th scope="row">{_esc(spec.label)}</th>'
            f'<td class="numeric">{_metric(document, absolute_key)}</td>'
            f'<td class="numeric">{_metric(document, res_key)}</td>'
            f'<td class="numeric">{conditioned}</td></tr>'
        )
    table = (
        '<table class="data-table energy-table"><caption>Preserved final site-energy '
        f'results for the {_esc(period.label.lower())}</caption><thead><tr>'
        '<th scope="col">End use</th>'
        f'<th scope="col">Energy ({_esc(period.energy_unit)})</th>'
        '<th scope="col">End-use energy / geometric residential area '
        '(kWh/m² per period)</th>'
        '<th scope="col">End-use energy / conditioned area '
        '(kWh/m² per period)</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )
    return _table_scroll(table, "Energy and emissions results")


_STYLE = """
:root {
  --ink:#17201c; --muted:#4e5d56; --line:#c7d0ca; --soft:#f4f6f3;
  --accent:#185d55; --link:#075e54; --good:#185c36; --good-bg:#e8f4eb;
  --warn:#6f4300; --warn-bg:#fff1ce; --bad:#7a1f1f; --bad-bg:#fdeaea;
  --unknown:#39464e; --unknown-bg:#edf1f3;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body {
  margin:0; padding:32px 24px 72px; color:var(--ink); background:#fff;
  font:16px/1.62 Charter,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  text-rendering:optimizeLegibility;
}
main { width:min(100%, 1080px); margin:0 auto; }
a { color:var(--link); text-decoration-thickness:.08em; text-underline-offset:.15em; }
a:focus-visible, summary:focus-visible, .table-scroll:focus-visible {
  outline:3px solid #b46200; outline-offset:3px; border-radius:2px;
}
.skip-link {
  position:absolute; left:12px; top:-80px; padding:10px 14px; z-index:2;
  color:#fff; background:#17201c; font:700 14px/1.2 system-ui,sans-serif;
}
.skip-link:focus { top:12px; }
.report-header { margin:0 0 28px; padding-bottom:22px; border-bottom:2px solid var(--ink); }
.eyebrow, h1, h2, h3, .status, nav, th, caption, .meta-label, .kpi-label,
.technical-key, .result, summary, footer {
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
}
.eyebrow {
  margin:0 0 8px; color:var(--accent); font-size:13px; font-weight:750;
  letter-spacing:.09em; text-transform:uppercase;
}
h1 { margin:0; font-size:clamp(28px,4vw,32px); line-height:1.15; letter-spacing:-.025em; }
h1 code { font-size:.88em; }
.header-meta {
  display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px 24px;
  margin:18px 0 0; padding:0;
}
.header-meta div { min-width:0; }
.meta-label {
  display:block; color:var(--muted); font-size:13px; font-weight:700;
  letter-spacing:.035em; text-transform:uppercase;
}
.header-meta dd { margin:2px 0 0; font-size:15px; overflow-wrap:anywhere; }
.status {
  display:flex; align-items:flex-start; gap:10px; margin-top:18px; padding:12px 14px;
  border:2px solid currentColor; font-size:14px; font-weight:800; line-height:1.35;
}
.status-symbol { flex:0 0 auto; font-size:18px; line-height:1; }
.status small { display:block; margin-top:3px; font:13px/1.45 Charter,Georgia,serif; font-weight:400; }
.status--pass { color:var(--good); background:var(--good-bg); }
.status--classified, .status--incomplete { color:var(--warn); background:var(--warn-bg); }
.status--failed, .status--conflict { color:var(--bad); background:var(--bad-bg); }
.status--not-assessed { color:var(--unknown); background:var(--unknown-bg); }
.report-nav {
  display:flex; flex-wrap:wrap; gap:8px 18px; margin:0 0 28px; padding:13px 15px;
  border:1px solid var(--line); background:var(--soft); font-size:14px; font-weight:700;
}
.report-nav a { text-decoration:none; }
.report-nav a:hover { text-decoration:underline; }
section { scroll-margin-top:20px; margin:0 0 34px; }
h2 {
  margin:0 0 8px; font-size:20px; line-height:1.25; letter-spacing:-.01em;
  padding-bottom:7px; border-bottom:1px solid var(--line);
}
h3 { margin:24px 0 7px; font-size:16px; line-height:1.35; }
.section-number { color:var(--accent); }
.section-note, .prose, .method > p, .limitation-list, .summary-list {
  max-width:72ch;
}
.section-note { margin:0 0 16px; color:var(--muted); font-size:14px; }
.prose, .method > p { margin:0 0 14px; }
.summary-list, .limitation-list { margin:0; padding-left:22px; }
.summary-list li, .limitation-list li { margin:7px 0; }
.kpis {
  display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px;
  margin:18px 0 0; border:1px solid var(--line); background:var(--line);
}
.kpis div { min-width:0; padding:14px 15px; background:#fff; }
.kpi-label {
  display:block; color:var(--muted); font-size:13px; font-weight:700;
  letter-spacing:.035em; text-transform:uppercase;
}
.kpis strong {
  display:block; margin:4px 0 2px; font:700 20px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;
}
.kpis small { display:block; color:var(--muted); font-size:13px; line-height:1.4; }
table { width:100%; border-collapse:collapse; font-size:14px; line-height:1.45; }
caption { padding:0 0 8px; text-align:left; color:var(--muted); font-size:13px; font-weight:650; }
th, td { padding:9px 11px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
thead th {
  color:#34453d; background:#edf1ed; font-size:13px; font-weight:750;
  border-bottom:2px solid #87958d;
}
.definition-table th[scope=row] { width:40%; color:#33443d; font-weight:700; }
td, .numeric { font-variant-numeric:tabular-nums lining-nums; }
.numeric { text-align:right; white-space:nowrap; }
.quantity { white-space:nowrap; }
.unit { color:var(--muted); }
.table-scroll { max-width:100%; overflow-x:auto; margin:12px 0; }
.data-table { min-width:780px; }
.qa-table { min-width:960px; }
.check-name, .check-description, .technical-key { display:block; }
.check-description { margin-top:3px; max-width:40ch; color:var(--muted); font:13px/1.4 Charter,Georgia,serif; font-weight:400; }
code, .technical-key {
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  overflow-wrap:anywhere;
}
code { padding:.06em .28em; background:#edf0ed; font-size:.88em; }
.technical-key { margin-top:4px; color:#52615a; background:transparent; font-size:11px; font-weight:500; }
.result { display:inline-block; font-size:12px; font-weight:850; white-space:nowrap; }
.result--pass { color:var(--good); }
.result--fail { color:var(--bad); }
.result--unknown { color:var(--unknown); }
.evidence-alert {
  margin:0 0 18px; padding:13px 15px; border-left:5px solid var(--bad);
  color:var(--bad); background:var(--bad-bg); font-weight:700;
}
.evidence-alert ul { margin:5px 0 0; }
.method { padding:0 0 17px; border-bottom:1px solid var(--line); break-inside:avoid; }
.method:last-child { border-bottom:0; }
.references { max-width:82ch; padding-left:26px; }
.references li { margin:0 0 12px; padding-left:4px; }
.reference-title { font-weight:700; }
details.raw-files { margin-top:22px; border:1px solid var(--line); background:var(--soft); }
details.raw-files summary { padding:12px 14px; cursor:pointer; font-size:14px; font-weight:750; }
details.raw-files p { margin:0; padding:0 14px 12px; max-width:72ch; color:var(--muted); font-size:13px; }
details.raw-files nav { display:flex; flex-wrap:wrap; gap:8px 16px; padding:0 14px 14px; font-size:14px; }
.print-raw-files { display:none; }
footer { margin-top:34px; padding-top:14px; border-top:1px solid var(--line); color:var(--muted); font-size:13px; }
@media (max-width:720px) {
  body { padding:22px 16px 52px; }
  .header-meta { grid-template-columns:1fr; gap:9px; }
  .kpis { grid-template-columns:1fr; }
  .definition-table, .definition-table tbody, .definition-table tr,
  .definition-table th, .definition-table td { display:block; width:100%; }
  .definition-table tr { padding:10px 0; border-bottom:1px solid var(--line); }
  .definition-table th, .definition-table td { padding:1px 0; border:0; }
  .definition-table td { margin-top:3px; }
}
@page { size:A4 portrait; margin:14mm 13mm 16mm; }
@media print {
  html { scroll-behavior:auto; }
  body { padding:0; font-size:10.5pt; line-height:1.48; color:#000; }
  main { width:auto; max-width:none; }
  .skip-link, .report-nav { display:none; }
  .report-header { margin-bottom:16pt; }
  h1 { font-size:20pt; }
  h2 { font-size:14pt; break-after:avoid; }
  h3 { font-size:11.5pt; break-after:avoid; }
  .section-note, table { font-size:9.5pt; }
  section { margin-bottom:20pt; }
  .status { color:#000 !important; background:#fff !important; border-color:#000; }
  .kpis { grid-template-columns:repeat(3,1fr); }
  .kpis strong { font-size:13pt; }
  .kpis small, .kpi-label { font-size:8.5pt; }
  .table-scroll { overflow:visible; }
  .data-table, .qa-table { min-width:0; }
  .definition-table { display:table; width:100%; }
  .definition-table tbody { display:table-row-group; }
  .definition-table tr { display:table-row; padding:0; border:0; }
  .definition-table th, .definition-table td {
    display:table-cell; width:auto; padding:6pt 7pt;
    border-bottom:1px solid var(--line);
  }
  .definition-table th[scope=row] { width:40%; }
  thead { display:table-header-group; }
  tr, .method, .kpis > div { break-inside:avoid; }
  a { color:#000; text-decoration:underline; }
  details.raw-files { display:block; }
  details.raw-files summary { display:none; }
  details.raw-files:not([open]) > :not(summary) { display:block !important; }
  details.raw-files nav { display:flex !important; }
  details.raw-files { display:none; }
  .print-raw-files {
    display:block; margin-top:12pt; padding-top:8pt; border-top:1px solid var(--line);
    break-inside:avoid;
  }
  .print-raw-files h3 { margin-top:0; }
  .print-raw-files nav { display:flex; flex-direction:column; gap:3pt; font-size:9.5pt; }
}
"""


def render(
    layers: Mapping[str, Any],
    *,
    run: str,
    reference: str,
    ledger_row: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Render one self-contained academic HTML report."""
    if not isinstance(layers, Mapping):
        raise TypeError("layers must be a mapping")
    summary = _mapping(layers.get("summary"))
    results = _mapping(layers.get("results"))
    carbon = _mapping(layers.get("carbon"))
    stages = _mapping(layers.get("layers"))
    row = _mapping(ledger_row)
    profile = _mapping(summary.get("verified_profile"))
    reference = str(reference)
    recorded_ref = summary.get("refparcela")
    row_ref = row.get("refparcela")
    if recorded_ref and str(recorded_ref) != reference:
        raise EvidenceIdentityError(
            f"route reference {reference!r} disagrees with summary.refparcela "
            f"{recorded_ref!r}"
        )
    if row_ref and str(row_ref) != reference:
        raise EvidenceIdentityError(
            f"route reference {reference!r} disagrees with ledger refparcela "
            f"{row_ref!r}"
        )

    document = dict(layers)
    document["_metadata"] = dict(metadata or {})
    conflicts = _evidence_conflicts(document)
    period = _period(row, stages)
    decision = _qa_decision(
        summary, row, period=period, conflicts=conflicts, results=results,
        checks=layers.get("qa"),
    )
    display_ref = str(recorded_ref or reference)

    conflict_alert = ""
    if conflicts:
        conflict_alert = (
            '<aside class="evidence-alert" role="alert"><strong>Evidence '
            'conflict detected.</strong><ul>'
            + "".join(f"<li>{_esc(item)}</li>" for item in conflicts)
            + "</ul></aside>"
        )

    interpretation = _section(
        "1", "Interpretation summary",
        "Concise reading of the preserved result and its fitness for academic use.",
        conflict_alert
        + '<ul class="summary-list">'
        f"<li><strong>Result.</strong> {_esc(_result_sentence(row, decision))}</li>"
        f"<li><strong>Building scope.</strong> {_integer(summary.get('n_floors_total'))} "
        f"modelled storeys; {_num(summary.get('res_area_m2'), 1, 'm²')} geometric "
        "residential floor area and "
        f"{_num(summary.get('total_conditioned_area_m2'), 1, 'm²')} total conditioned area.</li>"
        f"<li><strong>Energy.</strong> {_metric(document, 'total_res_eui', with_unit=True)} "
        "whole-site energy per geometric residential floor area for the "
        f"{_esc(period.label.lower())}. This is an accounting ratio, not an "
        "isolated residential end-use intensity.</li>"
        f"<li><strong>QA.</strong> {_esc(decision.explanation)}</li>"
        f"<li><strong>Academic use.</strong> {_esc(_use_sentence(decision))}</li>"
        "</ul>"
        '<div class="kpis">'
        f'<div><span class="kpi-label">Period</span><strong>{_esc(period.kind.upper())}</strong>'
        f"<small>{_esc(period.label)}</small></div>"
        f'<div><span class="kpi-label">Total site energy</span><strong>{_metric(document, "total_res_eui")}</strong>'
        "<small>kWh/m² per period · geometric residential denominator</small></div>"
        f'<div><span class="kpi-label">Operational emissions</span><strong>{_metric(document, "site_carbon")}</strong>'
        f"<small>{_esc(period.carbon_unit)}</small></div></div>",
        section_id="interpretation",
    )

    limitations = _section(
        "2", "Scope and limitations",
        "Boundaries that must accompany any quotation of the numerical results.",
        '<ul class="limitation-list">'
        "<li>This is a building-energy simulation, not a utility-bill "
        "measurement, calibrated prediction, certification rating or record of "
        "actual occupant behaviour.</li>"
        "<li>Energy is final site energy. It is not primary energy. Whole-site "
        "energy divided by residential area is reported only as a transparent "
        "comparison-oriented accounting ratio.</li>"
        "<li>The residential-attributed value is an upper bound: only "
        "commercial lighting and equipment are subtracted; HVAC and DHW cannot "
        "be partitioned by space use from the preserved end-use meters.</li>"
        "<li>Operational emissions use 0.331 kgCO₂/kWh_final for electricity and "
        "0.252 kgCO₂/kWh_final for natural gas [4]. They are model-derived, not "
        "measured, and exclude embodied carbon.</li>"
        "<li>Profile verification means that the modelling profile was checked "
        "against the EdiPluriP04 verification anchor [5]. It does not mean this "
        "specific building was validated against measured consumption. "
        "Building-specific occupancy, shading, geometry and openings remain "
        "production inputs recorded in this evidence.</li>"
        "</ul>",
        section_id="limitations",
    )

    energy_table = _energy_table(document, period)
    cadastral_ratio = DASH
    cadastral_area = _finite(summary.get("tipo15_res_area_m2"))
    # No new physical result is derived. This optional presentation conversion
    # is intentionally not performed unless it was preserved by the producer.
    if "total_site_kwh_m2_cadastral" in results:
        cadastral_ratio = _num(results.get("total_site_kwh_m2_cadastral"), 2, "kWh/m² per period")
    energy_details = _rows(
        [
            ("Geometric residential floor area", _num(summary.get("res_area_m2"), 1, "m²")),
            ("Total conditioned floor area", _num(summary.get("total_conditioned_area_m2"), 1, "m²")),
            ("Cadastral Tipo15 dwelling area", _num(cadastral_area, 1, "m²")),
            ("Preserved cadastral energy ratio", cadastral_ratio),
            ("Residential-attributed upper bound", _metric(document, "residential_upper", with_unit=True)),
            ("Commercial-attributed lighting and equipment", _metric(document, "commercial_attributed", with_unit=True)),
            ("Preserved attribution method", _text(results.get("residential_split_basis"))),
            ("HVAC operational emissions", (
                f'{_metric(document, "hvac_carbon")} <span class="unit">{_esc(period.carbon_unit)}</span>'
            )),
            ("Whole-site operational emissions", (
                f'{_metric(document, "site_carbon")} <span class="unit">{_esc(period.carbon_unit)}</span>'
            )),
        ],
        caption="Area denominators, attribution boundary and operational emissions",
    )
    energy = _section(
        "3", "Energy and emissions results",
        "Primary physical values are read from results and carbon. Summary "
        "duplicates are used only for evidence-consistency checks. ABUPS totals "
        "come from EnergyPlus output meters; subcategory splits come from "
        "user-defined end-use subcategories [1].",
        energy_table
        + energy_details
        + '<p class="prose"><strong>Presentation rule.</strong> Physical results '
        "are not recomputed. The report only formats preserved numbers, applies "
        "explicit unit and period labels, and checks duplicated evidence for "
        "consistency. A measured zero remains <strong>0</strong>; absent, "
        "non-numeric, boolean, NaN or infinite values are shown as an em dash.</p>",
        section_id="energy",
    )

    geometry = _section(
        "4", "Building geometry and occupancy",
        "Preserved model geometry, area denominators and administrative "
        "population evidence. Padrón is not real-time occupancy measurement.",
        _rows(
            [
                ("Cadastral reference", _code(display_ref)),
                ("Footprint", _num(summary.get("footprint_m2"), 1, "m²")),
                ("Total modelled storeys", _integer(summary.get("n_floors_total"))),
                ("Residential storeys above ground (legacy builder field)", _integer(summary.get("n_floors_residential"))),
                ("Effective residential storeys", _num(summary.get("residential_storeys_effective"), 3)),
                ("Ground-storey use", _text(summary.get("ground_use"))),
                ("Geometric residential floor area", _num(summary.get("res_area_m2"), 1, "m²")),
                ("Cadastral Tipo15 dwelling area", _num(summary.get("tipo15_res_area_m2"), 1, "m²")),
                ("Total conditioned floor area", _num(summary.get("total_conditioned_area_m2"), 1, "m²")),
                ("Padrón residents", _num(summary.get("padron_occupants"), 1)),
                ("Residents applied to model", _num(summary.get("occupants_applied"), 1)),
                ("Occupancy evidence source", _text(summary.get("occupants_source"))),
                ("Windows", _integer(summary.get("n_windows"))),
                ("Balcony doors", _integer(summary.get("n_balcony_doors"))),
                ("Opening area", _num(summary.get("window_area_m2"), 1, "m²")),
                ("Party-wall surfaces", _integer(summary.get("n_party_surfaces"))),
                ("Neighbour-shading surfaces", _integer(summary.get("n_shading_surfaces"))),
            ],
            caption="Geometry, use, area and occupancy evidence",
        ),
        section_id="geometry",
    )

    qa = _section(
        "5", "Quality assurance and diagnostics",
        "Cross-checks and method plausibility screens are distinguished. Status "
        "is expressed with text and a symbol, never colour alone.",
        _qa_table(layers.get("qa"))
        # `OutputsPage` links the READABLE EVIDENCE entry "Warnings and
        # errors" at `#diagnostics`, which nothing emitted: the fragment
        # resolved to no element and the browser stayed at the top of the
        # report - the same defect as the `#overview` link, found by
        # checking every link target rather than the one that was reported.
        + '<h3 id="diagnostics">5.1 EnergyPlus diagnostic counts</h3>'
        + _rows(
            [
                ("Warnings", _integer(summary.get("warnings"))),
                ("Severe, total", _integer(summary.get("severes"))),
                ("Severe, project-classified", _integer(summary.get("severes_benign_shading_ems"))),
                ("Severe, unexplained", _integer(summary.get("severes_unexplained"))),
                ("Fatal", _integer(summary.get("fatals"))),
                ("Occupancy plausibility", _text(summary.get("occupancy_plausibility"))),
            ],
            caption="Preserved EnergyPlus and project diagnostic counts",
        )
        + _classified_severe_note(summary),
        section_id="quality",
    )

    failure = ""
    if row.get("status") and row.get("status") != "ok":
        failure = _section(
            "6", "Recorded failure",
            "Failure text is displayed as preserved; no diagnosis is inferred.",
            _rows(
                [
                    ("Run status", _text(row.get("status"))),
                    ("Reason", _text(row.get("reason"))),
                    ("Message", _text(row.get("message") or row.get("error"))),
                ],
                caption="Preserved failure evidence",
            ),
            section_id="failure",
        )
        methods_number = "7"
        provenance_number = "8"
        references_number = "9"
    else:
        methods_number = "6"
        provenance_number = "7"
        references_number = "8"

    methods = _section(
        methods_number, "Model preparation and assumptions",
        "Visible methodological record. Raw model identifiers are labelled as "
        "such and are not used as academic prose.",
        _method_sections(stages, summary, methods_number),
        section_id="methods",
    )

    metadata_map = _mapping(metadata)
    engine_version = metadata_map.get("energyplus_version")
    profile_fingerprint = profile.get("fingerprint")
    locked_hashes = _mapping(profile.get("locked_source_hashes"))
    live_hashes = _mapping(profile.get("live_source_hashes"))
    hash_rows: list[tuple[str, str]] = []
    for name in sorted(set(locked_hashes) | set(live_hashes)):
        locked, live = locked_hashes.get(name), live_hashes.get(name)
        state = "matches frozen source" if locked and locked == live else "mismatch or not recorded"
        hash_rows.append(
            (
                f"Source: {Path(name).name}",
                f"{_code(locked)} <span class='unit'>({state})</span>",
            )
        )
    identity_rows = [
        ("Run name", _code(run)),
        ("Run identity fingerprint", _code(row.get("run_identity"))),
        ("Profile", _code(profile.get("profile_id"))),
        ("Profile schema version", _text(profile.get("schema_version"))),
        ("Profile fingerprint", _code(profile_fingerprint)),
        ("Profile verified on", _text(profile.get("verified_on"))),
        ("Verification anchor", _text(profile.get("verified_against"))),
        ("EnergyPlus version from preserved log", _text(engine_version)),
        ("Climate package", _text(summary.get("climate"))),
        ("Climate fingerprint", _code(row.get("climate_fingerprint") or summary.get("climate_fingerprint"))),
        ("Template fingerprint", _code(row.get("template_fingerprint"))),
        ("Policy fingerprint", _code(row.get("policy_fingerprint"))),
        ("Stock-source fingerprint", _code(row.get("stock_source_fingerprint"))),
        ("Runner schema", _text(row.get("runner_schema"))),
        ("Prepared target dataset", _source_name(summary.get("target_gis_path"))),
        ("Shading-context dataset", _source_name(summary.get("context_gis_path"))),
    ]
    provenance = _section(
        provenance_number, "Provenance and reproducibility",
        "Logical dataset names, file names and preserved fingerprints are "
        "reported. Host-specific absolute paths are intentionally omitted.",
        _rows(identity_rows, caption="Run, profile, engine and data identity")
        + (
            "<h3>Frozen source fingerprints</h3>"
            + _rows(hash_rows, caption="Frozen and live modelling-source identity")
            if hash_rows else ""
        ),
        section_id="provenance",
    )

    base = (
        f"/api/stock/runs/{quote(run, safe='')}/buildings/"
        f"{quote(reference, safe='')}"
    )
    links = (
        f'<a href="{_esc(base)}/eplustbl.htm">EnergyPlus result tables '
        '(original HTML)</a>'
        f'<a href="{_esc(base)}/deep_layers.json">Model layers and QA '
        '(preserved JSON)</a>'
        f'<a href="{_esc(base)}/model_python.osm">OpenStudio model '
        '(preserved OSM)</a>'
        f'<a href="{_esc(base)}/eplusout.err">EnergyPlus diagnostic log</a>'
        f'<a href="{_esc(base)}/verified_profile.json">Verified profile '
        '(preserved JSON)</a>'
    )
    references = _section(
        references_number, "References and original technical evidence",
        "Numbered public technical sources are followed by the internal, "
        "unpublished verification record. No bibliographic facts are invented.",
        _reference_list(profile)
        + '<details class="raw-files"><summary>Original technical files</summary>'
        '<p>These source files are preserved for specialist software and '
        'forensic audit. They are intentionally secondary to the readable '
        'evidence above. In print they remain visible as named evidence links.</p>'
        f'<nav aria-label="Original technical evidence">{links}</nav></details>'
        '<div class="print-raw-files" aria-label="Original technical evidence '
        'for print"><h3>Original technical evidence links</h3>'
        f'<nav>{links}</nav></div>',
        section_id="references",
    )

    status = (
        f'<div class="status status--{_esc(decision.key)}" role="status">'
        f'<span class="status-symbol" aria-hidden="true">{_esc(decision.symbol)}</span>'
        f'<span>{_esc(decision.label)}<small>{_esc(decision.explanation)}</small></span>'
        "</div>"
    )
    navigation = (
        '<nav class="report-nav" aria-label="Report sections">'
        '<a href="#interpretation">Interpretation</a>'
        '<a href="#limitations">Limitations</a>'
        '<a href="#energy">Energy and emissions</a>'
        '<a href="#geometry">Geometry and occupancy</a>'
        '<a href="#quality">QA and diagnostics</a>'
        '<a href="#methods">Model preparation</a>'
        '<a href="#provenance">Provenance</a>'
        '<a href="#references">References</a></nav>'
    )
    header = (
        '<header class="report-header">'
        '<p class="eyebrow">Readable building evidence</p>'
        f'<h1>Building energy simulation report · <code>{_esc(display_ref)}</code></h1>'
        '<dl class="header-meta">'
        f'<div><dt class="meta-label">Cadastral reference</dt><dd>{_code(display_ref)}</dd></div>'
        f'<div><dt class="meta-label">Run</dt><dd>{_code(run)}</dd></div>'
        f'<div><dt class="meta-label">Simulation period</dt><dd>{_esc(period.label)}</dd></div>'
        f"</dl>{status}</header>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(display_ref)} · readable building report</title>"
        f"<style>{_STYLE}</style></head><body>"
        '<a class="skip-link" href="#report-content">Skip to report content</a>'
        f'<main id="report-content">{header}{navigation}{interpretation}'
        f"{limitations}{energy}{geometry}{qa}{failure}{methods}{provenance}{references}"
        '<footer>This report formats preserved evidence for scholarly review. '
        "It does not alter the simulation, frozen sources or run outputs.</footer>"
        "</main></body></html>\n"
    )


def _result_sentence(row: Mapping[str, Any], decision: QaDecision) -> str:
    status = row.get("status")
    if decision.key in {"failed", "conflict", "incomplete"}:
        return decision.explanation
    if status == "ok":
        return "The simulation completed and produced a preserved energy result."
    if status:
        return "The run did not produce a usable building result."
    return "A run-level completion status was not preserved with this evidence."


def _use_sentence(decision: QaDecision) -> str:
    if decision.key in {"pass", "classified"}:
        return (
            "The record may be cited only with the stated denominators, period, "
            "limitations and diagnostic classification."
        )
    if decision.key == "not-assessed":
        return "Do not treat the record as QA-accepted until it is assessed."
    return "Do not cite the numerical result in academic work."
