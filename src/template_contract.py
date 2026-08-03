"""What the pipeline needs from an OpenStudio template, stated once.

`model_builder.load_template()` already reads its path from
`BuildConfig.data.template_path`, so pointing the chain at a different `.osm`
needs no code change at all.  What breaks is subtler: the frozen builder and the
deep layers reach into the template **by object name** in about twenty places,
and a template that spells any of them differently fails somewhere in the middle
of a build - or, worse, in the case of the shading blind, only prints a warning
and carries on with no solar control at all.

This module makes that surface explicit:

* `TEMPLATE_ROLES` names every object the chain binds to, what it is for, and
  whether the chain can run without it.
* `validate_template()` checks a template up front and reports *everything*
  missing at once, with the names it did find - never a silent fallback.
* `normalise_template()` renames a foreign template's objects onto the
  canonical names and writes a content-addressed copy, so a different template
  can be used **without touching the frozen builder**.

The last point is the reason this is a separate module rather than a patch:
`model_builder.py` is SHA-locked and five Workbench capabilities key off that
hash.  Renaming into a normalised copy leaves it untouched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openstudio


class TemplateError(ValueError):
    """A template does not satisfy the contract the pipeline depends on."""


@dataclass(frozen=True)
class Role:
    """One object the pipeline looks up by name."""
    canonical: str
    collection: str          # the model getter that returns this kind of object
    purpose: str
    required: bool = True


# The getter names are used through `getattr(model, ...)` so that this table
# stays a description rather than a pile of imports.
TEMPLATE_ROLES: dict[str, Role] = {
    # -- space types -------------------------------------------------------
    "residential_space_type": Role(
        "Espacio Tipo Vivienda CTE", "getSpaceTypes",
        "Dwelling storeys: CTE loads, schedules, infiltration and DSOA"),
    "buffer_space_type": Role(
        "Espacio Tipo No habitable 1ACH", "getSpaceTypes",
        "Unconditioned ground storey in the pilot regime"),
    "ground_terciario_space_type": Role(
        "Espacio Tipo Terciario 8h Media CTE", "getSpaceTypes",
        "Conditioned commercial ground storey (Rai's regime)"),

    # -- constructions -----------------------------------------------------
    "roof_uninsulated": Role(
        "Cubierta plana no aislada", "getConstructions",
        "Fallback roof when no U-value is given"),
    "party_wall": Role(
        "Medianera Referencia B", "getConstructions",
        "Adiabatic party walls between attached buildings"),
    "ground_slab": Role(
        "Solera con aislante", "getConstructions",
        "Ground slab under the conditioned ground storey (Rai: U 0.501)"),
    "interzone_ceiling": Role(
        "Techo Interior Referencia", "getConstructions",
        "Interzone ceiling, opt-in Rai replica only", required=False),
    "interzone_floor": Role(
        "Suelo Interior Referencia", "getConstructions",
        "Interzone floor, opt-in Rai replica only", required=False),

    # -- materials the layered envelope is assembled from ------------------
    "material_mortar": Role(
        "Mortero de cemento referencia", "getMaterials",
        "Outer render of every layered wall"),
    "material_plaster": Role(
        "Enlucido de yeso d < 1000_15mm", "getMaterials",
        "Inner plaster of every layered wall"),
    "material_brick_perforated": Role(
        "Ladrillo Perforado Referencia", "getMaterials",
        "Load-bearing leaf; also the calibration layer for solid walls"),
    "material_brick_hollow": Role(
        "Ladrillo Hueco Referencia", "getMaterials",
        "Inner leaf of the IVE cavity wall; the calibrated layer"),
    "material_brick_double_hollow": Role(
        "Ladrillo Doble Hueco Referencia", "getMaterials",
        "Inner leaf of the insulated wall"),
    "material_air_cavity": Role(
        "Camara de aire en paredes R 0.18", "getMaterials",
        "Wall cavity"),
    "material_party_insulation": Role(
        "Aislante Medianera Referencia B", "getMaterials",
        "Calibration layer for insulated walls and roofs"),
    "material_concrete_slab": Role(
        "FU Entrevigado de hormigon aligerado -Canto 300 mm", "getMaterials",
        "Roof structural deck"),
    "material_asphalt": Role(
        "Asfalto 10mm", "getMaterials", "Roof waterproofing"),
    "material_gravel": Role(
        "Arena y grava [1700 < d < 2200]  6 cm", "getMaterials",
        "Roof ballast"),

    # -- schedules ---------------------------------------------------------
    "thermostat_heating": Role(
        "T Calefaccion vivienda CTE", "getScheduleRulesets",
        "CTE heating setpoint program, sentinel-based off periods"),
    "thermostat_cooling": Role(
        "T refrigeracion vivienda CTE", "getScheduleRulesets",
        "CTE cooling setpoint program, sentinel-based off periods"),
    "dhw_demand_schedule": Role(
        "Demanda ACS Vivienda", "getScheduleRulesets",
        "Domestic hot water draw profile"),

    # -- equipment ---------------------------------------------------------
    "dhw_reference_definition": Role(
        "Demanda ACS 150l/dia", "getWaterUseEquipmentDefinitions",
        "Read only, to recover the template's litres/day -> m3/s scale"),
    "window_frame": Role(
        "Carpinteria_Aluminio_simple", "getWindowPropertyFrameAndDividers",
        "Aluminium carpentry attached to every glazed sub-surface"),
    "shading_blind": Role(
        "Lamas Horizontales 25mm cada 20mm", "getBlinds",
        "Persiana. NOTE: the builder only warns when this is missing and then "
        "runs with no solar control - which is why it is checked here instead"),
}

# The dwelling airtightness object is matched by substring, not by name
# (`model_builder.py:675`), because the residential space type stacks three
# infiltration objects and only the constant-airtightness one may be moved by
# the LHS knob; the occupancy-coupled CTE ventilation and the summer night
# ventilation must keep their template values.
#
# Measured on PlantillaOS_v2: five objects in the template match these
# substrings, but four of them belong to space types this pipeline never
# instantiates (RT2012 and EN 16798 variants), so the knob's extra writes reach
# nothing that is simulated. What matters - and what is checked below - is that
# exactly one match sits on the *residential space type actually in use*.
INFILTRATION_SUBSTRINGS = ("constante", "Viv")


@dataclass(frozen=True)
class TemplateSet:
    """A template that has been checked against the contract."""
    name: str
    source_path: Path
    resolved_path: Path      # what the builder should actually load
    sha256: str
    aliases: dict[str, str]
    normalised: bool
    fingerprint: str

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fingerprint": self.fingerprint,
            "source": self.source_path.name,
            "sha256": self.sha256,
            "normalised": self.normalised,
            "aliases": self.aliases,
        }


# ---------------------------------------------------------------------------
# Reading and checking
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_model(path: Path) -> openstudio.model.Model:
    loaded = openstudio.osversion.VersionTranslator().loadModel(
        openstudio.toPath(str(path)))
    if loaded.isNull():
        raise TemplateError(f"{path.name}: OpenStudio could not open this as a model")
    return loaded.get()


def _names(model: openstudio.model.Model, collection: str) -> list[str]:
    getter = getattr(model, collection, None)
    if getter is None:
        raise TemplateError(f"unknown OpenStudio collection '{collection}'")
    return [obj.nameString() for obj in getter()]


def check_roles(model: openstudio.model.Model,
                aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """Report which roles this template satisfies and which it does not.

    Returns rather than raises: callers want the whole picture, not the first
    problem.
    """
    aliases = dict(aliases or {})
    unknown = set(aliases) - set(TEMPLATE_ROLES)
    if unknown:
        raise TemplateError(
            f"alias map names roles that do not exist: {sorted(unknown)}.\n"
            f"Known roles: {sorted(TEMPLATE_ROLES)}")

    found: dict[str, str] = {}
    missing: list[dict[str, Any]] = []
    for key, role in TEMPLATE_ROLES.items():
        wanted = aliases.get(key, role.canonical)
        present = _names(model, role.collection)
        if wanted in present:
            found[key] = wanted
        else:
            missing.append({
                "role": key, "looked_for": wanted, "collection": role.collection,
                "required": role.required, "purpose": role.purpose,
                "template_has": sorted(present)[:40],
            })

    return {"found": found, "missing": missing,
            "infiltration_matches": _dwelling_infiltration(model, aliases)}


def _dwelling_infiltration(model: openstudio.model.Model,
                           aliases: dict[str, str]) -> list[str]:
    """Infiltration objects on the residential space type that the knob would move.

    Scoped to that space type on purpose: a template may carry the same naming
    pattern on space types this pipeline never instantiates, and those are not
    the pipeline's problem.
    """
    role = TEMPLATE_ROLES["residential_space_type"]
    wanted = aliases.get("residential_space_type", role.canonical)
    space_type = next((st for st in model.getSpaceTypes()
                       if st.nameString() == wanted), None)
    if space_type is None:
        return []
    return [inf.nameString() for inf in space_type.spaceInfiltrationDesignFlowRates()
            if all(token in inf.nameString() for token in INFILTRATION_SUBSTRINGS)]


def validate_template(path: str | Path,
                      aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """Refuse a template that cannot carry the pipeline.

    Optional roles are reported but do not block: they belong to the opt-in Rai
    replica, not to a production run.
    """
    path = Path(path)
    if not path.exists():
        raise TemplateError(f"template not found: {path}")
    model = load_model(path)

    # Library-only gate: the template supplies constructions, schedules and
    # space types - never geometry or systems.  The builder re-processes EVERY
    # Space in the model as one of its own storeys (model_builder sorts
    # osm.getSpaces() by z and types them), so a template that arrives with a
    # Space would have it renamed, zoned and counted as a floor of the
    # building; pre-existing loops and design days would ride along silently.
    # The default PlantillaOS_v2 carries none of these (measured 2026-08-03).
    contraband = {
        "Space": len(model.getSpaces()),
        "Surface": len(model.getSurfaces()),
        "ThermalZone": len(model.getThermalZones()),
        "AirLoopHVAC": len(model.getAirLoopHVACs()),
        "PlantLoop": len(model.getPlantLoops()),
        "DesignDay": len(model.getDesignDays()),
    }
    found = {kind: count for kind, count in contraband.items() if count}
    if found:
        listed = ", ".join(f"{kind} x{count}" for kind, count in sorted(found.items()))
        raise TemplateError(
            f"{path.name} is not a library-only template: it already contains "
            f"{listed}. The pipeline builds all geometry and systems itself, and "
            f"pre-existing ones would be silently absorbed into the building. "
            f"Strip them from the template before using it.")

    report = check_roles(model, aliases)

    blocking = [item for item in report["missing"] if item["required"]]
    problems: list[str] = []
    for item in blocking:
        problems.append(
            f"[{item['role']}] '{item['looked_for']}' not found among "
            f"{item['collection']} - {item['purpose']}\n"
            f"      template has: {', '.join(item['template_has'][:8]) or '(none)'}"
            + (" ..." if len(item["template_has"]) > 8 else ""))

    matches = report["infiltration_matches"]
    if len(matches) != 1 and not any(item["role"] == "residential_space_type"
                                     for item in blocking):
        problems.append(
            f"[dwelling_infiltration] the airtightness knob moves the infiltration "
            f"object on the residential space type whose name contains "
            f"{list(INFILTRATION_SUBSTRINGS)}; this template has {len(matches)}: "
            f"{matches or '(none)'}. Exactly one is required - with none the knob "
            f"silently does nothing, with several it would move ventilation objects "
            f"that must keep their template values.")

    if problems:
        raise TemplateError(
            f"{path.name} does not satisfy the template contract:\n  - "
            + "\n  - ".join(problems)
            + "\n\nUse an alias map to bind differently named objects.")
    return report


# ---------------------------------------------------------------------------
# Normalising a foreign template
# ---------------------------------------------------------------------------
def normalise_template(path: str | Path, aliases: dict[str, str],
                       cache_dir: str | Path) -> Path:
    """Rename aliased objects onto the canonical names, into a cached copy.

    The frozen builder then finds the names it has always looked for, and
    `model_builder.py` stays byte-identical.  The copy is content-addressed, so
    the same template and alias map always resolve to the same file and the
    rename runs once.
    """
    path = Path(path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    identity = json.dumps({"template": _sha256(path), "aliases": aliases},
                          sort_keys=True)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    target = cache_dir / f"{path.stem}_{digest}.osm"
    stamp = target.with_suffix(".osm.sha256")
    if target.exists():
        # The cached copy is what EnergyPlus actually runs, so its bytes are
        # checked before reuse.  Without this, editing a material inside the
        # cache would still satisfy the name contract and still report the same
        # fingerprint, because that was computed from the source template.
        recorded = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else None
        if recorded and recorded == _sha256(target):
            return target
        raise TemplateError(
            f"normalised template cache is not what it was written as: "
            f"{target.name}. Delete it and let it be rebuilt.")

    if not aliases:
        # nothing to rename: a copy keeps the caller's path handling uniform
        shutil.copy2(path, target)
        stamp.write_text(_sha256(target), encoding="utf-8")
        return target

    model = load_model(path)
    for key, their_name in aliases.items():
        role = TEMPLATE_ROLES[key]
        getter = getattr(model, role.collection)
        source = next((o for o in getter() if o.nameString() == their_name), None)
        if source is None:
            raise TemplateError(
                f"{path.name}: alias for '{key}' points at '{their_name}', which is "
                f"not among {role.collection}")
        clash = next((o for o in getter()
                      if o.nameString() == role.canonical), None)
        if clash is not None and clash.handle() != source.handle():
            raise TemplateError(
                f"{path.name}: cannot rename '{their_name}' to '{role.canonical}' - "
                f"a different object already uses that name. Rename it in the "
                f"template first, or alias that one too.")
        source.setName(role.canonical)

    staging = target.with_suffix(".osm.tmp")
    if not model.save(openstudio.toPath(str(staging)), True):
        raise TemplateError(f"normalised template could not be written: {staging}")
    staging.replace(target)        # atomic: a reader never sees a half-written model
    stamp.write_text(_sha256(target), encoding="utf-8")
    return target


def load_template_set(spec: dict[str, Any] | str | Path,
                      cache_dir: str | Path,
                      base_dir: Path | None = None) -> TemplateSet:
    """Validate a template (optionally aliased) and return what to load.

    `spec` is either a path, or a mapping with `template` and optional
    `aliases` / `name`.
    """
    if isinstance(spec, (str, Path)):
        candidate = Path(spec)
        if candidate.suffix.lower() == ".json":
            base_dir = candidate.parent
            spec = json.loads(candidate.read_text(encoding="utf-8"))
        else:
            spec = {"template": str(candidate)}

    if "template" not in spec:
        raise TemplateError("template spec needs a 'template' key")

    source = Path(spec["template"])
    if not source.is_absolute() and base_dir is not None:
        for option in (base_dir / source, base_dir.parent / source):
            if option.exists():
                source = option
                break
    aliases = dict(spec.get("aliases") or {})
    name = str(spec.get("name") or source.stem)

    resolved = (normalise_template(source, aliases, cache_dir)
                if aliases else source)
    # Validated without the alias map either way: after normalisation the copy
    # carries the canonical names, and without aliases there was nothing to
    # translate. So what is checked is always the file the builder will open.
    validate_template(resolved)

    fingerprint = hashlib.sha256(json.dumps(
        {"sha256": _sha256(source), "aliases": aliases},
        sort_keys=True).encode("utf-8")).hexdigest()
    return TemplateSet(
        name=name, source_path=source, resolved_path=resolved,
        sha256=_sha256(source), aliases=aliases,
        normalised=bool(aliases), fingerprint=fingerprint)


# ---------------------------------------------------------------------------
def _project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "templates").is_dir():
            return parent
    raise TemplateError("project root not found (no data/templates above src/)")


DEFAULT_TEMPLATE = _project_root() / "data" / "templates" / "PlantillaOS_v2.osm"


def main(argv: list[str] | None = None) -> int:
    """Check a template: `python src/template_contract.py [path] [--aliases f.json]`"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Check an OpenStudio template against the pipeline contract")
    parser.add_argument("template", nargs="?", default=str(DEFAULT_TEMPLATE), type=Path)
    parser.add_argument("--aliases", type=Path, help="JSON map of role -> object name")
    args = parser.parse_args(argv)

    aliases = (json.loads(args.aliases.read_text(encoding="utf-8"))
               if args.aliases else None)
    try:
        report = validate_template(args.template, aliases)
    except TemplateError as exc:
        print(f"REFUSED: {exc}")
        return 2

    optional_missing = [m["role"] for m in report["missing"]]
    print(f"OK  {args.template.name}")
    print(f"  {len(report['found'])}/{len(TEMPLATE_ROLES)} roles bound")
    print(f"  dwelling infiltration object: {report['infiltration_matches'][0]}")
    if optional_missing:
        print(f"  optional roles absent: {', '.join(optional_missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
