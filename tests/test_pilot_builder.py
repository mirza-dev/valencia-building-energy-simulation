import pytest

import model_builder as mb
from workbench.scene import extract_scene
from workbench.service import read_building, workbench_base_config


@pytest.mark.integration
def test_pilot_geometry_regression():
    config = workbench_base_config()
    row = read_building("4252702YJ2745A", config.data.building_path)
    geometry = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(
        geometry, row["refparcela"], config.data.neighbor_path, config=config)
    party = mb.find_party_walls(
        geometry, row["refparcela"], config.data.neighbor_path,
        neighbors=neighbors, config=config)
    result = mb.build_model_with_config(row, party, config, neighbors=neighbors)
    stats = result.stats

    assert len(neighbors) == 11
    assert stats["footprint_m2"] == 562.0
    assert stats["n_floors_total"] == 6
    assert stats["n_party_surfaces"] == 6
    assert stats["n_windows"] == 100
    assert stats["n_balcony_doors"] == 30
    assert stats["window_area_m2"] == 219.6
    assert stats["n_shading_surfaces"] == 69
    assert stats["wall_construction"] == "Muro IVE ladrillo (U_base=1.33) + TB(dU=0.10)"
    assert stats["roof_construction"] == "Cubierta plana no aislada"

    scene = extract_scene(result.osm, stats)
    assert len(scene["surfaces"]) == 36
    assert len(scene["subsurfaces"]) == 130
    assert len(scene["shading"]) == 99
