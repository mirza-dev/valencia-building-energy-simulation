"""Register Valencia the way an operator would, without any test isolation.

This lives beside `conftest.py` rather than inside it because importing
`conftest` re-points `WORKBENCH_DB_PATH` and every other root at a throwaway
pytest directory as a module-level side effect.  That is correct for pytest and
wrong for anything else: the browser suite, which has its own isolated roots
already, imported the helper and silently provisioned a database nobody would
ever read.  A module with no import-time side effects can be called from either
suite and writes wherever the caller's environment says.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def provision_reference_city() -> list[str]:
    """Register and activate Valencia the way an operator would.

    The product ships no city: an empty install reports its inputs as missing
    until they are uploaded.  Tests that need a city therefore provision one,
    which is also the honest arrangement - what a run reads is what somebody
    put in, never a shipped default.  The files still live in `data/`, because
    the frozen regression and `verified_model --verify` read them directly.

    Callable directly as well as through the fixture, because several tests
    install their own isolation inside the test body and must provision after
    that, not before it.
    """
    from workbench import db, integrity, service

    # The app normally does this on startup; provisioning happens first here,
    # so the schema has to exist before the rows do.
    service.bootstrap()
    project = PROJECT_ROOT
    inputs = [
        ("valencia-city", "gis", "Valencia city buildings",
         project / "data/gis/DatosRai_ciudadValencia.shp"),
        ("tipo15-ledger", "companion", "Tipo15 dwelling ledger",
         project / "data/reference/Tipo15_soloV(in).csv"),
        ("plantilla-v2", "template", "PlantillaOS_v2",
         project / "data/templates/PlantillaOS_v2.osm"),
        ("valencia-iwec", "weather", "Valencia IWEC",
         project / "data/weather/ESP_Valencia.082840_IWEC.epw"),
        ("valencia-iwec-ddy", "ddy", "Valencia IWEC design days",
         project / "data/weather/ESP_Valencia.082840_IWEC.ddy"),
    ]
    registered = []
    for dataset_id, kind, name, path in inputs:
        if not path.exists():
            continue
        snapshot = integrity.ensure_snapshot(path, kind=kind)
        metadata = {"managed": False, "suffix": path.suffix.lower()}
        if kind == "gis":
            gdf = service.read_gdf(path)
            metadata.update({
                "rows": len(gdf), "crs": str(gdf.crs),
                "columns": list(gdf.columns),
                "geometry_types": sorted(gdf.geometry.geom_type.dropna().unique().tolist()),
            })
        elif kind == "companion":
            from workbench.data_dictionary import companion_bootstrap_metadata
            metadata.update(companion_bootstrap_metadata())
        else:
            metadata.update(service._inspect_uploaded_dataset(kind, path))
        db.upsert_dataset({
            "id": dataset_id, "kind": kind, "name": name, "path": str(path),
            "sha256": snapshot["snapshot_hash"], "snapshot_hash": snapshot["snapshot_hash"],
            "verification_status": "VERIFIED", "read_only": True,
            "metadata": metadata | {"snapshot_components": snapshot["components"]},
        })
        registered.append(dataset_id)
    db.update_project_settings({
        field: dataset_id for field, dataset_id in (
            ("building_dataset_id", "valencia-city"),
            ("neighbor_dataset_id", "valencia-city"),
            ("tipo15_dataset_id", "tipo15-ledger"),
            ("template_dataset_id", "plantilla-v2"),
            ("weather_dataset_id", "valencia-iwec"),
            ("ddy_dataset_id", "valencia-iwec-ddy"),
        ) if dataset_id in registered
    })
    return registered
