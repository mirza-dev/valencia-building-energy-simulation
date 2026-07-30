from copy import deepcopy

from workbench.renderer_provenance import visual_geometry_descriptor


def _context_wall(name, vertices):
    return {
        "id": name,
        "name": name,
        "category": "context",
        "vertices": vertices,
        "area_m2": 30.0,
    }


def test_visual_geometry_fingerprint_is_order_independent():
    walls = [
        _context_wall("south", [[0, 0, 0], [10, 0, 0], [10, 0, 3], [0, 0, 3]]),
        _context_wall("east", [[10, 0, 0], [10, 10, 0], [10, 10, 3], [10, 0, 3]]),
        _context_wall("north", [[10, 10, 0], [0, 10, 0], [0, 10, 3], [10, 10, 3]]),
        _context_wall("west", [[0, 10, 0], [0, 0, 0], [0, 0, 3], [0, 10, 3]]),
    ]
    scene = {"shading": walls}
    reordered = deepcopy(scene)
    reordered["shading"] = list(reversed(reordered["shading"]))

    first = visual_geometry_descriptor(scene)
    second = visual_geometry_descriptor(reordered)

    assert first["context_roof_count"] == 1
    assert first["context_roofs"][0]["area_m2"] == 100.0
    assert first["fingerprint"] == second["fingerprint"]
