"""One readable page for one simulated building.

The runner already preserves everything anyone could want to know about a
building - `deep_layers.json` carries 11 stage records, 30 result fields, the
five QA checks with their tolerances, and 78 summary fields including full
provenance.  It was only ever offered as raw JSON, so in practice nobody read
it: of the five preserved files, only `eplustbl.htm` rendered.

This module turns that record into a page.  It computes nothing and corrects
nothing: every number here is read straight out of the file the run wrote, and
a field the run did not record prints as an em dash rather than a zero -
"absent" and "measured zero" are different claims and this project has been
bitten by conflating them before.

HTML rather than PDF, deliberately: `eplustbl.htm` already opens in a browser
tab through the same CSP-sandboxed route, the server gains no rendering
dependency, and a browser prints to PDF perfectly well if someone wants one.
"""
from __future__ import annotations

import html
from typing import Any, Iterable, Mapping

DASH = "—"


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any, digits: int = 2, unit: str = "") -> str:
    """A number as written, or an em dash.

    Booleans are excluded on purpose: `True` is not 1.00, and Python would
    happily format it as such.
    """
    if isinstance(value, bool) or value is None:
        return _esc(_yes_no(value)) if isinstance(value, bool) else DASH
    if not isinstance(value, (int, float)):
        return DASH
    text = f"{value:,.{digits}f}"
    return f"{text}&thinsp;{_esc(unit)}" if unit else text


def _yes_no(value: Any) -> str:
    if value is None:
        return DASH
    return "yes" if value else "no"


def _text(value: Any) -> str:
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, (list, tuple)):
        return _esc(", ".join(str(item) for item in value)) if value else DASH
    return _esc(value)


def _rows(pairs: Iterable[tuple[str, str]]) -> str:
    return "".join(
        f"<tr><th>{_esc(label)}</th><td>{value}</td></tr>" for label, value in pairs)


def _section(title: str, note: str, body: str) -> str:
    if not body:
        return ""
    return (f'<section><h2>{_esc(title)}</h2>'
            f'<p class="note">{_esc(note)}</p>{body}</section>')


# ---------------------------------------------------------------------------
# layer descriptions
#
# Each entry: the stage's heading, the sentence that says what the stage is
# for, and the fields worth showing with their labels.  A stage the run did not
# record is skipped entirely rather than rendered empty - a heading over
# nothing reads as a stage that did nothing.
# ---------------------------------------------------------------------------
_LAYERS: tuple[tuple[str, str, str, tuple[tuple[str, str, str], ...]], ...] = (
    ("mixed_use", "Mixed use", (
        "Storeys the cadastral record does not account for as dwellings are "
        "re-typed as commercial rather than simulated as housing."), (
        ("residential_storeys", "Residential storeys", "int"),
        ("storeys_converted", "Converted to commercial", "int"),
        ("space_type", "Commercial space type", "text"),
        ("in_floor_area_basis", "Counted in the area basis", "bool"),
    )),
    ("partial_top_storey", "Partial top storey", (
        "The storey rule rounds up, so the top storey is usually not full. Its "
        "loads are scaled to the recorded fraction; the geometry is not touched, "
        "because the building really is that tall."), (
        ("fraction", "Occupied fraction", "f4"),
        ("full_storeys_remaining", "Full storeys below", "int"),
        ("geometry_changed", "Geometry changed", "bool"),
        ("space", "Scaled space", "text"),
        ("unscaled", "Deliberately not scaled", "text"),
    )),
    ("occupancy", "Occupancy", (
        "Residents come from the cadastral population register, not from the "
        "CTE design norm. Ventilation is bound to occupancy, so it is rescaled "
        "with it."), (
        ("occupants", "Occupants applied", "f1"),
        ("padron_occupants", "Padrón record", "f1"),
        ("m2_per_person", "Floor area per person", "m2"),
        ("cte_norm_people_per_m2", "CTE norm (people/m²)", "f4"),
        ("people_per_m2", "Applied (people/m²)", "f6"),
    )),
    ("ground", "Ground storey", (
        "Rai's regime: the ground storey is conditioned commercial space, kept "
        "out of the floor-area basis, and given an insulated slab."), (
        ("ground_space_type", "Space type", "text"),
        ("ground_spaces_conditioned", "Conditioned spaces", "int"),
        ("ground_thermostat", "Own thermostat", "bool"),
        ("ground_slab_construction", "Slab construction", "text"),
        ("in_floor_area_basis", "Counted in the area basis", "bool"),
    )),
    ("ground_glazing", "Ground-storey glazing", (
        "Shopfront glazing, reusing the building's own window construction "
        "rather than inventing one."), (
        ("ground_windows", "Windows", "int"),
        ("ground_glass_area_m2", "Glass area", "m2"),
        ("ground_facades_glazed", "Facades glazed", "int"),
    )),
    ("window_frames", "Window frames", (
        "EnergyPlus treats a window polygon as glass, so the target ratio is "
        "shrunk before placement and the frame added around it."), (
        ("window_frame", "Frame", "text"),
        ("frame_width_m", "Frame width", "m"),
        ("windows_with_frame", "Openings framed", "int"),
        ("glass_area_m2", "Glass area", "m2"),
        ("opening_area_m2", "Opening area", "m2"),
        ("wwr_rescaled_by", "Ratio rescaled by", "f3"),
    )),
    ("dhw", "Domestic hot water", (
        "A real plant loop, drawn per person from the CTE figure - not a "
        "per-area norm."), (
        ("dhw_litres_per_person_day", "Litres per person per day", "f1"),
        ("dhw_litres_per_day", "Litres per day", "f1"),
        ("dhw_setpoint_c", "Setpoint", "degc"),
        ("dhw_boiler_efficiency", "Boiler efficiency", "f2"),
        ("dhw_equipment_count", "Draw objects", "int"),
        ("dhw_end_use_subcategory", "Metered as", "text"),
    )),
    ("hvac", "HVAC", (
        "Packaged terminal heat pumps, one per zone, at the coefficients the "
        "verified profile pins."), (
        ("hvac_system", "System", "text"),
        ("hvac_zones", "Zones", "int"),
        ("residential_zones", "Residential zones", "int"),
        ("heating_cop", "Heating COP", "f2"),
        ("cooling_cop", "Cooling COP", "f2"),
        ("ground_heating_cop", "Ground-storey heating COP", "f2"),
        ("heating_sizing_factor", "Heating sizing factor", "f2"),
        ("cooling_sizing_factor", "Cooling sizing factor", "f2"),
    )),
    ("terciario_metering", "Commercial metering", (
        "Commercial lights and equipment are metered under their own end-use "
        "subcategory so the residential split is measured, not estimated."), (
        ("subcategory", "Subcategory", "text"),
        ("spaces", "Spaces", "int"),
        ("tagged_lights", "Light objects tagged", "int"),
        ("tagged_equipment", "Equipment objects tagged", "int"),
    )),
    ("zoning", "Thermal zoning", (
        "One well-mixed zone per storey. Above the threshold below that stops "
        "being defensible, and the building is flagged rather than quietly "
        "blended into the totals."), (
        ("scheme", "Scheme", "text"),
        ("footprint_m2", "Footprint", "m2"),
        ("single_zone_threshold_m2", "Single-zone threshold", "m2"),
        ("large_footprint_single_zone", "Above the threshold", "bool"),
        ("note", "Known limitation", "text"),
    )),
    ("weather", "Weather", (
        "The climate package this building was simulated on, by content hash."), (
        ("climate", "Climate", "text"),
        ("epw_file", "Weather file", "text"),
        ("climate_fingerprint", "Fingerprint", "hash"),
    )),
)


def _format(value: Any, kind: str) -> str:
    if kind == "int":
        return _num(value, 0)
    if kind == "bool":
        return _esc(_yes_no(value))
    if kind == "hash":
        return f"<code>{_esc(value)}</code>" if value else DASH
    if kind == "m2":
        return _num(value, 1, "m²")
    if kind == "m":
        return _num(value, 3, "m")
    if kind == "degc":
        return _num(value, 1, "°C")
    if kind.startswith("f") and kind[1:].isdigit():
        return _num(value, int(kind[1:]))
    return _text(value)


def _layer_sections(layers: Mapping[str, Any]) -> str:
    out = []
    for key, title, note, fields in _LAYERS:
        record = layers.get(key)
        if not isinstance(record, Mapping):
            continue
        pairs = [(label, _format(record.get(field), kind))
                 for field, label, kind in fields if field in record]
        if pairs:
            out.append(_section(title, note, f"<table>{_rows(pairs)}</table>"))
    return "".join(out)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
_STYLE = """
:root { --ink:#1d221f; --muted:#63706a; --line:#e1e4df; --line-strong:#c8cdc6;
        --bg:#fafbf8; --teal:#2f6f62; --warn:#9b5c14; --bad:#884438; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 22px 60px; color:var(--ink); background:#fff;
       font:13px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
main { max-width: 980px; margin: 0 auto; }
code { font: 11px ui-monospace,SFMono-Regular,Menlo,monospace; background:#f2f3ef; padding:1px 4px; }
h1 { margin:0 0 2px; font-size:21px; letter-spacing:-.01em; }
h1 code { background:none; padding:0; font-size:21px; }
h2 { margin:0 0 3px; font-size:13px; }
.sub { margin:0 0 22px; color:var(--muted); font-size:11px; }
.status { display:inline-block; padding:2px 7px; margin-left:8px; vertical-align:3px;
          font:9px ui-monospace,monospace; text-transform:uppercase; letter-spacing:.05em; }
.status.ok { color:#496d4d; background:#e8f1e5; }
.status.bad { color:var(--bad); background:#fae8e4; }
section { margin:0 0 20px; padding:13px 15px; border:1px solid var(--line-strong); background:var(--bg); }
.note { margin:0 0 10px; color:var(--muted); font-size:11px; max-width:74ch; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th, td { padding:6px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
tr:last-child th, tr:last-child td { border-bottom:0; }
th { width:44%; font-weight:600; color:var(--muted); }
td { font-variant-numeric: tabular-nums; overflow-wrap:anywhere; }
table.grid th { width:auto; background:#eef1ec; color:#63706a; font:9px ui-monospace,monospace;
                text-transform:uppercase; letter-spacing:.04em; border-bottom:1px solid var(--line-strong); }
.pass { color:#496d4d; } .fail { color:var(--bad); font-weight:600; }
.kpi { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px;
       background:var(--line-strong); border:1px solid var(--line-strong); margin-bottom:20px; }
.kpi > div { padding:11px 13px; background:#fff; }
.kpi span { display:block; color:var(--muted); font:9px ui-monospace,monospace;
            text-transform:uppercase; letter-spacing:.05em; }
.kpi strong { display:block; margin-top:4px; font:19px ui-monospace,monospace; font-weight:600; }
.kpi small { display:block; color:var(--muted); font-size:10px; }
footer { margin-top:26px; padding-top:14px; border-top:1px solid var(--line-strong);
         color:var(--muted); font-size:11px; }
footer a { color:var(--teal); }
@media print { body { padding:0; } section { break-inside:avoid; } }
"""


def _qa_table(checks: Any) -> str:
    if not isinstance(checks, list) or not checks:
        return ""
    body = []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        passed = check.get("passed")
        mark = ("<span class='pass'>PASS</span>" if passed
                else "<span class='fail'>FAIL</span>" if passed is False else DASH)
        body.append(
            "<tr>"
            f"<td><code>{_esc(check.get('check'))}</code></td>"
            f"<td>{_text(check.get('model'))}</td>"
            f"<td>{_text(check.get('eplus'))}</td>"
            f"<td>{_text(check.get('tolerance'))}</td>"
            f"<td>{mark}</td></tr>")
    if not body:
        return ""
    return ("<table class='grid'><thead><tr><th>Check</th><th>Model says</th>"
            "<th>EnergyPlus says</th><th>Tolerance</th><th></th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def render(layers: Mapping[str, Any], *, run: str, reference: str,
           ledger_row: Mapping[str, Any] | None = None) -> str:
    """One self-contained HTML page for one building.

    `layers` is the parsed `deep_layers.json`.  `ledger_row` is optional: it
    supplies the run-level status wording, and its absence must not stop the
    report - a building can have a preserved model and no matching row only in
    situations worth seeing, not worth erroring on.
    """
    summary: Mapping[str, Any] = layers.get("summary") or {}
    results: Mapping[str, Any] = layers.get("results") or {}
    carbon: Mapping[str, Any] = layers.get("carbon") or {}
    stages: Mapping[str, Any] = layers.get("layers") or {}
    profile: Mapping[str, Any] = summary.get("verified_profile") or {}
    row: Mapping[str, Any] = ledger_row or {}

    status = str(row.get("status") or "")
    qa_all = summary.get("qa_all_passed")
    badge = ""
    if status:
        css = "ok" if status == "ok" else "bad"
        badge = f'<span class="status {css}">{_esc(status)}</span>'

    kpi = f"""<div class="kpi">
      <div><span>Total site energy</span><strong>{_num(results.get('total_site_kwh_m2'), 2)}</strong><small>kWh/m²&thinsp;· residential storeys</small></div>
      <div><span>Space heating</span><strong>{_num(results.get('space_heating_kwh_m2'), 2)}</strong><small>kWh/m²</small></div>
      <div><span>Space cooling</span><strong>{_num(results.get('cooling_kwh_m2'), 2)}</strong><small>kWh/m²</small></div>
      <div><span>Hot water</span><strong>{_num(results.get('dhw_kwh_m2'), 2)}</strong><small>kWh/m²&thinsp;· {_num(results.get('dhw_share_pct'), 1)}% of the total</small></div>
      <div><span>Carbon</span><strong>{_num(carbon.get('total_site_co2_t_yr'), 1)}</strong><small>tCO₂/yr</small></div>
      <div><span>QA</span><strong>{'PASS' if qa_all else 'FAIL' if qa_all is False else DASH}</strong><small>{_num(summary.get('warnings'), 0)} warnings&thinsp;· {_num(summary.get('severes_unexplained'), 0)} unexplained severe</small></div>
    </div>"""

    identity = _section(
        "Identity and provenance",
        "What produced these numbers. Every field here is stamped on the run, "
        "so the page can be read years later without the machine that made it.",
        "<table>" + _rows([
            ("Cadastral reference", f"<code>{_esc(summary.get('refparcela') or reference)}</code>"),
            ("Run", f"<code>{_esc(run)}</code>"),
            ("Typology cluster", _text(row.get("cluster"))),
            ("Verified profile", f"<code>{_esc(profile.get('profile_id'))}</code> "
                                 f"<code>{_esc(str(profile.get('fingerprint') or '')[:16])}…</code>"),
            ("Verified against", _text(profile.get("verified_against"))),
            ("Climate", _text(summary.get("climate"))),
            ("Occupant source", _text(summary.get("occupants_source"))),
            ("Empty-building policy", _text(summary.get("zero_policy"))),
            ("Ground-use source", _text(summary.get("ground_use_source"))),
            ("Target GIS", f"<code>{_esc(summary.get('target_gis_path'))}</code>"),
            ("Shading context GIS", f"<code>{_esc(summary.get('context_gis_path'))}</code>"),
        ]) + "</table>")

    geometry = _section(
        "Geometry as built",
        "What the engine actually constructed, including the two places where "
        "the cadastral record overrode the raw GIS height.",
        "<table>" + _rows([
            ("Footprint", _num(summary.get("footprint_m2"), 1, "m²")),
            ("Storeys built", _num(summary.get("n_floors_total"), 0)),
            ("Residential storeys", _num(summary.get("n_floors_residential"), 0)),
            ("Storey cap applied", _esc(_yes_no(summary.get("storey_cap_applied")))),
            ("Ground storey use", _text(summary.get("ground_use"))),
            ("Residential area (geometric)", _num(summary.get("res_area_m2"), 1, "m²")),
            ("Cadastral dwelling area (Tipo15)", _num(summary.get("tipo15_res_area_m2"), 1, "m²")),
            ("Conditioned area incl. commercial", _num(summary.get("total_conditioned_area_m2"), 1, "m²")),
            ("Conditioned area / cadastral", _num(summary.get("conditioned_to_cadastral_ratio"), 3)),
            ("Party-wall surfaces", _num(summary.get("n_party_surfaces"), 0)),
            ("Windows", _num(summary.get("n_windows"), 0)),
            ("Balcony doors", _num(summary.get("n_balcony_doors"), 0)),
            ("Opening area", _num(summary.get("window_area_m2"), 1, "m²")),
            ("Neighbour shading surfaces", _num(summary.get("n_shading_surfaces"), 0)),
            ("Wall construction", _text(summary.get("wall_construction"))),
            ("Roof construction", _text(summary.get("roof_construction"))),
        ]) + "</table>")

    energy = _section(
        "Energy, on both area bases",
        "The primary basis is residential floor area, which is the basis every "
        "published kWh/m² in this project uses. The conditioned basis also "
        "counts the commercial ground storey, and is shown so the two are never "
        "confused for one another.",
        "<table class='grid'><thead><tr><th>End use</th><th>kWh</th>"
        "<th>kWh/m² residential</th><th>kWh/m² conditioned</th></tr></thead><tbody>"
        f"<tr><td>Space heating</td><td>{_num(results.get('space_heating_kwh'), 1)}</td>"
        f"<td>{_num(results.get('space_heating_kwh_m2'), 2)}</td>"
        f"<td>{_num(results.get('space_heating_kwh_m2_conditioned'), 2)}</td></tr>"
        f"<tr><td>Space cooling</td><td>{_num(results.get('cooling_kwh'), 1)}</td>"
        f"<td>{_num(results.get('cooling_kwh_m2'), 2)}</td>"
        f"<td>{_num(results.get('cooling_kwh_m2_conditioned'), 2)}</td></tr>"
        f"<tr><td>Hot water</td><td>{_num(results.get('dhw_kwh'), 1)}</td>"
        f"<td>{_num(results.get('dhw_kwh_m2'), 2)}</td><td>{DASH}</td></tr>"
        f"<tr><td>Fans</td><td>{DASH}</td><td>{_num(results.get('fans_kwh_m2'), 2)}</td><td>{DASH}</td></tr>"
        f"<tr><td>Pumps</td><td>{DASH}</td><td>{_num(results.get('pumps_kwh_m2'), 2)}</td><td>{DASH}</td></tr>"
        f"<tr><td>Lighting</td><td>{DASH}</td><td>{_num(results.get('lighting_kwh_m2'), 2)}</td><td>{DASH}</td></tr>"
        f"<tr><td>Equipment</td><td>{DASH}</td><td>{_num(results.get('equipment_kwh_m2'), 2)}</td><td>{DASH}</td></tr>"
        f"<tr><td><strong>Total site</strong></td><td><strong>{_num(results.get('total_site_kwh'), 1)}</strong></td>"
        f"<td><strong>{_num(results.get('total_site_kwh_m2'), 2)}</strong></td>"
        f"<td><strong>{_num(results.get('total_site_kwh_m2_conditioned'), 2)}</strong></td></tr>"
        "</tbody></table>"
        "<table>" + _rows([
            ("Electricity", _num(results.get("site_elec_kwh_m2"), 2, "kWh/m²")),
            ("Gas", _num(results.get("site_gas_kwh_m2"), 2, "kWh/m²")),
            ("Residential share of site energy",
             _num(results.get("residential_site_kwh"), 1, "kWh")),
            ("Commercial share of site energy",
             f"{_num(results.get('terciario_site_kwh'), 1, 'kWh')} "
             f"({_num(results.get('terciario_share_pct'), 1)}%)"),
            ("How the split was made", _text(results.get("residential_split_basis"))),
            ("Area basis", _text(results.get("area_basis"))),
            ("Carbon, HVAC only", _num(carbon.get("hvac_co2_t_yr"), 2, "tCO₂/yr")),
            ("Carbon, whole site", _num(carbon.get("total_site_co2_t_yr"), 2, "tCO₂/yr")),
        ]) + "</table>")

    qa = _section(
        "Quality checks",
        "The model's own description compared against what EnergyPlus reported "
        "back, with the tolerance each comparison was allowed.",
        _qa_table(layers.get("qa")))

    severes = summary.get("severes")
    benign = summary.get("severes_benign_shading_ems")
    unexplained = summary.get("severes_unexplained")
    diagnostics = _section(
        "EnergyPlus diagnostics",
        "Severe messages are classified, not silenced: the shading-control EMS "
        "message is a known and measured false alarm, and any severe outside "
        "that one class invalidates the building's result.",
        "<table>" + _rows([
            ("Warnings", _num(summary.get("warnings"), 0)),
            ("Severe, total", _num(severes, 0)),
            ("Severe, classified benign (shading EMS)", _num(benign, 0)),
            ("Severe, unexplained", _num(unexplained, 0)),
            ("Fatal", _num(summary.get("fatals"), 0)),
            # Unmet hours are not repeated here: the QA table above already
            # carries them with the tolerance they were judged against, and a
            # second copy without that tolerance would read as a bare number.
            ("Occupancy plausibility", _text(summary.get("occupancy_plausibility"))),
        ]) + "</table>")

    failure = ""
    if status and status != "ok":
        failure = _section(
            "Why this building has no result",
            "Recorded by the run itself, verbatim.",
            "<table>" + _rows([
                ("Status", _esc(status)),
                ("Reason", _text(row.get("reason"))),
                ("Message", _text(row.get("message") or row.get("error"))),
            ]) + "</table>")

    base = f"/api/stock/runs/{run}/buildings/{reference}"
    links = " · ".join(
        f'<a href="{_esc(base)}/{_esc(name)}">{_esc(label)}</a>'
        for name, label in (
            ("eplustbl.htm", "EnergyPlus table"),
            ("deep_layers.json", "deep_layers.json"),
            ("model_python.osm", "OpenStudio model"),
            ("eplusout.err", "EnergyPlus errors"),
            ("verified_profile.json", "verified_profile.json"),
        ))

    body = (kpi + failure + identity + geometry + energy + qa + diagnostics
            + _layer_sections(stages))

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(summary.get('refparcela') or reference)} · building report</title>"
        f"<style>{_STYLE}</style></head><body><main>"
        f"<h1><code>{_esc(summary.get('refparcela') or reference)}</code>{badge}</h1>"
        f'<p class="sub">Building report · run <code>{_esc(run)}</code> · '
        "every figure below is read from the files this run preserved, and nothing "
        "on this page is recomputed.</p>"
        f"{body}"
        f"<footer>Preserved files: {links}</footer>"
        "</main></body></html>\n")
