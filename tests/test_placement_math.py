from __future__ import annotations

import sys
import unittest
from math import pi
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"))

from placement_math import bench_path, bench_positions, cluster_boxes, forward_angle, mirrored_keep_from


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


if __name__ == "__main__":
    unittest.main()
