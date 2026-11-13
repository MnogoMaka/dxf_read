from __future__ import annotations

import sys
import unittest
from math import cos, hypot, pi, sin
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
    choose_plant_names,
    auto_playground_kinds,
    bench_island_keys,
    cluster_nearby_islands,
    glue_selected_islands,
    compose_playground,
    equipment_quads,
    forward_angle,
    impact_margin,
    is_playground_layer,
    playground_fill_kind,
    play_group,
    playground_clearance,
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


class PlantTargetTests(unittest.TestCase):
    def test_several_selected_names_are_kept(self) -> None:
        self.assertEqual(choose_plant_names(["газон", "газон_2", "газон"], ["старый"]), ["газон", "газон_2"])

    def test_empty_selection_keeps_the_remembered_set(self) -> None:
        remembered = ["газон", "газон_2"]
        self.assertEqual(choose_plant_names([], remembered), remembered)

    def test_empty_memory_plants_nothing(self) -> None:
        self.assertEqual(choose_plant_names([], []), [])
        self.assertEqual(choose_plant_names(["", " "], ["", " "]), [])


def _side_offset(angle: float) -> float:
    """Насколько угол далёк от стороны квадрата и от перпендикуляра к ней. 45° даёт pi/4."""
    turned = abs(float(angle)) % (pi / 2.0)
    return min(turned, pi / 2.0 - turned)


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
                "clearance": playground_clearance(item),
                "motion": item["motion"],
                "role": item.get("role") or "activity",
                "fall_height": item.get("fall_height"),
                "group": play_group(item),
                "playground_type": item.get("playground_type") or "children",
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
        margin = impact_margin(float(catalog["sandbox_01"]["fall_height"]))
        self.assertEqual(catalog["sandbox_01"]["clearance"], (margin, margin, margin, margin))
        swing = catalog["swing_01"]["clearance"]
        self.assertEqual(swing[:2], (2.0, 2.0))
        self.assertEqual(swing[2:], (1.5, 1.5))
        self.assertNotEqual(swing[0], impact_margin(float(catalog["swing_01"]["fall_height"])))

    def test_seed_is_reproducible_and_changes_the_layout(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        bench = {"width": 1.82, "depth": 0.68, "urn_width": 0.4, "urn_depth": 0.26}
        first = compose_playground(vertices, faces, children, seed=40, bench=bench, layout="single")
        repeated = compose_playground(vertices, faces, children, seed=40, bench=bench, layout="single")
        self.assertEqual(first, repeated)
        self.assertIn("play_08", [item["id"] for item in first["items"]])
        self.assertFalse(first["hero_missing"])
        self.assertGreater(len(first["items"]), 1)
        self.assertGreater(len(first["benches"]), 0)
        hero = next(item for item in first["items"] if item["id"] == "play_08")
        self.assertAlmostEqual(hero["x"], 20.0, delta=6.0)
        self.assertAlmostEqual(hero["y"], 15.0, delta=6.0)
        self.assertLess(_side_offset(hero["angle"]), 0.2)
        hero_distance = hypot(hero["x"] - 20.0, hero["y"] - 15.0)
        for item in first["items"]:
            if item is hero:
                continue
            self.assertLessEqual(hero_distance, hypot(item["x"] - 20.0, item["y"] - 15.0) + 1.0)
        assets = {item["id"]: item for item in children}
        signatures = set()
        for seed in range(8):
            layout = compose_playground(vertices, faces, children, seed=seed, bench=bench, layout="set")
            self.assertEqual(layout, compose_playground(vertices, faces, children, seed=seed, bench=bench, layout="set"))
            signatures.add(tuple(
                (item["id"], round(item["x"], 1), round(item["y"], 1), round(item["angle"], 2))
                for item in layout["items"]
            ))
            quads = []
            for item in layout["items"]:
                asset = assets[item["id"]]
                _gap, zone = equipment_quads(
                    asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
                )
                self.assertTrue(all(
                    0.0 - 1e-6 <= x <= 40.0 + 1e-6 and 0.0 - 1e-6 <= y <= 30.0 + 1e-6 for x, y in zone
                ))
                quads.append(zone)
                for other in quads[:-1]:
                    self.assertFalse(quads_overlap(zone, other))
                self.assertLess(_side_offset(item["angle"]), 0.2)
            self.assertGreater(len(layout["items"]), 1)
            self.assertGreater(len(layout["benches"]), 2)
        self.assertGreater(len(signatures), 1)
        square, square_faces = _square(20.0, 20.0)
        one = compose_playground(square, square_faces, children, seed=3, layout="single")
        self.assertGreaterEqual(len(one["items"]), 1)
        for item in one["items"]:
            self.assertLess(_side_offset(item["angle"]), 0.2)

    def test_both_layouts_mix_sizes_and_turn_a_swing(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        by_id = {item["id"]: item for item in children}
        bench = {"width": 1.82, "depth": 0.68, "urn_width": 0.4, "urn_depth": 0.26}

        def zone_area(asset: dict) -> float:
            front, back, left, right = asset["clearance"]
            return (float(asset["width"]) + left + right) * (float(asset["depth"]) + front + back)

        for mode in ("set", "single"):
            layout = compose_playground(vertices, faces, children, seed=4, bench=bench, layout=mode)
            self.assertGreater(len(layout["items"]), 1)
            self.assertGreater(len(layout["benches"]), 0)
            areas = [zone_area(by_id[item["id"]]) for item in layout["items"]]
            self.assertGreater(max(areas), min(areas) * 1.5)
            quads = []
            for item in layout["items"]:
                asset = by_id[item["id"]]
                _gap, zone = equipment_quads(
                    asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
                )
                self.assertLess(_side_offset(item["angle"]), 0.2)
                quads.append(zone)
            for point in layout["benches"]:
                self.assertLess(min(point["x"], point["y"], 40.0 - point["x"], 30.0 - point["y"]), 1.5)
                gap, _safety = equipment_quads(1.82, 0.68, (0.0, 0.0, 0.0, 0.0), point["angle"], (point["x"], point["y"]))
                for zone in quads:
                    self.assertFalse(quads_overlap(gap, zone))
            if mode == "single":
                hero = max(layout["items"], key=lambda item: zone_area(by_id[item["id"]]))
                hero_distance = hypot(hero["x"] - 20.0, hero["y"] - 15.0)
                for item in layout["items"]:
                    if item is hero:
                        continue
                    self.assertLessEqual(hero_distance, hypot(item["x"] - 20.0, item["y"] - 15.0) + 1.0)
        along = across = None
        for seed in range(12):
            layout = compose_playground(vertices, faces, children, seed=seed, layout="set")
            for item in layout["items"]:
                if item["id"] != "swing_02":
                    continue
                if item["orientation"] == "A":
                    along = item["angle"]
                else:
                    across = item["angle"]
        self.assertIsNotNone(along)
        self.assertIsNotNone(across)
        folded = abs(along - across) % pi
        self.assertAlmostEqual(min(folded, pi - folded), pi / 2.0, delta=0.15)

    def test_lone_hero_stays_at_the_widest_point(self) -> None:
        vertices, faces = _square(40.0, 40.0)
        sports = [item for item in self._assets() if item["id"] == "play_big"]
        first = compose_playground(vertices, faces, sports, seed=6, layout="single")
        repeated = compose_playground(vertices, faces, sports, seed=6, layout="single")
        self.assertEqual(first["items"], repeated["items"])
        self.assertEqual(len(first["items"]), 1)
        poses = set()
        for seed in range(6):
            layout = compose_playground(vertices, faces, sports, seed=seed, layout="single")
            self.assertEqual(len(layout["items"]), 1)
            self.assertEqual(layout["benches"], [])
            hero = layout["items"][0]
            self.assertEqual(hero["id"], "play_big")
            self.assertAlmostEqual(hero["x"], 20.0, delta=4.0)
            self.assertAlmostEqual(hero["y"], 20.0, delta=4.0)
            poses.add(round(hero["angle"], 2))
            self.assertLess(_side_offset(hero["angle"]), 0.05)
        self.assertGreaterEqual(len(poses), 2)

    def test_sports_layout_uses_the_new_models(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        assets = {item["id"]: item for item in self._assets() if item["playground_type"] == "sports"}
        alone = compose_playground(vertices, faces, list(assets.values()), seed=0, layout="single")
        self.assertIn("play_big", [item["id"] for item in alone["items"]])
        self.assertGreater(len(alone["items"]), 1)
        self.assertFalse(alone["hero_missing"])
        self.assertEqual(alone["benches"], [])
        layout = compose_playground(vertices, faces, list(assets.values()), seed=1, layout="set")
        ids = {item["id"] for item in layout["items"]}
        self.assertGreater(len(ids - {"play_big"}), 1)
        self.assertGreater(len(layout["items"]), 1)
        self.assertLessEqual(len(layout["items"]), 16)
        quads = []
        for item in layout["items"]:
            asset = assets[item["id"]]
            _gap, zone = equipment_quads(
                asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
            )
            self.assertTrue(all(0.0 - 1e-6 <= x <= 40.0 + 1e-6 and 0.0 - 1e-6 <= y <= 30.0 + 1e-6 for x, y in zone))
            quads.append(zone)
            for other in quads[:-1]:
                self.assertFalse(quads_overlap(zone, other))

    def test_composition_keeps_zones_inside_and_uncluttered(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        assets = {item["id"]: item for item in self._assets() if item["playground_type"] == "children"}
        placed = compose_playground(vertices, faces, list(assets.values()), seed=3)
        self.assertGreater(len(placed["items"]), 1)
        self.assertLessEqual(len(placed["items"]), 16)
        quads = []
        for item in placed["items"]:
            asset = assets[item["id"]]
            _gap, zone = equipment_quads(
                asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
            )
            self.assertTrue(all(0.0 - 1e-6 <= x <= 40.0 + 1e-6 and 0.0 - 1e-6 <= y <= 30.0 + 1e-6 for x, y in zone))
            if item["pad"] is not None:
                self.assertEqual(item["pad_kind"], "rubber" if asset["motion"] in {"swing", "slide"} or asset["role"] == "hero" else "sand")
            quads.append(zone)
            for other in quads[:-1]:
                self.assertFalse(quads_overlap(zone, other))

    def test_benches_sit_inside_along_the_edge(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        bench = {"width": 1.82, "depth": 0.68, "urn_width": 0.4, "urn_depth": 0.26}
        layout = compose_playground(vertices, faces, children, seed=1, bench=bench)
        self.assertTrue(layout["benches"])
        self.assertGreater(len(layout["benches"]), 2)
        self.assertEqual(len(layout["urns"]), len(layout["benches"]))
        assets = {item["id"]: item for item in children}
        zones = []
        for item in layout["items"]:
            asset = assets[item["id"]]
            _gap, zone = equipment_quads(
                asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
            )
            zones.append(zone)
        center = (20.0, 15.0)
        for point in layout["benches"]:
            self.assertGreater(point["x"], 0.0)
            self.assertLess(point["x"], 40.0)
            self.assertGreater(point["y"], 0.0)
            self.assertLess(point["y"], 30.0)
            self.assertLess(min(point["x"], point["y"], 40.0 - point["x"], 30.0 - point["y"]), 1.5)
            facing = (-sin(point["angle"]), cos(point["angle"]))
            self.assertGreater(
                facing[0] * (center[0] - point["x"]) + facing[1] * (center[1] - point["y"]),
                0.0,
            )
            gap, _safety = equipment_quads(1.82, 0.68, (0.0, 0.0, 0.0, 0.0), point["angle"], (point["x"], point["y"]))
            for zone in zones:
                self.assertFalse(quads_overlap(gap, zone))
        for urn in layout["urns"]:
            self.assertGreater(urn["x"], 0.0)
            self.assertLess(urn["x"], 40.0)
            self.assertGreater(urn["y"], 0.0)
            self.assertLess(urn["y"], 30.0)
            nearest = min(hypot(urn["x"] - point["x"], urn["y"] - point["y"]) for point in layout["benches"])
            self.assertGreater(nearest, 0.9)
            self.assertLess(nearest, 1.4)
            gap, _safety = equipment_quads(0.4, 0.26, (0.0, 0.0, 0.0, 0.0), urn["angle"], (urn["x"], urn["y"]))
            for zone in zones:
                self.assertFalse(quads_overlap(gap, zone))
        for point in layout["items"]:
            turned = abs(float(point["angle"])) % (pi / 2.0)
            self.assertLess(min(turned, pi / 2.0 - turned), 0.05)
        shifted = compose_playground(vertices, faces, children, seed=2, bench=bench)
        here = [(round(point["x"], 1), round(point["y"], 1)) for point in layout["benches"]]
        there = [(round(point["x"], 1), round(point["y"], 1)) for point in shifted["benches"]]
        self.assertNotEqual(here, there)

    def test_hole_keeps_two_pairs_on_the_long_edges(self) -> None:
        vertices = [
            (0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (20.0, 4.0, 0.0), (0.0, 4.0, 0.0),
            (0.0, 8.0, 0.0), (20.0, 8.0, 0.0), (20.0, 12.0, 0.0), (0.0, 12.0, 0.0),
            (8.0, 4.0, 0.0), (12.0, 4.0, 0.0), (12.0, 8.0, 0.0), (8.0, 8.0, 0.0),
        ]
        faces = [
            (0, 1, 2, 3), (3, 8, 11, 4), (9, 2, 5, 10), (4, 5, 6, 7),
        ]
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        bench = {"width": 1.82, "depth": 0.68, "urn_width": 0.4, "urn_depth": 0.26}
        first = compose_playground(vertices, faces, children, seed=3, bench=bench)
        repeated = compose_playground(vertices, faces, children, seed=3, bench=bench)
        self.assertEqual(first, repeated)
        self.assertGreaterEqual(len(first["benches"]), 1)
        self.assertEqual(len(first["urns"]), len(first["benches"]))
        for point in first["benches"]:
            self.assertLess(min(point["x"], point["y"], 20.0 - point["x"], 12.0 - point["y"]), 1.5)
        for urn in first["urns"]:
            nearest = min(hypot(urn["x"] - point["x"], urn["y"] - point["y"]) for point in first["benches"])
            self.assertGreater(nearest, 0.9)
            self.assertLess(nearest, 1.4)

    def test_nearby_squares_share_one_layout(self) -> None:
        def island(x: float, y: float, size: float, key: str):
            vertices = [
                (x, y, 0.0), (x + size, y, 0.0), (x + size, y + size, 0.0), (x, y + size, 0.0),
            ]
            return vertices, [(0, 1, 2, 3)], key

        near = [island(0.0, 0.0, 16.0, "A:0"), island(18.0, 0.0, 16.0, "B:0")]
        far = near + [island(60.0, 0.0, 16.0, "C:0")]
        self.assertEqual(len(cluster_nearby_islands(far)), 2)
        self.assertEqual(len(cluster_nearby_islands([near[0], island(40.0, 0.0, 16.0, "D:0")])), 2)
        merged = cluster_nearby_islands(near)
        self.assertEqual(len(merged), 1)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        bench = {"width": 1.82, "depth": 0.68, "urn_width": 0.4, "urn_depth": 0.26}
        layout = compose_playground(merged[0][0], merged[0][1], children, seed=1, bench=bench)
        self.assertTrue(any(item["x"] < 16.0 for item in layout["items"]))
        self.assertTrue(any(item["x"] > 18.0 for item in layout["items"]))
        for point in layout["benches"]:
            self.assertFalse(15.2 < point["x"] < 18.8)

    def test_selected_squares_glue_and_distant_ones_stay_apart(self) -> None:
        def island(x: float, y: float, size: float, key: str):
            vertices = [
                (x, y, 0.0), (x + size, y, 0.0), (x + size, y + size, 0.0), (x, y + size, 0.0),
            ]
            return vertices, [(0, 1, 2, 3)], key

        touching = [island(0.0, 0.0, 16.0, "A:0"), island(16.0, 0.0, 16.0, "B:0")]
        glued = glue_selected_islands(touching)
        self.assertEqual(len(glued), 1)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        bench = {"width": 1.82, "depth": 0.68, "urn_width": 0.4, "urn_depth": 0.26}
        layout = compose_playground(glued[0][0], glued[0][1], children, seed=1, bench=bench)
        self.assertTrue(any(item["x"] < 16.0 for item in layout["items"]))
        self.assertTrue(any(item["x"] > 16.0 for item in layout["items"]))
        for point in layout["benches"]:
            self.assertFalse(abs(point["x"] - 16.0) < 1.2 and 1.5 < point["y"] < 14.5)
        distant = [island(0.0, 0.0, 16.0, "A:0"), island(80.0, 0.0, 16.0, "B:0")]
        self.assertEqual(len(distant), 2)
        self.assertEqual(len(cluster_nearby_islands(distant)), 2)

    def test_two_pieces_give_benches_to_the_largest(self) -> None:
        large = ([(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)], [(0, 1, 2)], "Yard:0")
        small = ([(20.0, 0.0, 0.0), (24.0, 0.0, 0.0), (20.0, 2.0, 0.0)], [(0, 1, 2)], "Yard:1")
        other = ([(0.0, 30.0, 0.0), (6.0, 30.0, 0.0), (0.0, 36.0, 0.0)], [(0, 1, 2)], "Court:0")
        self.assertEqual(bench_island_keys([large, small, other]), {"Yard:0", "Court:0"})

    def test_compact_model_fills_a_polygon_the_hero_misses(self) -> None:
        vertices, faces = _square(8.0, 8.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        layout = compose_playground(vertices, faces, children, seed=0, layout="single")
        ids = {item["id"] for item in layout["items"]}
        self.assertNotIn("play_08", ids)
        self.assertTrue(layout["hero_missing"])
        self.assertGreaterEqual(len(layout["items"]), 1)
        placed = next(asset for asset in children if asset["id"] == layout["items"][0]["id"])
        front, back, left, right = placed["clearance"]
        placed_area = (placed["width"] + left + right) * (placed["depth"] + front + back)
        for asset in children:
            zone_front, zone_back, zone_left, zone_right = asset["clearance"]
            area = (asset["width"] + zone_left + zone_right) * (asset["depth"] + zone_front + zone_back)
            if area <= placed_area + 1e-6:
                continue
            alone = compose_playground(vertices, faces, [asset], seed=0, layout="single")
            self.assertEqual(alone["items"], [])
        repeated = compose_playground(vertices, faces, children, seed=0, layout="single")
        self.assertEqual(layout, repeated)
        self.assertGreaterEqual(len(layout["items"]), 1)
        self.assertEqual(layout["items"][0]["id"], placed["id"])
        tight_vertices, tight_faces = _square(6.0, 6.0)
        for mode in ("set", "single"):
            tight = compose_playground(tight_vertices, tight_faces, children, seed=0, layout=mode)
            self.assertEqual(len(tight["items"]), 1)

    def test_large_rectangle_fills_past_the_old_budget(self) -> None:
        vertices, faces = _square(40.0, 30.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        layout = compose_playground(vertices, faces, children, seed=5)
        self.assertGreater(len(layout["items"]), int(40.0 * 30.0 / 300.0))
        self.assertLessEqual(len(layout["items"]), 16)
        ids = [item["id"] for item in layout["items"]]
        children_by_id = {item["id"]: item for item in children}
        seen: list[str] = []
        for item_id in ids:
            if item_id in seen:
                break
            seen.append(item_id)
        self.assertTrue(all(item_id in seen for item_id in ids))
        by_x: dict[float, int] = {}
        by_y: dict[float, int] = {}
        areas = []
        for item in layout["items"]:
            asset = children_by_id[item["id"]]
            front, back, left, right = asset["clearance"]
            areas.append((asset["width"] + left + right) * (asset["depth"] + front + back))
            by_x[round(item["x"], 1)] = by_x.get(round(item["x"], 1), 0) + 1
            by_y[round(item["y"], 1)] = by_y.get(round(item["y"], 1), 0) + 1
        self.assertEqual(areas[0], max(areas))
        self.assertGreater(max(max(by_x.values()), max(by_y.values())), 1)
        plain = compose_playground(vertices, faces, children, seed=0, layout="single")
        filled = compose_playground(vertices, faces, children, seed=1, layout="set")
        self.assertGreater(len(plain["items"]), 1)
        self.assertEqual(plain["benches"], [])
        self.assertGreater(len(filled["items"]), 1)
        plain_items = tuple((item["id"], round(item["x"], 1), round(item["y"], 1)) for item in plain["items"])
        filled_items = tuple((item["id"], round(item["x"], 1), round(item["y"], 1)) for item in filled["items"])
        self.assertNotEqual(plain_items, filled_items)
        self.assertEqual(plain, compose_playground(vertices, faces, children, seed=0, layout="single"))
        self.assertEqual(layout, compose_playground(vertices, faces, children, seed=5, layout="set"))
        assets = {item["id"]: item for item in children}
        quads = []
        for item in layout["items"]:
            asset = assets[item["id"]]
            _gap, zone = equipment_quads(
                asset["width"], asset["depth"], asset["clearance"], item["angle"], (item["x"], item["y"]),
            )
            self.assertTrue(all(
                -1e-6 <= point[0] <= 40.0 + 1e-6 and -1e-6 <= point[1] <= 30.0 + 1e-6 for point in zone
            ))
            for other in quads:
                self.assertFalse(quads_overlap(zone, other))
            quads.append(zone)

    def test_smaller_clearance_lets_a_larger_model_in(self) -> None:
        vertices, faces = _square(14.0, 14.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        full = compose_playground(vertices, faces, children, seed=0, clearance_scale=1.0, layout="single")
        self.assertEqual(full, compose_playground(vertices, faces, children, seed=0, layout="single"))
        self.assertEqual(full, compose_playground(vertices, faces, children, seed=0, clearance_scale=2.0, layout="single"))
        self.assertTrue(full["hero_missing"])
        self.assertNotIn("play_08", {item["id"] for item in full["items"]})
        tight = compose_playground(vertices, faces, children, seed=0, clearance_scale=0.5, layout="single")
        self.assertIn("play_08", {item["id"] for item in tight["items"]})
        self.assertFalse(tight["hero_missing"])
        self.assertEqual(tight, compose_playground(vertices, faces, children, seed=0, clearance_scale=0.5, layout="single"))

    def test_slide_is_placed_when_the_hero_does_not_fit(self) -> None:
        vertices, faces = _square(14.0, 14.0)
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        slides = {item["id"] for item in children if item["group"] == "slide"}
        for seed in range(6):
            layout = compose_playground(vertices, faces, children, seed=seed, layout="single")
            self.assertTrue(layout["hero_missing"])
            self.assertGreaterEqual(len(layout["items"]), 1)
            self.assertNotIn("play_08", {item["id"] for item in layout["items"]})
            self.assertNotIn(layout["items"][0]["id"], slides)
            for item in layout["items"]:
                self.assertLess(_side_offset(item["angle"]), 0.2)

    def test_narrow_neck_stays_empty(self) -> None:
        ring = [
            (0.0, 0.0), (16.0, 0.0), (16.0, 7.5), (22.0, 7.5), (22.0, 0.0), (40.0, 0.0),
            (40.0, 18.0), (22.0, 18.0), (22.0, 10.5), (16.0, 10.5), (16.0, 18.0), (0.0, 18.0),
        ]
        vertices = [(x, y, 0.0) for x, y in ring]
        faces = [tuple(range(len(ring)))]
        children = [item for item in self._assets() if item["playground_type"] == "children"]
        layout = compose_playground(vertices, faces, children, seed=4)
        self.assertFalse(layout["hero_missing"])
        for item in layout["items"]:
            self.assertFalse(16.0 < item["x"] < 22.0 and 7.5 <= item["y"] <= 10.5)
        for point in layout["benches"]:
            self.assertFalse(16.0 < point["x"] < 22.0 and 7.5 <= point["y"] <= 10.5)

    def test_small_polygon_reports_the_missing_hero(self) -> None:
        vertices, faces = _square(4.0, 4.0)
        large = [item for item in self._assets() if item["id"] == "play_big"]
        layout = compose_playground(vertices, faces, large)
        self.assertEqual(layout["items"], [])
        self.assertTrue(layout["hero_missing"])

    def test_composition_does_not_tile_one_box(self) -> None:
        vertices, faces = _square(50.0, 40.0)
        asset = {
            "id": "box",
            "width": 1.2,
            "depth": 1.2,
            "clearance": (0.4, 0.4, 0.4, 0.4),
            "motion": "static",
            "role": "activity",
            "fall_height": 0.4,
        }
        layout = compose_playground(vertices, faces, [asset], seed=2, layout="single")
        self.assertEqual(len(layout["items"]), 1)
        filled = compose_playground(vertices, faces, [asset], seed=3, layout="set")
        self.assertGreater(len(filled["items"]), 1)
        item = layout["items"][0]
        self.assertTrue(safety_inside(
            vertices, faces, 1.2, 1.2, (0.4, 0.4, 0.4, 0.4), item["angle"], (item["x"], item["y"]),
        ))
        self.assertIsNone(item["pad"])

    def test_small_polygon_stays_empty(self) -> None:
        vertices, faces = _square(4.0, 4.0)
        large = [item for item in self._assets() if item["id"] == "play_big"]
        self.assertEqual(pack_playground(vertices, faces, large), [])

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

    def test_models_stay_out_of_the_hole(self) -> None:
        vertices = [
            (0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (20.0, 20.0, 0.0), (0.0, 20.0, 0.0),
            (8.0, 8.0, 0.0), (12.0, 8.0, 0.0), (12.0, 12.0, 0.0), (8.0, 12.0, 0.0),
        ]
        faces = [(0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
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
        self.assertEqual(playground_fill_kind("Спортивная площадка"), "sports")
        self.assertEqual(playground_fill_kind("Детская площадка"), "children")
        names = ["Детская", "Спортивная площадка", "Резина", "Игровая 2", "Спорт 2", "Площадка 3"]
        self.assertEqual(auto_playground_kinds(names, "children", 3), ["children"] * len(names))
        self.assertEqual(auto_playground_kinds(names, "sports", 3), ["sports"] * len(names))
        mixed = auto_playground_kinds(names, "random", 1)
        self.assertEqual(mixed, auto_playground_kinds(names, "random", 1))
        self.assertEqual(set(mixed), {"children", "sports"})
        self.assertIn("children", auto_playground_kinds(["Спортивная площадка"] * len(names), "random", 1))

    def test_rubber_counts_as_a_playground_and_waste_does_not(self) -> None:
        from placement_math import is_playground_surface

        self.assertTrue(is_playground_surface("Резиновая крошка", ""))
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
