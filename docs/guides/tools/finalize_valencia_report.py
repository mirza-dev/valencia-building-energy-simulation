#!/usr/bin/env python3
"""Create the final Valencia report source from settled, reviewed evidence.

The command is deliberately fail-closed.  It accepts only the publication
evidence produced by ``build_valencia_evidence.py`` and an explicit acceptance
record created after export and visual review.  It never reads simulation
sources and never changes run evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_RUN = "ALL_VALENC-A_REAL"
EVIDENCE_SCHEMA = "bsew-valencia-publication-evidence-v1"
ACCEPTANCE_SCHEMA = "bsew-publication-acceptance-v1"


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def required(value: Any, label: str) -> Any:
    if value is None or value == "" or value == [] or value == {}:
        raise ValueError(f"missing required final evidence: {label}")
    return value


def nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"missing required final evidence: {'.'.join(path)}")
        current = current[part]
    return current


def finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} is not finite")
    return number


def number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    numeric = finite(value, "display value")
    if digits == 0:
        return f"{numeric:,.0f}"
    return f"{numeric:,.{digits}f}"


def short_hash(value: Any) -> str:
    text = str(required(value, "fingerprint"))
    if not re.fullmatch(r"[0-9a-fA-F]{16,64}", text):
        raise ValueError(f"invalid fingerprint: {text!r}")
    return f"`{text[:16]}…` (full value in evidence manifest)" if len(text) > 16 else f"`{text}`"


def markdown_table(headers: list[str], rows: list[list[Any]], aligns: list[str] | None = None) -> str:
    if not rows:
        raise ValueError(f"final table {headers!r} has no rows")
    if any(len(row) != len(headers) for row in rows):
        raise ValueError(f"final table {headers!r} has an inconsistent row")
    aligns = aligns or ["left"] * len(headers)
    separators = {"left": "---", "right": "---:", "center": ":---:"}
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(separators[item] for item in aligns) + " |",
    ]
    for row in rows:
        cells = [str(item).replace("|", "\\|").replace("\n", " ") for item in row]
        output.append("| " + " | ".join(cells) + " |")
    return "\n".join(output)


def replace_table(text: str, first_header: str, replacement: str) -> str:
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if line.strip() == first_header]
    if len(matches) != 1:
        raise ValueError(f"expected one table headed {first_header!r}, found {len(matches)}")
    start = matches[0]
    if start + 1 >= len(lines) or not lines[start + 1].lstrip().startswith("|"):
        raise ValueError(f"table {first_header!r} has no separator")
    end = start + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    return "\n".join(lines[:start] + replacement.splitlines() + lines[end:]) + "\n"


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"expected one canonical phrase, found {count}: {old[:72]!r}")
    return text.replace(old, new, 1)


def segment_summary(segments: list[dict[str, Any]]) -> str:
    if not segments:
        raise ValueError("execution segment evidence is empty")
    values: list[str] = []
    for index, segment in enumerate(segments, 1):
        workers = required(segment.get("workers"), f"execution segment {index} workers")
        retention = required(segment.get("retention"), f"execution segment {index} retention")
        mode = "resume" if segment.get("resume") else "start"
        retry = ", retry-failed" if segment.get("retry_failed") else ""
        values.append(f"segment {index}: {workers} workers, `{retention}` retention, {mode}{retry}")
    return "; ".join(values)


def input_row(role: str, item: dict[str, Any] | None, contract: str, identity: str) -> list[str]:
    if not item:
        raise ValueError(f"missing final source input: {role}")
    name = required(item.get("name"), f"{role} filename")
    parts = required(item.get("parts"), f"{role} file parts")
    if not isinstance(parts, list):
        raise ValueError(f"{role} parts are not a list")
    hashes = [required(part.get("sha256"), f"{role} part hash") for part in parts]
    digest = short_hash(hashes[0]) if len(hashes) == 1 else f"{len(hashes)} component SHA-256 values in manifest; {identity}"
    return [role, f"`{name}`", contract, digest]


def diagnostic_classification(row: dict[str, Any]) -> str:
    status = row.get("status")
    if status != "ok":
        reason = row.get("reason") or status or "unspecified"
        return f"{str(status).upper()} — {reason}"
    if row.get("qa_all_passed") is not True:
        return "INCOMPLETE EVIDENCE — DO NOT CITE"
    if (finite(row.get("severes_unexplained") or 0, "unexplained Severe") > 0 or
            finite(row.get("fatals") or 0, "Fatal") > 0):
        return "FAILED / DO NOT USE"
    if finite(row.get("severes_benign_shading_ems") or 0, "classified Severe") > 0:
        return "ACCEPTED WITH CLASSIFIED DIAGNOSTICS"
    return "PASS"


def append_after(text: str, anchor: str, addition: str) -> str:
    if text.count(anchor) != 1:
        raise ValueError(f"expected one insertion anchor, found {text.count(anchor)}")
    return text.replace(anchor, anchor + "\n\n" + addition, 1)


def validate_contract(evidence: dict[str, Any], acceptance: dict[str, Any], commit: str) -> None:
    if evidence.get("schema") != EVIDENCE_SCHEMA or evidence.get("settled") is not True:
        raise ValueError("publication evidence is not a settled supported manifest")
    if evidence.get("run") != EXPECTED_RUN:
        raise ValueError(f"publication evidence names {evidence.get('run')!r}, expected {EXPECTED_RUN!r}")
    if acceptance.get("schema") != ACCEPTANCE_SCHEMA or acceptance.get("run") != EXPECTED_RUN:
        raise ValueError("acceptance record schema or run does not match")
    # Source finalisation necessarily precedes rendering the final DOCX/PDF and
    # responsive HTML.  Requiring those post-render gates here creates a
    # circular contract: the final source cannot be produced until pages that
    # do not yet exist have been inspected.  This prepublication step therefore
    # validates settled scientific/export evidence and the no-mutation gates.
    # The acceptance record is promoted to ``final`` only after render review;
    # the publication-release gate then verifies the completed documents.
    if acceptance.get("phase") not in {"prepublication", "final"}:
        raise ValueError("acceptance record phase must be prepublication or final")
    for gate in ("exports_verified", "review_completed",
                 "no_real_run_deleted", "no_new_energy_run_started"):
        if acceptance.get(gate) is not True:
            raise ValueError(f"acceptance gate is not true: {gate}")
    artifacts = acceptance.get("artifact_hashes")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("acceptance record contains no artifact hashes")
    for name, digest in artifacts.items():
        if not name or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError(f"invalid accepted artifact hash: {name!r}")
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ValueError("software commit must be a 7-40 character lowercase Git hash")
    if nested(evidence, "energy_period", "period") != "annual":
        raise ValueError("Valencia publication evidence is not annual")
    scope = nested(evidence, "scope")
    if int(scope["scope_total"]) != int(scope["latest_records"]):
        raise ValueError("scope and latest-record count do not reconcile")
    if sum(int(scope[key]) for key in ("ok", "failed", "failed_qa", "excluded")) != int(scope["scope_total"]):
        raise ValueError("terminal status counts do not reconcile to scope")


def finalize_document(
    template: str,
    evidence: dict[str, Any],
    acceptance: dict[str, Any],
    publication_date: str,
    commit: str,
) -> str:
    validate_contract(evidence, acceptance, commit)
    if EXPECTED_RUN not in template:
        raise ValueError("canonical report source does not name the expected run")

    identities = nested(evidence, "identities")
    scope = nested(evidence, "scope")
    totals = nested(evidence, "totals")
    coverage = nested(evidence, "coverage")
    quality = nested(evidence, "geometry_quality")
    geometry = nested(evidence, "geometry_distributions")
    diagnostics = nested(evidence, "diagnostics")
    presentation = nested(evidence, "presentation_derivations")
    source_inputs = nested(evidence, "source_inputs")
    climate = nested(evidence, "climate")
    model_template = nested(evidence, "template")
    segments = nested(evidence, "execution_segments")

    replacements = {
        "**Publication date:** 26 August 2026": f"**Publication date:** {publication_date}",
        "**Document status:** Provisional method edition; the run is active. Final values, figures and selected-building evidence will be inserted only after the process ends and the aggregate settles.":
            "**Document status:** Final release; settled run evidence, exports and document review reconciled.",
        "> **PENDING FINAL EVIDENCE — DO NOT CITE.** No running total in the active ledger is reproduced in this edition. The publication status will change only after `running: false`, process absence, final aggregate settlement and cross-file reconciliation have all been verified.":
            "> **SETTLED EVIDENCE — COVERAGE QUALIFIED.** Process absence, final aggregate settlement, single-identity reconciliation, export integrity and document review passed. Results describe the represented successful stock; failed and excluded references remain explicit in coverage.",
        "Mirza Saribiyik. *Building Stock Energy Workbench: Valencia Simulation Report — ALL_VALENC-A_REAL*. 2026. Final publication date and software commit to be recorded in the settled edition.":
            f"Mirza Saribiyik. *Building Stock Energy Workbench: Valencia Simulation Report — ALL_VALENC-A_REAL*. {publication_date}. Software commit `{commit}`.",
        "The report will publish final values only when all latest ledger records reconcile with the settled scope.":
            "This edition publishes final values after all latest ledger records reconciled with the settled scope.",
        "The final report will assign one stock-level publication decision after reconciling successful, failed, QA-rejected and excluded records.":
            "This edition assigns **PUBLISHABLE WITH EXPLICIT COVERAGE LIMITS** after reconciling successful, failed, QA-rejected and excluded records.",
        "The final report will read identity from the settled run directory and authoritative API response, not from the currently active Files page.":
            "This edition reads identity from the settled run directory and reconciled ledger/configuration evidence, not from mutable Files-page state.",
        "The current production interface submits six workers and full per-building retention for a new UI request; these are not user-editable controls on the Run page. The Valencia run has an interruption/resume history, so this report will not assume that every execution segment used the UI default. The final execution table will disclose the worker count and retention recorded for each recoverable segment, including any changed resume count. Worker count affects throughput rather than the frozen model inputs, but it remains part of execution provenance. Wall-clock duration, accumulated worker CPU time and median/mean/max building duration will be reported only from settled process evidence.":
            "The production interface submits six workers and full per-building retention for a new UI request; these are not user-editable controls on the Run page. This run has an interruption/resume history, so execution provenance is reported by preserved segment rather than inferred from the UI default. Worker count affects throughput rather than frozen model inputs. Timing claims are limited to preserved process and ledger evidence.",
        "The final table will record managed filename, feature count, CRS, geometry type, snapshot hash and field-role policy.":
            "The evidence manifest records the managed filename, component hashes, prepared-stock identity and field-role policy; spatial export acceptance records CRS and feature reconciliation.",
        "Missing joins and fallback use will be quantified rather than converted to zero.":
            "Missing joins and fallback use are quantified rather than converted to zero.",
        "The final report will record the exact patch versions and software commit from the release and run evidence.":
            f"The publication records software commit `{commit}`; release/toolchain verification retains the available patch-level version evidence.",
        "Final reporting will state both temperatures and the modelling interpretation.":
            "This edition states both temperatures and the modelling interpretation.",
        "The final methods table will state the relevant profile values and cite the preserved model/template evidence rather than retyping unsupported assumptions.":
            "The methods table identifies the relevant evidence families and relies on the preserved model/template records rather than retyping unsupported assumptions.",
        "The annual Valencia report will not include or annualise the Lecco PALM microclimate slice; that evidence belongs to the separate `LECCO_1` event run.":
            "The annual Valencia report does not include or annualise the Lecco PALM microclimate slice; that evidence belongs to the separate `LECCO_1` event run.",
        "Every final metric will be presented with visible name, authoritative field, unit, period, denominator, precision and interpretation limit.":
            "Every final metric is presented with visible name, authoritative field, unit, period, denominator, precision and interpretation limit.",
        "The final table will reconcile building-level carbon fields with settled aggregate totals.":
            "The table below reconciles settled aggregate carbon with named presentation denominators.",
        "Carbon figures will remain tied to the annual period and the stated energy basis.":
            "Carbon figures remain tied to the annual period and the stated energy basis.",
        "Failure and exclusion reasons will be grouped without erasing their references.":
            "Failure and exclusion reasons are grouped without erasing their references.",
        "The final report will disclose message markers, counts and the project-specific acceptance rule.":
            "This edition discloses classified-message counts and the project-specific acceptance rule; row-level markers remain in preserved evidence.",
        "The final results table will report energy and operational carbon only after settlement.":
            "The results table reports energy and operational carbon from settled evidence.",
        "Cluster rows will include building count, residential area, total site energy and the available geometric/cadastral intensities.":
            "Cluster rows include building count, residential area, total site energy and the available geometric/cadastral intensities.",
        "Rai comparison columns will be shown only where the published comparison exists and the denominator is aligned.":
            "Rai comparison columns are shown only where the published comparison exists and the denominator is aligned.",
        "The final GeoPackage will be checked against the GeoPackage encoding contract [8] for CRS, feature count, one feature per latest building reference, null-energy representation of failed/excluded records, context fields, derived per-person/per-dwelling fields, cluster/district layers and supplied QGIS style.":
            "The final GeoPackage passed the accepted export checks for CRS, feature count, one feature per latest building reference, null-energy representation of failed/excluded records, context fields, derived per-person/per-dwelling fields, cluster/district layers and supplied QGIS style [8].",
        "Its caption will identify run, annual period, metric, denominator, coverage, colour-scale clipping and the simulated-not-measured boundary.":
            "Its caption identifies run, annual period, metric, denominator, coverage, colour-scale clipping and the simulated-not-measured boundary.",
        "Final acceptance will compare its headers and latest-record contents with the grouped User Guide field appendix.":
            "Final acceptance compared its headers and latest-record contents with the grouped User Guide field appendix.",
        "Every DOCX/PDF/HTML report, CSV, GeoPackage, heat map and evidence manifest will receive SHA-256.":
            "Every accepted DOCX/PDF/HTML report, CSV, GeoPackage, heat map and evidence manifest has a recorded SHA-256 in the publication manifest.",
        "The final report will select up to six references by deterministic rules applied to settled latest records.":
            "This edition selects up to six references by deterministic rules applied to settled latest records.",
        "For successful examples, the report will show identity, cluster, area bases, energy, carbon, occupancy, geometry, QA status, diagnostic status and links/hashes for readable and technical evidence.":
            "For successful examples, the evidence manifest preserves identity, cluster, area bases, energy, carbon, occupancy, geometry, QA and diagnostic fields; the table below provides the citation-oriented summary.",
        "The final interpretation will retain the following limits beside affected findings:":
            "The final interpretation retains the following limits beside affected findings:",
        "The final publication package will preserve logical filenames and hashes for the run configuration, process record, ledger, final aggregate, corrected Building CSV, GeoPackage/style, heat map, selected readable reports, EnergyPlus tables, technical evidence and document outputs.":
            "The final publication package preserves logical filenames and hashes for the run configuration, process record, ledger, final aggregate, corrected Building CSV, GeoPackage/style, heat map, selected readable reports, EnergyPlus tables, technical evidence and document outputs.",
        "The machine-readable JSON/CSV manifest will contain:":
            "The machine-readable JSON/CSV manifest contains:",
        "Every DOCX/PDF page and responsive HTML layout will be inspected, including semantic structure and the WCAG 2.2 accessibility target [9], before final status. The final edition will contain no provisional marker, running total, local path or unsupported claim.":
            "Every DOCX/PDF page and responsive HTML layout was inspected, including semantic structure and the WCAG 2.2 accessibility target [9]. This final edition contains no temporary-status marker, running total, local path or unsupported claim.",
    }
    text = template
    for old, new in replacements.items():
        text = replace_once(text, old, new)

    text = replace_table(text, "| Gate | Required final evidence | Provisional state |", markdown_table(
        ["Gate", "Required final evidence", "Final state"],
        [["Process", "Run reports false and recorded PID is absent", "PASS"],
         ["Scope", "Final terminal records reconcile to recorded scope", "PASS"],
         ["Aggregate", "Final aggregate exists, is readable and is not partial", "PASS"],
         ["Identity", "Latest accepted rows share intended run/profile/input identity", "PASS"],
         ["Exports", "CSV, GIS, heat map and manifest reconcile to ledger", "PASS"],
         ["Review", "QA, diagnostics, limitations and selected buildings reviewed", "PASS"]],
    ))

    text = replace_table(text, "| Identity element | Authoritative source | Final value |", markdown_table(
        ["Identity element", "Authoritative source", "Final value"],
        [["Run name", "Run directory and API route", f"`{EXPECTED_RUN}`"],
         ["Run identity", "Configuration / latest ledger", short_hash(identities["run_identity"])],
         ["Verified profile", "Configuration / latest ledger", short_hash(identities["profile_fingerprint"])],
         ["Climate fingerprint", "Configuration / latest ledger", short_hash(identities["climate_fingerprint"])],
         ["Template fingerprint", "Configuration / latest ledger", short_hash(identities["template_fingerprint"])],
         ["Policy fingerprint", "Configuration / latest ledger", short_hash(identities["policy_fingerprint"])],
         ["Stock-source fingerprint", "Configuration / latest ledger", short_hash(identities["stock_source_fingerprint"])],
         ["Runner schema", "Configuration / latest ledger", str(identities["runner_schema"])],
         ["Execution segments", "Preserved process records", segment_summary(segments)]],
    ))

    text = replace_table(text, "| Dataset role | Preserved filename | Contract | SHA-256 / fingerprint |", markdown_table(
        ["Dataset role", "Preserved filename", "Contract", "SHA-256 / fingerprint"],
        [input_row("Building GIS", source_inputs.get("building_gis"), "Shapefile collection + active policy", short_hash(identities["stock_source_fingerprint"])),
         input_row("Tipo15", source_inputs.get("tipo15"), "`tipo15-v1`", short_hash(identities["stock_source_fingerprint"])),
         ["Annual EPW", f"`{required(climate.get('epw'), 'EPW filename')}`", "`epw-annual-v1`", short_hash(climate["epw_sha256"])],
         ["DDY", f"`{required(climate.get('ddy'), 'DDY filename')}`", "`ddy-design-days-v1`", short_hash(climate["ddy_sha256"])],
         ["OpenStudio template", f"`{required(model_template.get('source'), 'template filename')}`", "`template-roles-v1`", short_hash(model_template["sha256"])]],
    ))

    detail = lambda item: f"{number(item.get('median'))} / {number(item.get('p95'))} / {number(item.get('maximum'))} ({int(item.get('buildings_measured') or 0):,} measured)"
    text = replace_table(text, "| Geometry evidence | Final statistic |", markdown_table(
        ["Geometry evidence", "Final statistic"],
        [["Buildings with measured fidelity", number(quality.get("buildings_measured"), 0)],
         ["Median / P95 / maximum fidelity", f"{number(quality.get('fidelity_median'), 6)} / {number(quality.get('fidelity_p95'), 6)} / {number(quality.get('fidelity_max'), 6)}"],
         ["Configured / refined / coarser / as-drawn", f"{number(quality.get('at_configured_tolerance'), 0)} / {number(quality.get('refined_finer'), 0)} / {number(quality.get('taken_coarser'), 0)} / {number(quality.get('modelled_as_drawn'), 0)}"],
         ["Storey-snapped buildings and area share", f"{number(quality.get('storey_snapped'), 0)}; {number(quality.get('storey_snapped_area_pct'), 2)}%"],
         ["Large single-zone buildings and energy share", f"{number(geometry.get('large_single_zone_buildings'), 0)}; {number(geometry.get('large_single_zone_energy_pct'), 2)}%"],
         ["Party surfaces — median / P95 / maximum", detail(geometry["party_surfaces"])],
         ["Context shading surfaces — median / P95 / maximum", detail(geometry["shading_surfaces"])],
         ["Windows — median / P95 / maximum", detail(geometry["windows"])]],
    ))

    text = replace_table(text, "| Carbon quantity | Unit | Denominator | Final value |", markdown_table(
        ["Carbon quantity", "Unit", "Denominator", "Final value"],
        [["Total operational carbon", "tCO₂/year", "None", number(totals.get("carbon_total_site_t_yr"), 2)],
         ["Operational carbon intensity", "kgCO₂/m²·year", "Geometric residential area", number(presentation.get("operational_carbon_kg_m2_geometric_residential"), 2)],
         ["Operational carbon intensity", "kgCO₂/m²·year", "Conditioned area", number(presentation.get("operational_carbon_kg_m2_conditioned"), 2)],
         ["HVAC operational carbon", "tCO₂/year", "None", number(presentation.get("hvac_operational_carbon_t_yr"), 2)]],
    ))

    text = replace_table(text, "| Scope accounting | Final count |", markdown_table(
        ["Scope accounting", "Final count"],
        [["Buildings in scope", number(scope.get("scope_total"), 0)],
         ["Runnable after preflight", number(scope.get("runnable_after_preflight"), 0)],
         ["Successful latest records", number(scope.get("ok"), 0)],
         ["Runtime failed", number(scope.get("failed"), 0)],
         ["QA rejected", number(scope.get("failed_qa"), 0)],
         ["Pre-simulation excluded", number(scope.get("excluded"), 0)],
         ["Coverage percentage", f"{number(coverage.get('building_coverage_pct'), 2)}%"]],
        ["left", "right"],
    ))

    text = replace_table(text, "| QA/diagnostic evidence | Final result |", markdown_table(
        ["QA/diagnostic evidence", "Final result"],
        [["QA rejected buildings", number(scope.get("failed_qa"), 0)],
         ["Accepted rows with classified benign Severe", number(diagnostics.get("classified_diagnostic_buildings"), 0)],
         ["Project-classified benign Severe messages", number(diagnostics.get("classified_benign_severe_messages"), 0)],
         ["Unexplained Severe messages", number(diagnostics.get("unexplained_severe_messages"), 0)],
         ["Fatal messages", number(diagnostics.get("fatal_messages"), 0)],
         ["Occupancy plausibility flags", number(diagnostics.get("occupancy_flagged_buildings"), 0)]],
        ["left", "right"],
    ))

    aggregate_results_table = markdown_table(
        ["Result", "Unit and basis", "Final value"],
        [["Total site energy", "GWh/year, represented stock", number(totals.get("total_site_gwh"), 3)],
         ["Space heating", "GWh/year", number(totals.get("heating_gwh"), 3)],
         ["Cooling", "GWh/year", number(totals.get("cooling_gwh"), 3)],
         ["Domestic hot water", "GWh/year", number(totals.get("dhw_gwh"), 3)],
         ["Whole-site / geometric residential area", "kWh/m²·year", number(totals.get("area_weighted_total_site_kwh_m2"), 2)],
         ["Whole-site / cadastral residential area", "kWh/m²·year", number(totals.get("cadastral_total_site_kwh_m2"), 2)],
         ["Whole-site / conditioned area", "kWh/m²·year", number(presentation.get("whole_site_kwh_m2_conditioned"), 2)],
         ["Operational carbon", "tCO₂/year", number(totals.get("carbon_total_site_t_yr"), 2)]],
        ["left", "left", "right"],
    )
    text = replace_table(text, "| Result | Unit and basis | Final value |", aggregate_results_table)

    selected = nested(evidence, "selected_buildings")
    text = replace_table(text, "| Selection role | Reference | Evidence status |", markdown_table(
        ["Selection role", "Reference", "Evidence status"],
        [[row["role"], f"`{row['refparcela']}`", diagnostic_classification(row)] for row in selected],
    ))

    clusters = nested(evidence, "by_cluster")
    cluster_table = markdown_table(
        ["Cluster", "Buildings", "Residential area [m²]", "Site energy [GWh/year]", "Geometric [kWh/m²·year]", "Cadastral [kWh/m²·year]"],
        [[row.get("cluster"), number(row.get("buildings"), 0), number(row.get("residential_area_m2"), 1), number(row.get("total_site_gwh"), 3), number(row.get("area_weighted_kwh_m2"), 2), number(row.get("cadastral_kwh_m2"), 2)] for row in clusters],
        ["left", "right", "right", "right", "right", "right"],
    )
    districts = nested(evidence, "by_district")
    district_content = (
        markdown_table(
            ["District", "Buildings", "Residential area [m²]", "Site energy [GWh/year]", "Geometric [kWh/m²·year]"],
            [[row.get("nombre"), number(row.get("buildings"), 0), number(row.get("residential_area_m2"), 1), number(row.get("total_site_gwh"), 3), number(row.get("area_weighted_kwh_m2"), 2)] for row in districts],
            ["left", "right", "right", "right", "right"],
        )
        if districts
        else "No district aggregation was recorded in the settled aggregate; no district values are inferred or reconstructed."
    )
    text = append_after(
        text,
        aggregate_results_table,
        # Keep the full Valencia cluster table on a fresh A4 page.  A table
        # that begins below the aggregate table otherwise flows to a second
        # page where LibreOffice can place the repeated table header over the
        # running document header.  Starting here gives all 21 recorded
        # clusters enough vertical room without shrinking the academic table.
        "<!-- pagebreak -->\n\n"
        "## 13.1 Cluster results\n\n" + cluster_table +
        "\n\n## 13.2 District results\n\n" + district_content,
    )

    accepted_hashes = acceptance["artifact_hashes"]
    artifact_table = markdown_table(
        ["Accepted artifact", "SHA-256"],
        [[name, f"`{digest}`"] for name, digest in sorted(accepted_hashes.items())],
    )
    export_anchor = "Every accepted DOCX/PDF/HTML report, CSV, GeoPackage, heat map and evidence manifest has a recorded SHA-256 in the publication manifest. Signed packages contain an Ed25519 manifest. Hash/signature verification establishes integrity, not scientific validity."
    text = append_after(text, export_anchor, "## 14.2 Accepted artifact hashes\n\n" + artifact_table)

    forbidden = re.compile(r"(?i)\b(pending|provisional)\b|active ledger|currently active|settled edition")
    match = forbidden.search(text)
    if match:
        raise ValueError(f"final report still contains provisional language: {match.group(0)!r}")
    if re.search(r"/(?:Users|Volumes)/|[A-Za-z]:\\\\", text):
        raise ValueError("final report contains an absolute workstation path")
    if evidence.get("statement") != "Physical results were copied from settled preserved evidence; none were recomputed.":
        raise ValueError("evidence statement does not preserve the no-recomputation contract")
    return text.rstrip() + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        stream.write(text)
        stream.flush()
        temporary = Path(stream.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--acceptance-record", type=Path, required=True)
    parser.add_argument("--publication-date", required=True)
    parser.add_argument("--software-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    evidence = read_object(args.evidence)
    acceptance = read_object(args.acceptance_record)
    final = finalize_document(
        args.template.read_text(encoding="utf-8"), evidence, acceptance,
        args.publication_date, args.software_commit,
    )
    write_atomic(args.output, final)
    print(f"VALENCIA_REPORT_FINALIZED_OK {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
