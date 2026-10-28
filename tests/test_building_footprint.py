from __future__ import annotations

import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay" / "building_export.py"


def _functions(*names: str) -> dict:
    text = SOURCE.read_text(encoding="utf-8")
    module = ast.parse(text)
    wanted = set(names)
    namespace: dict = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(ast.get_source_segment(text, node), namespace)
    return namespace


class FootprintLoopTests(unittest.TestCase):
    def test_ring_keeps_the_outer_contour_and_the_hole(self) -> None:
        ns = _functions("_boundary_loops", "_ring_area", "_oriented_loops")
        faces = [
            (0, 1, 5, 4),
            (1, 2, 6, 5),
            (2, 3, 7, 6),
            (3, 0, 4, 7),
        ]
        loops = ns["_boundary_loops"](faces)
        self.assertEqual(len(loops), 2)
        self.assertEqual({frozenset(loop) for loop in loops}, {frozenset(range(4)), frozenset(range(4, 8))})

        vertices = [
            (0.0, 0.0, 0.0),
            (10.0, 0.0, 0.0),
            (10.0, 10.0, 0.0),
            (0.0, 10.0, 0.0),
            (4.0, 4.0, 0.0),
            (6.0, 4.0, 0.0),
            (6.0, 6.0, 0.0),
            (4.0, 6.0, 0.0),
        ]
        oriented = ns["_oriented_loops"](vertices, faces, (5.0, 5.0))
        self.assertEqual(len(oriented), 2)
        self.assertGreater(ns["_ring_area"](oriented[0]), 0.0)
        self.assertLess(ns["_ring_area"](oriented[1]), 0.0)
        self.assertAlmostEqual(abs(ns["_ring_area"](oriented[0])), 100.0, places=3)

    def test_place_path_does_not_scale_the_footprint_to_the_node_box(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("def _seat_on_polygon", text)
        self.assertNotIn("def _footprint_span", text)
        self.assertIn('modifier.operation = "INTERSECT"', text)
        self.assertIn('modifier.name == "Полигон"', text)

    def test_reloaded_node_group_still_counts_as_the_house(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        module = ast.parse(text)
        node = next(
            item for item in module.body if isinstance(item, ast.FunctionDef) and item.name == "_is_building_group"
        )
        namespace = {"BUILDING_GROUP": "1_Building"}
        exec(ast.get_source_segment(text, node), namespace)
        self.assertTrue(namespace["_is_building_group"]("1_Building"))
        self.assertTrue(namespace["_is_building_group"]("1_Building.001"))
        self.assertFalse(namespace["_is_building_group"]("modules"))

    def test_auto_houses_get_the_house_preset_not_only_an_exact_tower(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        self.assertIn("apply_preset(obj, DEFAULT_BUILDING_PRESET)", text)
        self.assertIn("return found[-1]", text)
