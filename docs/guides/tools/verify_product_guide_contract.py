#!/usr/bin/env python3
"""Compare the User Guide's operational claims with product source contracts.

The check parses constants and literal upload rules without importing the
simulation package.  It is therefore safe while a protected stock run is
active: product source and run evidence are only read.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path
from typing import Any


def literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                value = node.value
                if value is not None:
                    return ast.literal_eval(value)
    raise ValueError(f"literal assignment {name} not found in {path}")


def upload_rules(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upload_dataset":
            for child in ast.walk(node):
                if isinstance(child, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "allowed"
                    for target in child.targets
                ):
                    value = ast.literal_eval(child.value)
                    return {str(key): set(items) for key, items in value.items()}
    raise ValueError(f"upload_dataset allowed-file map not found in {path}")


def require_tokens(text: str, tokens: list[str], label: str, errors: list[str]) -> None:
    for token in tokens:
        if token not in text:
            errors.append(f"{label} is missing {token!r}")


def section_between(text: str, start: str, end: str) -> str:
    """Return a named Markdown slice, failing instead of checking the wrong prose."""
    try:
        return text.split(start, 1)[1].split(end, 1)[0]
    except IndexError as exc:
        raise ValueError(f"could not isolate guide section {start!r}..{end!r}") from exc


def curated_appendix_fields(text: str) -> tuple[str, ...]:
    """Return the ordered CSV field contract declared by Appendix A.1-A.7."""
    try:
        appendix = text.split("# Appendix A.", 1)[1].split("## A.8", 1)[0]
    except IndexError as exc:
        raise ValueError("User Guide Appendix A.1-A.7 could not be isolated") from exc
    section = ""
    fields: list[str] = []
    for line in appendix.splitlines():
        heading = re.match(r"^##\s+(A\.[1-7])\b", line)
        if heading:
            section = heading.group(1)
            continue
        if not section or not line.startswith("|") or "`" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        # A.1 explicitly distinguishes CSV fields from GIS-only context.
        if section == "A.1" and "CSV" not in cells[1]:
            continue
        fields.extend(re.findall(r"`([^`]+)`", cells[0]))
    if not fields:
        raise ValueError("no curated CSV fields were found in Appendix A.1-A.7")
    return tuple(fields)


def literal_field_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return tuple(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError("CURATED_BUILDING_CSV_FIELDS must be a literal ordered string list/tuple or mapping")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--guide", type=Path, required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    root = args.project_root.resolve()
    guide = args.guide.read_text(encoding="utf-8")
    api = root / "src/workbench/api.py"
    inputs = root / "src/workbench/file_inputs.py"
    run_page = (root / "frontend/src/components/RunPage.tsx").read_text(encoding="utf-8")
    files_page = (root / "frontend/src/components/FilesPage.tsx").read_text(encoding="utf-8")
    outputs_page = (root / "frontend/src/components/OutputsPage.tsx").read_text(encoding="utf-8")
    errors: list[str] = []
    notes: list[str] = []

    allowed = upload_rules(api)
    expected_uploads = {
        "gis": {".gpkg", ".geojson", ".json", ".zip"},
        "tipo15": {".csv"},
        "template": {".osm"},
        "weather": {".epw"},
        "ddy": {".ddy"},
        "stock": {".gpkg", ".geojson", ".json"},
        "eu_database": {".db", ".sqlite", ".sqlite3", ".gpkg"},
        "microclimate": {".zip"},
    }
    if allowed != expected_uploads:
        errors.append(f"upload extension contract changed: {allowed!r}")

    tipo15 = tuple(literal_assignment(inputs, "TIPO15_REQUIRED_COLUMNS"))
    stock = tuple(literal_assignment(inputs, "STOCK_REQUIRED_COLUMNS"))
    envelope = tuple(literal_assignment(inputs, "STOCK_PINNED_ENVELOPE_COLUMNS"))
    require_tokens(guide, [f"`{item}`" for item in tipo15], "Tipo15 tutorial", errors)
    require_tokens(guide, [f"`{item}`" for item in stock], "Prepared-stock tutorial", errors)
    require_tokens(guide, [f"`{item}`" for item in envelope], "Envelope tutorial", errors)
    require_tokens(
        guide,
        ["`.gpkg`", "`.geojson`", "`.json`", "`.zip`", "`.db`", "`.sqlite`", "`.sqlite3`",
         "`.csv`", "`.osm`", "EPW", "DDY", "refuses a bare `.shp`"],
        "Files tutorial",
        errors,
    )

    # The approved guide is an executable lifecycle, not a glossary.  Every
    # input route has to tell the researcher what to do, why, what evidence to
    # expect and how rejection is represented.  Isolating the four tutorials
    # prevents one complete Valencia section from masking a thin Lecco/event
    # section elsewhere in the document.
    tutorial_ranges = (
        ("## 5.1 Tutorial A", "## 5.2 Tutorial B", 3, "raw Cadastre + Dwelling Ledger"),
        ("## 5.2 Tutorial B", "## 5.3 Tutorial C", 3, "prepared stock"),
        ("## 5.3 Tutorial C", "## 5.4 Tutorial D", 3, "building database"),
        ("## 5.4 Tutorial D", "## 5.5 Input interpretation limits", 4, "microclimate event"),
    )
    for start, end, steps, label in tutorial_ranges:
        try:
            tutorial = section_between(guide, start, end)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for field in ("**Action.**", "**Purpose.**", "**Expected evidence.**", "**Common rejection.**"):
            observed = tutorial.count(field)
            if observed != steps:
                errors.append(
                    f"{label} tutorial must contain {steps} {field} blocks, found {observed}"
                )

    require_tokens(
        guide,
        [
            "Uploading copies a source into managed, content-addressed storage",
            "Activating selects which validated snapshot future runs will resolve",
            "snapshot hash",
            "fingerprint",
            "more than one `.shp` dataset",
            "semicolon-delimited Latin-1",
            "projected CRS whose unit is the metre",
            "It is not a generic SQL importer",
            "Build stock from database",
            "Microclimate event run — these are not annual figures",
        ],
        "Files lifecycle and interpretation contract",
        errors,
    )

    require_tokens(run_page, ["keep: 'full' as const, workers: 6", "Start run", "Resume same run",
                              "Stop safely", "Run preflight", "PREFLIGHT PASSED"], "Run UI", errors)
    require_tokens(guide, ["**6 workers**", "**full per-building retention**", "**Start run**",
                           "**Resume same run**", "**Stop safely**", "Run preflight"],
                   "Run tutorial", errors)
    require_tokens(
        guide,
        [
            "**Selected buildings**",
            "**One district**",
            "**All [city]**",
            "Changing scope, district, references, run mode or run name invalidates the displayed preflight",
            "The percentage is terminal records divided by the recorded scope",
            "The progress counts come from the fsynced ledger on disk, not process memory",
            "Process absence without a final aggregate is interruption, not completion",
            "Retry failed",
            "**New run from all**",
        ],
        "Run, monitor, stop and resume lifecycle",
        errors,
    )

    output_labels = ["Building CSV", "GIS layer (.gpkg)", "Heat map (.png)", "Full signed ZIP",
                     "Delete run", "Geometry check", "EnergyPlus result tables", "Signed building package"]
    require_tokens(outputs_page, output_labels, "Outputs UI", errors)
    require_tokens(guide, [f"**{label}:**" for label in output_labels[:4]], "Outputs download guide", errors)
    require_tokens(guide, ["**Delete run**", "**Geometry check**", "**EnergyPlus result tables**",
                           "**Signed building package**"], "Outputs evidence guide", errors)
    require_tokens(
        guide,
        [
            "Select the run and establish its state",
            "Read coverage and status before intensity",
            "Read cluster totals",
            "Treat uncertainty evidence as contextual",
            "Resolve unfinished records",
            "Audit the Building ledger",
            "Perform the geometry check",
            "Use readable and technical evidence together",
            "exact run name before permanent deletion",
            "Import the file as UTF-8 CSV",
            "Open `results_buildings.gpkg` as a vector dataset",
            "Facade WWR is intentionally blank in this viewer",
            "`deep_layers.json`",
            "`model_python.osm`",
            "`eplusout.err`",
            "`verified_profile.json`",
            "`eplustbl.htm`",
            "Ed25519 manifest establishes file integrity",
        ],
        "Outputs reading, geometry and export lifecycle",
        errors,
    )

    # Scientific non-claims need to be repeated where readers actually make
    # decisions, not left only on the cover.
    require_tokens(
        guide,
        [
            "measured consumption or a utility-bill observation",
            "not measurement calibration",
            "simulated, not measured consumption",
            "Event-period carbon is operational emissions over the event, not annual carbon",
            "A green input card is not a scientific conclusion",
        ],
        "Scientific boundary",
        errors,
    )

    try:
        appendix_fields = curated_appendix_fields(guide)
    except ValueError as exc:
        appendix_fields = ()
        errors.append(str(exc))
    duplicates = sorted({field for field in appendix_fields if appendix_fields.count(field) > 1})
    if duplicates:
        errors.append(f"curated CSV appendix defines fields more than once: {duplicates!r}")
    require_tokens(
        guide,
        ["`total_site_co2_t`", "`hvac_co2_t`", "event value must never be exposed to the user under a per-year label"],
        "Period-neutral curated carbon contract",
        errors,
    )
    if "`total_site_co2_t_yr`" in guide or "`hvac_co2_t_yr`" in guide:
        errors.append("curated CSV appendix still exposes an event-capable carbon total under an _yr field")

    if 'accept=".gpkg,.shp,.geojson,.zip"' in files_page:
        message = "Files picker still offers bare .shp although the API refuses it"
        if args.final:
            errors.append(message)
        else:
            notes.append(message)

    figure_count = len(re.findall(r'^!\[[^]]+\]\(', guide, re.MULTILINE))
    if args.final and not 12 <= figure_count <= 16:
        errors.append(f"final User Guide must contain 12-16 figures, found {figure_count}")
    elif not args.final:
        notes.append(f"provisional figure count is {figure_count}; final target is 12-16")

    stock_adapter_path = root / "src/workbench/stock_adapter.py"
    stock_adapter = stock_adapter_path.read_text(encoding="utf-8")
    if "CURATED_BUILDING_CSV_FIELDS" not in stock_adapter:
        message = "curated Building CSV schema constant is not implemented yet"
        if args.final:
            errors.append(message)
        else:
            notes.append(message)
    else:
        try:
            implementation_fields = literal_field_names(
                literal_assignment(stock_adapter_path, "CURATED_BUILDING_CSV_FIELDS")
            )
        except (ValueError, SyntaxError) as exc:
            errors.append(str(exc))
        else:
            if implementation_fields != appendix_fields:
                missing = [field for field in appendix_fields if field not in implementation_fields]
                undocumented = [field for field in implementation_fields if field not in appendix_fields]
                errors.append(
                    "curated Building CSV header differs from Appendix A "
                    f"(missing={missing!r}, undocumented={undocumented!r}, order_equal=False)"
                )

    marker = re.compile(r"\b(?:provisional|pending|capture pending|publication edition)\b", re.IGNORECASE)
    if args.final and marker.search(guide):
        errors.append("final User Guide still contains provisional/pending language")

    if errors:
        print("PRODUCT_GUIDE_CONTRACT_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PRODUCT_GUIDE_CONTRACT_OK")
    print(f"- CURATED CSV APPENDIX FIELDS: {len(appendix_fields)} unique ordered fields")
    for note in notes:
        print(f"- KNOWN POST-RUN GAP: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
