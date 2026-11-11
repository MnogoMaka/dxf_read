from __future__ import annotations

import json
import unittest
from math import cos, hypot, pi, sin
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"))

from placement_math import (
    DEFAULT_PLANT_DENSITY,
    DEFAULT_TREE_SHARE,
    FOOTPRINT_SLIVER_M2,
    adopt_plant_defaults,
    OccupiedFootprints,
    PlantObstacles,
    host_rings,
    footprint_overlap_fraction,
    footprints_overlap_too_much,
    glb_node_scale,
    glb_plan_side,
    glb_planted_points,
    mesh_plan_side,
    oriented_square,
    plant_footprint,
    plant_slot_count,
    scatter_points,
    slot_attempt_families,
)


LIBRARY = Path(__file__).resolve().parents[1] / "maf_blender_models"


class FootprintRuleTests(unittest.TestCase):
    def test_epsilon_is_a_square_millimetre_sliver(self) -> None:
        # Общая кромка и шум ниже 1e-6 м² не считаются наложением карты.
        self.assertEqual(FOOTPRINT_SLIVER_M2, 1e-6)

    def test_bush_on_pavement_is_rejected_and_tree_is_not(self) -> None:
        pavement = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]
        obstacles = PlantObstacles([], pavement)
        quad = oriented_square((5.0, 5.0), 2.0, 0.2)
        self.assertTrue(obstacles.blocks(quad, "bush"))
        self.assertFalse(obstacles.blocks(quad, "tree"))

    def test_building_rejects_every_family(self) -> None:
        building = [[(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]]
        obstacles = PlantObstacles(building, [])
        quad = oriented_square((3.2, 2.0), 2.0, 0.4)
        self.assertTrue(obstacles.blocks(quad, "tree"))
        self.assertTrue(obstacles.blocks(quad, "bush"))

    def test_tree_on_pavement_still_stops_at_a_building(self) -> None:
        building = [[(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]]
        pavement = [[(0.0, 0.0), (8.0, 0.0), (8.0, 8.0), (0.0, 8.0)]]
        obstacles = PlantObstacles(building, pavement)
        quad = oriented_square((1.0, 1.0), 2.0, 0.0)
        self.assertTrue(obstacles.blocks(quad, "tree"))
        self.assertTrue(obstacles.blocks(quad, "bush"))

    def test_shared_edge_is_not_an_overlap_but_a_real_clip_is(self) -> None:
        quad = oriented_square((0.0, 0.0), 2.0, 0.0)
        touching = PlantObstacles([[(1.0, -1.0), (3.0, -1.0), (3.0, 1.0), (1.0, 1.0)]], [])
        clipping = PlantObstacles([[(0.8, -1.0), (3.0, -1.0), (3.0, 1.0), (0.8, 1.0)]], [])
        sliver = PlantObstacles(
            [[(1.0 - 1e-8, -1.0), (3.0, -1.0), (3.0, 1.0), (1.0 - 1e-8, 1.0)]],
            [],
        )
        self.assertFalse(touching.blocks(quad, "tree"))
        self.assertFalse(sliver.blocks(quad, "bush"))
        self.assertTrue(clipping.blocks(quad, "tree"))

    def test_equal_squares_accept_thirty_percent_and_reject_more(self) -> None:
        # Сторона 10, площадь 100. Сдвиг на 7 оставляет полосу 3×10 = 30%.
        left = oriented_square((0.0, 0.0), 10.0, 0.0)
        at_limit = oriented_square((7.0, 0.0), 10.0, 0.0)
        over = oriented_square((6.9, 0.0), 10.0, 0.0)
        self.assertAlmostEqual(footprint_overlap_fraction(left, at_limit), 0.30, places=6)
        self.assertFalse(footprints_overlap_too_much(left, at_limit))
        self.assertGreater(footprint_overlap_fraction(left, over), 0.30)
        self.assertTrue(footprints_overlap_too_much(left, over))

    def test_overlap_fraction_uses_the_smaller_footprint(self) -> None:
        big = oriented_square((0.0, 0.0), 10.0, 0.0)
        at_limit = oriented_square((5.4, 0.0), 2.0, 0.0)
        over = oriented_square((5.2, 0.0), 2.0, 0.0)
        self.assertAlmostEqual(footprint_overlap_fraction(big, at_limit), 0.30, places=6)
        self.assertFalse(footprints_overlap_too_much(big, at_limit))
        self.assertGreater(footprint_overlap_fraction(big, over), 0.30)
        occupied = OccupiedFootprints()
        occupied.add(big)
        self.assertFalse(occupied.blocks(at_limit))
        self.assertTrue(occupied.blocks(over))

    def test_yaw_moves_the_corners_onto_a_building(self) -> None:
        building = [[(1.05, -0.2), (2.0, -0.2), (2.0, 0.2), (1.05, 0.2)]]
        obstacles = PlantObstacles(building, [])
        still = oriented_square((0.0, 0.0), 2.0, 0.0)
        turned = oriented_square((0.0, 0.0), 2.0, pi / 4)
        self.assertNotEqual(
            sorted((round(x, 5), round(y, 5)) for x, y in still),
            sorted((round(x, 5), round(y, 5)) for x, y in turned),
        )
        self.assertFalse(obstacles.blocks(still, "tree"))
        self.assertTrue(obstacles.blocks(turned, "bush"))
        self.assertTrue(obstacles.blocks(turned, "tree"))

    def test_tree_crosses_pavement_until_the_flag_is_on(self) -> None:
        pavement = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]
        obstacles = PlantObstacles([], pavement)
        quad = oriented_square((5.0, 5.0), 2.0, 0.2)
        self.assertFalse(obstacles.blocks(quad, "tree"))
        self.assertTrue(obstacles.blocks(quad, "tree", avoid_ground=True))
        self.assertTrue(obstacles.blocks(quad, "bush"))

    def test_polygon_under_a_house_blocks_only_trees(self) -> None:
        lawn = [[(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]]
        split = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            [(10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (10.0, 10.0)],
        ]
        far = [[(40.0, 0.0), (50.0, 0.0), (50.0, 10.0), (40.0, 10.0)]]
        house = [[(8.0, 8.0), (12.0, 8.0), (12.0, 12.0), (8.0, 12.0)]]
        self.assertEqual(host_rings([split, far], [(5.0, 5.0)]), split)
        obstacles = PlantObstacles(house, [], lawn)
        on_lawn = oriented_square((2.0, 2.0), 2.0, 0.0)
        on_house = oriented_square((10.0, 10.0), 2.0, 0.0)
        self.assertTrue(obstacles.blocks(on_lawn, "tree"))
        self.assertFalse(obstacles.blocks(on_lawn, "bush"))
        self.assertTrue(obstacles.blocks(on_house, "tree"))
        self.assertTrue(obstacles.blocks(on_house, "bush"))


class PlanSizeTests(unittest.TestCase):
    def test_scale_multiplies_the_measured_side(self) -> None:
        measured = 17.331648
        quad = plant_footprint((5.0, -1.0), measured, 0.0, 2.0)
        xs = [point[0] for point in quad]
        ys = [point[1] for point in quad]
        self.assertAlmostEqual(max(xs) - min(xs), measured * 2.0, places=5)
        self.assertAlmostEqual(max(ys) - min(ys), measured * 2.0, places=5)
        self.assertAlmostEqual((min(xs) + max(xs)) * 0.5, 5.0, places=5)
        self.assertAlmostEqual((min(ys) + max(ys)) * 0.5, -1.0, places=5)

    def test_mesh_plan_side_is_the_longer_xy_extent(self) -> None:
        self.assertAlmostEqual(
            mesh_plan_side([(0.0, 1.0, 5.0), (4.0, -1.0, 9.0), (1.0, 2.0, 0.0)]),
            4.0,
        )
        self.assertEqual(mesh_plan_side([]), 0.0)

    def test_extra_scatter_points_keep_the_original_prefix(self) -> None:
        vertices = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)]
        faces = [(0, 1, 2)]
        short = scatter_points(vertices, faces, 5, seed=11)
        full = scatter_points(vertices, faces, 5 * 24, seed=11)
        self.assertEqual(full[:5], short)

    def test_imported_plan_sides_follow_the_glb_not_the_stale_tree_catalog(self) -> None:
        catalog = {item["id"]: item for item in json.loads((LIBRARY / "catalog.json").read_text(encoding="utf-8"))}
        tree = LIBRARY / "Дерево .glb"
        spruce = LIBRARY / "Елка.glb"
        bush = LIBRARY / "Куст.glb"
        tree_scale = glb_node_scale(tree)
        tree_side = glb_plan_side(tree)
        spruce_side = glb_plan_side(spruce)
        bush_side = glb_plan_side(bush)
        # Масштаб узла читается из файла, а не подставляется константой 3.055.
        self.assertAlmostEqual(tree_scale, 3.0552125, places=5)
        self.assertEqual(glb_node_scale(spruce), 1.0)
        self.assertEqual(glb_node_scale(bush), 1.0)
        # До запекания рысканья узла Blender показывает габарит × масштаб ≈ 21.93 м.
        # transform_apply вписывает это рысканье в вершины. Локальная рамка XY,
        # которую затем крутит рысканье посадки, у «Дерева» — 17.332 м, не 7.18 и не 21.93.
        self.assertAlmostEqual(tree_side, 17.331648, places=4)
        self.assertAlmostEqual(catalog["tree_01"]["footprint"][0] * tree_scale, 21.936, delta=0.02)
        self.assertGreater(abs(tree_side - catalog["tree_01"]["footprint"][0]), 5.0)
        self.assertAlmostEqual(spruce_side, 10.608327, places=4)
        self.assertAlmostEqual(bush_side, 4.641953, places=4)
        self.assertAlmostEqual(spruce_side, catalog["tree_spruce"]["footprint"][0], delta=0.01)
        self.assertAlmostEqual(bush_side, catalog["bush_01"]["footprint"][0], delta=0.01)

    def test_tree_chord_is_longer_than_the_square_and_still_inside_it(self) -> None:
        # Хорда карт ~23.03 м. После запекания она лежит под ~41° и концами
        # упирается в стороны квадрата 17.332 м, а не выходит за них.
        points = glb_planted_points(LIBRARY / "Дерево .glb")
        side = glb_plan_side(LIBRARY / "Дерево .glb")
        chord = 0.0
        for index, left in enumerate(points):
            for right in points[index + 1:]:
                chord = max(chord, hypot(left[0] - right[0], left[1] - right[1]))
        half = side * 0.5
        self.assertAlmostEqual(chord, 23.031954, places=3)
        self.assertGreater(chord, side)
        self.assertLessEqual(max(abs(point[0]) for point in points), half + 1e-6)
        self.assertLessEqual(max(abs(point[1]) for point in points), half + 1e-6)

    def test_yawed_vertices_lie_inside_the_rejection_square(self) -> None:
        # Чистая математика по вершинам GLB, без Blender. Квадрат — тот, что
        # plant_footprint отдаёт в проверку отказа.
        yaws = (0.0, 0.4, pi / 4, 1.7, pi)
        poses = ((1.0, (0.0, 0.0)), (2.5, (30.0, -12.0)))
        for name in ("Дерево .glb", "Елка.glb", "Куст.glb"):
            path = LIBRARY / name
            points = glb_planted_points(path)
            side = glb_plan_side(path)
            self.assertGreater(len(points), 0)
            for yaw in yaws:
                ca, sa = cos(yaw), sin(yaw)
                for scale, center in poses:
                    quad = plant_footprint(center, side, yaw, scale)
                    half = side * scale * 0.5
                    expected = [
                        (center[0] + x * ca - y * sa, center[1] + x * sa + y * ca)
                        for x, y in ((-half, -half), (half, -half), (half, half), (-half, half))
                    ]
                    for corner, want in zip(quad, expected, strict=True):
                        self.assertAlmostEqual(corner[0], want[0], places=5)
                        self.assertAlmostEqual(corner[1], want[1], places=5)
                    for point in points:
                        world_x = center[0] + scale * (point[0] * ca - point[1] * sa)
                        world_y = center[1] + scale * (point[0] * sa + point[1] * ca)
                        dx = world_x - center[0]
                        dy = world_y - center[1]
                        local_x = dx * ca + dy * sa
                        local_y = -dx * sa + dy * ca
                        self.assertLessEqual(abs(local_x), half + 1e-5)
                        self.assertLessEqual(abs(local_y), half + 1e-5)


class SlotMixTests(unittest.TestCase):
    def test_retry_keeps_the_family_chosen_for_the_slot(self) -> None:
        # Повтор слота двигает точку. Семейство, уже выбранное для слота, не меняется:
        # неудачное дерево не становится кустом.
        calls = []

        def choose():
            family = "tree" if len(calls) % 2 == 0 else "bush"
            calls.append(family)
            return family

        first = slot_attempt_families(choose, 24)
        second = slot_attempt_families(choose, 24)
        self.assertEqual(calls, ["tree", "bush"])
        self.assertEqual(first, ["tree"] * 24)
        self.assertEqual(second, ["bush"] * 24)

    def test_small_lawn_at_yard_density_gets_no_plant(self) -> None:
        # 10 м² при 3 шт/100 м² дают 0.3 слота. Раньше max(..., 1) всё равно ставил куст.
        per_square_meter = 3.0 / 100.0
        self.assertEqual(plant_slot_count(10.0, per_square_meter), 0)
        self.assertEqual(plant_slot_count(1.0, per_square_meter), 0)
        self.assertEqual(plant_slot_count(100.0, per_square_meter), 3)
        self.assertEqual(plant_slot_count(0.0, per_square_meter), 0)

    def test_generation_starts_at_one_per_hundred_and_thirty_percent_trees(self) -> None:
        self.assertEqual(DEFAULT_PLANT_DENSITY, 1.0)
        self.assertEqual(DEFAULT_TREE_SHARE, 30.0)
        self.assertEqual(adopt_plant_defaults(3.0, 10.0), (3.0, 10.0))
        self.assertEqual(adopt_plant_defaults(5.0, 80.0), (5.0, 80.0))
        self.assertEqual(adopt_plant_defaults(1.0, 30.0), (1.0, 30.0))


if __name__ == "__main__":
    unittest.main()
