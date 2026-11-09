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

    def test_place_path_cuts_the_house_to_the_contour(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("def _seat_on_polygon", text)
        self.assertNotIn("def _footprint_span", text)
        self.assertIn("def _clip_to_polygon", text)
        self.assertIn('modifier.operation = "INTERSECT"', text)
        self.assertIn("_clip_to_polygon(obj, loops)", text)
        self.assertIn('modifier.name == "Полигон"', text)
        self.assertIn("_clean_loops(raw) or raw", text)
        self.assertIn("_drop_clip(obj)", text)
        self.assertIn("obj.hide_select = False", text)

    def test_clean_ring_drops_a_point_on_a_straight_wall_and_keeps_the_corner(self) -> None:
        ns = _functions(
            "_ring_area",
            "_almost_same",
            "_on_straight_wall",
            "_clean_ring",
            "_largest_polygon",
            "_rings_from_polygon",
            "_repair_rings",
            "_clean_loops",
        )
        square = [
            (0.0, 0.0, 0.0),
            (5.0, 0.0, 0.0),
            (10.0, 0.0, 0.0),
            (10.0, 10.0, 0.0),
            (0.0, 10.0, 0.0),
            (0.0, 0.0, 0.0),
        ]
        cleaned = ns["_clean_ring"](square)
        self.assertEqual(len(cleaned), 4)
        self.assertAlmostEqual(abs(ns["_ring_area"](cleaned)), 100.0, places=3)

        kink = [
            (0.0, 0.0, 0.0),
            (5.0, 0.05, 0.0),
            (10.0, 0.0, 0.0),
            (10.0, 10.0, 0.0),
            (0.0, 10.0, 0.0),
        ]
        self.assertEqual(len(ns["_clean_ring"](kink)), 5)

        hole = [
            (4.0, 4.0, 0.0),
            (6.0, 4.0, 0.0),
            (6.0, 6.0, 0.0),
            (4.0, 6.0, 0.0),
        ]
        loops = ns["_clean_loops"]([cleaned, hole])
        self.assertEqual(len(loops), 2)
        self.assertGreater(ns["_ring_area"](loops[0]), 0.0)
        self.assertLess(ns["_ring_area"](loops[1]), 0.0)

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
        self.assertIn('DEFAULT_BUILDING_PRESET = "main002"', text)
        self.assertIn("return found[-1]", text)
