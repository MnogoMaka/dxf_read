from __future__ import annotations

import sys
import unittest
from math import pi
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"))

import json

from placement_math import (
    bench_path,
    tagged_mesh_parts,
    bench_positions,
    blocks_playground,
    cluster_boxes,
    small_sheet_indexes,
    equipment_quads,
    forward_angle,
    impact_margin,
    is_playground_layer,
    mirrored_keep_from,
    pack_playground,
    plan_image,
    playground_capacity,
    polygon_area_xy,
    polygon_centroid_xy,
    areas_to_square_meters,
    footprint_longer_side,
    quads_overlap,
    safety_inside,
    scatter_points,
    split_far_parts,
)


class MirroredCopyTests(unittest.TestCase):
    def test_lounger_pair_keeps_the_positive_half(self) -> None:
        negative = [-3.4 + index * 0.05 for index in range(40)]
        positive = [1.0 + index * 0.05 for index in range(40)]
        cut = mirrored_keep_from(negative + positive)
        self.assertIsNotNone(cut)
        assert cut is not None
        self.assertGreater(cut, 0.5)
        self.assertTrue(all(value < cut for value in negative))
        self.assertTrue(all(value >= cut for value in positive))

    def test_single_bench_is_not_split(self) -> None:
        xs = [-0.95 + index * 0.02 for index in range(90)]
        self.assertIsNone(mirrored_keep_from(xs))


class BenchPathTests(unittest.TestCase):
    def test_two_points_match_the_straight_segment(self) -> None:
        start, end = (0.0, 0.0), (10.0, 0.0)
        straight = bench_positions(start, end, 3.0, 0.68)
        along = bench_path([start, end], 3.0, 0.68)
        self.assertEqual(len(along), len(straight))
        for (x, y, angle), point in zip(along, straight):
            self.assertAlmostEqual(x, point[0], places=4)
            self.assertAlmostEqual(y, point[1], places=4)
            self.assertAlmostEqual(angle, forward_angle(1.0, 0.0) + pi / 2, places=4)

    def test_corner_follows_the_path_instead_of_the_chord(self) -> None:
        placed = bench_path([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], 2.0, 0.0)
        near_corner = [
            item for item in placed
            if abs(item[0] - 10.0) < 1.0 and abs(item[1]) < 1.0
        ]
        self.assertTrue(near_corner)
        self.assertFalse(any(abs(item[0] - 5.0) < 1.0 and abs(item[1] - 5.0) < 1.0 for item in placed))
        self.assertAlmostEqual(placed[-1][2], forward_angle(0.0, 1.0) + pi / 2, delta=0.4)

    def test_uniform_heading_uses_one_angle(self) -> None:
        placed = bench_path(
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)],
            10.0,
            0.0,
            heading="uniform",
            turn=pi,
        )
        angles = {round(item[2], 5) for item in placed}
        self.assertEqual(len(angles), 1)

    def test_roundness_zero_stays_on_the_segments(self) -> None:
        placed = bench_path([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], 2.0, 0.0, roundness=0.0)
        self.assertTrue(placed)
        for x, y, _angle in placed:
            on_bottom = abs(y) < 1e-6 and -1e-6 <= x <= 10.0
            on_side = abs(x - 10.0) < 1e-6 and -1e-6 <= y <= 10.0
            self.assertTrue(on_bottom or on_side)

    def test_full_roundness_matches_the_default_curve(self) -> None:
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
        self.assertEqual(
            bench_path(points, 2.0, 0.0, roundness=1.0),
            bench_path(points, 2.0, 0.0),
        )

    def test_half_roundness_sits_between_the_line_and_the_arc(self) -> None:
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
        straight = bench_path(points, 2.0, 0.0, roundness=0.0)
        curved = bench_path(points, 2.0, 0.0, roundness=1.0)
        middle = bench_path(points, 2.0, 0.0, roundness=0.5)

        def off_corner(row) -> float:
            return max(min(abs(y), abs(x - 10.0)) for x, y, _angle in row)

        self.assertGreater(off_corner(curved), off_corner(straight) + 0.05)
        self.assertGreater(off_corner(middle), off_corner(straight) + 0.02)
        self.assertLess(off_corner(middle), off_corner(curved) - 0.02)


class SheetClusterTests(unittest.TestCase):
    def test_distant_boxes_stay_apart(self) -> None:
        boxes = [(0.0, 0.0, 20.0, 10.0), (1000.0, 0.0, 1020.0, 10.0)]
        groups = cluster_boxes(boxes, 200.0)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0], [0])
        self.assertEqual(groups[1], [1])

    def test_a_chain_of_neighbours_is_one_drawing(self) -> None:
        boxes = [
            (0.0, 0.0, 10.0, 10.0),
            (12.0, 0.0, 22.0, 10.0),
            (40.0, 0.0, 50.0, 10.0),
        ]
        groups = cluster_boxes(boxes, 30.0)
        self.assertEqual(groups, [[0, 1, 2]])

    def test_order_is_left_to_right(self) -> None:
        boxes = [(500.0, 0.0, 510.0, 10.0), (0.0, 0.0, 10.0, 10.0)]
        groups = cluster_boxes(boxes, 10.0)
        self.assertEqual(groups, [[1], [0]])

    def test_sheets_below_three_quarters_of_the_largest_are_dropped(self) -> None:
        self.assertEqual(small_sheet_indexes([100, 80, 40]), [2])
        self.assertEqual(small_sheet_indexes([100, 75]), [])
        self.assertEqual(small_sheet_indexes([100, 74]), [1])
        self.assertEqual(small_sheet_indexes([12]), [])
        self.assertEqual(small_sheet_indexes([40, 40]), [])


def _square(width: float, depth: float) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    vertices = [(0.0, 0.0, 0.0), (width, 0.0, 0.0), (width, depth, 0.0), (0.0, depth, 0.0)]
    return vertices, [(0, 1, 2), (0, 2, 3)]


class PlaygroundPackTests(unittest.TestCase):
    def _assets(self) -> list[dict]:
        catalog = json.loads(
            (Path(__file__).resolve().parents[1] / "maf_blender_models" / "catalog.json").read_text(encoding="utf-8")
        )
        found = []
        for item in catalog:
            if item.get("family") != "playground":
                continue
            found.append({
                "id": item["id"],
                "width": item["footprint"][0],
                "depth": item["footprint"][1],
                "clearance": tuple(item["clearance"]),
                "motion": item["motion"],
            })
        return found

    def test_fall_margin_follows_the_table(self) -> None:
        self.assertEqual(impact_margin(1.0), 1.5)
        self.assertAlmostEqual(impact_margin(2.5), 1.5 + (2.0 / 3.0))
        self.assertAlmostEqual(impact_margin(5.98), 2.5)

    def test_slide_runout_is_longer_on_one_side(self) -> None:
        catalog = {item["id"]: item for item in self._assets()}
        front, back, left, right = catalog["play_01"]["clearance"]
        self.assertEqual(catalog["play_01"]["motion"], "slide")
        self.assertGreater(right, front)
        self.assertAlmostEqual(right - left, 2.0, places=2)
        self.assertEqual(catalog["swing_01"]["motion"], "swing")

    def test_small_polygon_stays_empty(self) -> None:
        vertices, faces = _square(4.0, 4.0)
        large = [item for item in self._assets() if item["id"] == "play_big"]
        self.assertEqual(pack_playground(vertices, faces, large), [])

    def test_seed_is_reproducible_and_changes_the_layout(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        first = pack_playground(vertices, faces, self._assets(), seed=41)
        repeated = pack_playground(vertices, faces, self._assets(), seed=41)
        changed = pack_playground(vertices, faces, self._assets(), seed=42)
        self.assertTrue(first)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, changed)

    def test_seed_changes_positions_for_a_single_model_type(self) -> None:
        vertices, faces = _square(40.0, 40.0)
        sports = [item for item in self._assets() if item["id"] == "play_big"]
        first = pack_playground(vertices, faces, sports, seed=7)
        changed = pack_playground(vertices, faces, sports, seed=8)
        self.assertTrue(first)
        self.assertNotEqual(first, changed)

    def test_large_polygon_fits_several_without_zone_conflicts(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        assets = {item["id"]: item for item in self._assets()}
        placed = pack_playground(vertices, faces, list(assets.values()))
        self.assertGreater(len(placed), 1)
        quads = []
        for item in placed:
            asset = assets[item["id"]]
            gap, safety = equipment_quads(
                asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]), inflate=0.25,
            )
            _body, zone = equipment_quads(
                asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
            )
            self.assertTrue(all(0.0 - 1e-6 <= x <= 40.0 + 1e-6 and 0.0 - 1e-6 <= y <= 30.0 + 1e-6 for x, y in zone))
            quads.append((asset["motion"], gap, zone))
        for index, (motion, gap, zone) in enumerate(quads):
            for other_motion, other_gap, other_zone in quads[index + 1:]:
                self.assertFalse(quads_overlap(gap, other_gap))
                if motion in {"swing", "slide"} or other_motion in {"swing", "slide"}:
                    self.assertFalse(quads_overlap(zone, other_zone))

    def test_fence_blocks_the_middle(self) -> None:
        vertices, faces = _square(20.0, 12.0)
        fence = ([(9.0, 0.0, 0.0), (11.0, 0.0, 0.0), (11.0, 12.0, 0.0), (9.0, 12.0, 0.0)], [(0, 1, 2), (0, 2, 3)])
        assets = [item for item in self._assets() if item["id"] == "swing_round"]
        clear = pack_playground(vertices, faces, assets)
        blocked = pack_playground(vertices, faces, assets, blocked=fence)
        self.assertGreaterEqual(len(clear), 1)
        self.assertLess(len(blocked), len(clear))

    def test_capacity_follows_area_and_stays_in_range(self) -> None:
        self.assertEqual(playground_capacity(10), 8)
        self.assertEqual(playground_capacity(20 * 15), 15)
        self.assertEqual(playground_capacity(20 * 80), 40)

    def test_trees_do_not_block_a_playground(self) -> None:
        self.assertFalse(blocks_playground("green", "scatter", "", "tree", True))
        self.assertFalse(blocks_playground("", "", "scatter", "", True))
        self.assertFalse(blocks_playground("pavement", "", "", "", False))
        self.assertTrue(blocks_playground("fence", "", "", "", False))
        self.assertTrue(blocks_playground("building", "", "", "", False))
        self.assertTrue(blocks_playground("", "area", "", "playground", True))
        self.assertTrue(blocks_playground("", "", "linear", "bench", True))

    def test_higher_limit_fits_more_models_inside_the_polygon(self) -> None:
        vertices, faces = _square(50.0, 40.0)
        asset = {
            "id": "box",
            "width": 1.2,
            "depth": 1.2,
            "clearance": (0.4, 0.4, 0.4, 0.4),
            "motion": "static",
        }
        few = pack_playground(vertices, faces, [asset], limit=4, seed=3)
        many = pack_playground(vertices, faces, [asset], limit=24, seed=3)
        self.assertEqual(len(few), 4)
        self.assertGreater(len(many), 12)
        for item in many:
            self.assertTrue(safety_inside(
                vertices, faces, 1.2, 1.2, (0.4, 0.4, 0.4, 0.4), item["angle"], (item["x"], item["y"]),
            ))

    def test_models_stay_out_of_the_hole(self) -> None:
        vertices = [
            (0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (20.0, 20.0, 0.0), (0.0, 20.0, 0.0),
            (8.0, 8.0, 0.0), (12.0, 8.0, 0.0), (12.0, 12.0, 0.0), (8.0, 12.0, 0.0),
        ]
        faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        asset = {
            "id": "box",
            "width": 1.2,
            "depth": 1.2,
            "clearance": (0.4, 0.4, 0.4, 0.4),
            "motion": "static",
        }
        placed = pack_playground(vertices, faces, [asset], limit=12, seed=2)
        self.assertGreater(len(placed), 1)
        for item in placed:
            self.assertFalse(8.0 < item["x"] < 12.0 and 8.0 < item["y"] < 12.0)
            self.assertTrue(safety_inside(
                vertices, faces, 1.2, 1.2, (0.4, 0.4, 0.4, 0.4), item["angle"], (item["x"], item["y"]),
            ))


class ConcaveGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vertices = [
            (0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (3.0, 3.0, 0.0),
            (2.0, 3.0, 0.0), (2.0, 1.0, 0.0), (1.0, 1.0, 0.0),
            (1.0, 3.0, 0.0), (0.0, 3.0, 0.0),
        ]
        self.faces = [tuple(range(8))]

    def test_concave_ngon_area_and_centroid_are_exact(self) -> None:
        self.assertAlmostEqual(polygon_area_xy(self.vertices, self.faces), 7.0)
        center = polygon_centroid_xy(self.vertices, self.faces)
        self.assertIsNotNone(center)
        assert center is not None
        self.assertAlmostEqual(center[0], 1.5)
        self.assertAlmostEqual(center[1], 9.5 / 7.0)

    def test_scatter_never_puts_points_in_the_notch(self) -> None:
        from shapely.geometry import Point, Polygon

        polygon = Polygon([(point[0], point[1]) for point in self.vertices])
        points = scatter_points(self.vertices, self.faces, 500, seed=17)
        self.assertEqual(len(points), 500)
        self.assertTrue(all(polygon.covers(Point(point[0], point[1])) for point in points))


class SafetyGeometryTests(unittest.TestCase):
    def test_obstacle_inside_safety_quad_is_rejected(self) -> None:
        vertices, faces = _square(4.0, 4.0)
        obstacle = (
            [(1.9, 1.9, 0.0), (2.1, 1.9, 0.0), (2.1, 2.1, 0.0), (1.9, 2.1, 0.0)],
            [(0, 1, 2), (0, 2, 3)],
        )
        clearance = (0.5, 0.5, 0.5, 0.5)
        self.assertTrue(safety_inside(vertices, faces, 1.0, 1.0, clearance, 0.0, (2.0, 2.0)))
        self.assertFalse(safety_inside(
            vertices, faces, 1.0, 1.0, clearance, 0.0, (2.0, 2.0), blocked=obstacle,
        ))

    def test_safety_quad_cannot_cover_a_concave_notch(self) -> None:
        vertices = [
            (0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (3.0, 3.0, 0.0),
            (2.0, 3.0, 0.0), (2.0, 1.0, 0.0), (1.0, 1.0, 0.0),
            (1.0, 3.0, 0.0), (0.0, 3.0, 0.0),
        ]
        self.assertFalse(safety_inside(
            vertices, [tuple(range(8))], 3.0, 3.0, (0.0, 0.0, 0.0, 0.0), 0.0, (1.5, 1.5),
        ))

    def test_scale_grows_body_and_clearance(self) -> None:
        vertices, faces = _square(3.9, 3.9)
        clearance = (0.5, 0.5, 0.5, 0.5)
        self.assertTrue(safety_inside(
            vertices, faces, 1.0, 1.0, clearance, 0.0, (1.95, 1.95), scale=1.0,
        ))
        self.assertFalse(safety_inside(
            vertices, faces, 1.0, 1.0, clearance, 0.0, (1.95, 1.95), scale=2.0,
        ))


class PlanImageTests(unittest.TestCase):
    def test_empty_plan_has_no_bytes(self) -> None:
        self.assertEqual(plan_image([]), b"")

    def test_segment_becomes_a_png(self) -> None:
        image = plan_image([(0.0, 0.0, 10.0, 0.0)])
        other = plan_image([(0.0, 0.0, 0.0, 10.0)])
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(image, other)


class PlaygroundLayerTests(unittest.TestCase):
    def test_play_and_sport_names_pass_and_waste_does_not(self) -> None:
        self.assertTrue(is_playground_layer("Детская площадка"))
        self.assertTrue(is_playground_layer("Игровая"))
        self.assertTrue(is_playground_layer("Спортивная площадка"))
        self.assertFalse(is_playground_layer("Площадка ТБО"))
        self.assertFalse(is_playground_layer("Тротуар"))

    def test_rubber_counts_as_a_playground_and_waste_does_not(self) -> None:
        from placement_math import is_playground_surface

        self.assertTrue(is_playground_surface("Резиновая крошка", "rubber"))
        self.assertTrue(is_playground_surface("Детская площадка", ""))
        self.assertFalse(is_playground_surface("Тротуар", "tile"))
        self.assertFalse(is_playground_surface("Площадка ТБО", "rubber"))


class BuildingFootprintTagTests(unittest.TestCase):
    def _two_footprints(self):
        vertices = [
            (0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 5.0, 0.0), (0.0, 5.0, 0.0),
            (4.0, 0.0, 0.0), (14.0, 0.0, 0.0), (14.0, 20.0, 0.0), (4.0, 20.0, 0.0),
        ]
        faces = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
        return vertices, faces, [2, 2], [20.0, 200.0]

    def test_one_layer_object_keeps_each_footprint_area(self) -> None:
        vertices, faces, counts, areas = self._two_footprints()
        parts = tagged_mesh_parts(vertices, faces, "layer", 10.0, counts, areas)
        self.assertEqual(len(parts), 1)
        _part_vertices, part_faces, part_counts, part_areas = parts[0]
        self.assertEqual(len(part_faces), 4)
        self.assertEqual(part_counts, [2, 2])
        self.assertEqual(part_areas, [20.0, 200.0])
        self.assertEqual([area for area in part_areas if area >= 100.0], [200.0])

    def test_gap_and_polygon_modes_keep_the_same_areas(self) -> None:
        vertices, faces, counts, areas = self._two_footprints()
        gap = tagged_mesh_parts(vertices, faces, "gap", 10.0, counts, areas)
        polygons = tagged_mesh_parts(vertices, faces, "polygon", 10.0, counts, areas)
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0][2:], ([2, 2], [20.0, 200.0]))
        self.assertEqual(len(polygons), 2)
        self.assertEqual(sorted(part[3][0] for part in polygons), [20.0, 200.0])
        self.assertTrue(all(part[2] == [2] for part in polygons))

    def test_missing_areas_leave_the_mesh_untagged(self) -> None:
        vertices, faces, counts, _areas = self._two_footprints()
        parts = tagged_mesh_parts(vertices, faces, "layer", 10.0, counts, [])
        self.assertEqual(parts[0][2:], ([], []))


class BuildingAreaUnitTests(unittest.TestCase):
    def test_millimeter_rectangle_is_216_square_meters(self) -> None:
        areas = areas_to_square_meters([18000 * 12000], [18000])
        self.assertAlmostEqual(areas[0], 216.0)
        self.assertLess(areas[0], 10000)

    def test_meter_rectangle_stays_below_10000(self) -> None:
        areas = areas_to_square_meters([40 * 25], [40])
        self.assertAlmostEqual(areas[0], 1000.0)
        self.assertLess(areas[0], 10000)

    def test_large_meter_rectangle_stays_above_10000(self) -> None:
        areas = areas_to_square_meters([200 * 80], [200])
        self.assertAlmostEqual(areas[0], 16000.0)
        self.assertGreaterEqual(areas[0], 10000)

    def test_annex_uses_the_same_multiplier_as_the_tower(self) -> None:
        areas = areas_to_square_meters(
            [18000 * 12000, 1500 * 1000],
            [18000, 1500],
        )
        self.assertAlmostEqual(areas[0], 216.0)
        self.assertAlmostEqual(areas[1], 1.5)
        self.assertTrue(all(area < 10000 for area in areas))

    def test_longer_side_uses_only_the_spot_faces(self) -> None:
        vertices = [
            (0.0, 0.0, 0.0), (18000.0, 0.0, 0.0), (18000.0, 12000.0, 0.0), (0.0, 12000.0, 0.0),
            (20000.0, 0.0, 0.0), (40000.0, 0.0, 0.0), (40000.0, 10000.0, 0.0),
        ]
        self.assertEqual(footprint_longer_side(vertices, [(0, 1, 2), (0, 2, 3)]), 18000.0)


class AddonLoadTests(unittest.TestCase):
    def test_placement_math_imports_before_shapely_is_installed(self) -> None:
        import subprocess

        folder = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"
        script = (
            "import sys\n"
            "class _Block:\n"
            "    def find_spec(self, name, path, target=None):\n"
            "        if name == 'shapely' or name.startswith('shapely.'):\n"
            "            raise ImportError(name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "import placement_math\n"
            "print('ok')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=folder,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
