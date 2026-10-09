from __future__ import annotations

import unittest

from dxf_read.arrangement import bounded_faces
from dxf_read.config import PipelineSettings, use_settings
from dxf_read.dxf import (
    HatchPiece,
    _curve_pieces_from_segments,
    _layer_by_boundary,
    arrange_hatch_faces_by_layer,
    pieces_from_cache,
    refine_layer_groups,
)


Segment = tuple[tuple[float, float], tuple[float, float], str]


def square(layer: str = "A") -> list[Segment]:
    return [
        ((0.0, 0.0), (1.0, 0.0), layer),
        ((1.0, 0.0), (1.0, 1.0), layer),
        ((1.0, 1.0), (0.0, 1.0), layer),
        ((0.0, 1.0), (0.0, 0.0), layer),
    ]


class BoundedFacesTests(unittest.TestCase):
    def test_closed_square_becomes_one_face(self) -> None:
        faces = bounded_faces(square(), 0.02)
        self.assertEqual(len(faces), 1)
        outer, holes, lengths = faces[0]
        self.assertEqual(len(outer), 4)
        self.assertEqual(holes, [])
        self.assertAlmostEqual(lengths["A"], 4.0)

    def test_crossing_line_splits_face(self) -> None:
        segments = square() + [((0.5, 0.0), (0.5, 1.0), "A")]
        faces = bounded_faces(segments, 0.02)
        self.assertEqual(len(faces), 2)

    def test_near_endpoints_join_by_distance_across_grid_boundary(self) -> None:
        segments: list[Segment] = [
            ((0.0, 0.0), (1.0, 0.0), "A"),
            ((1.0, 0.0), (1.0, 1.0), "A"),
            ((0.989, 1.0), (0.0, 1.0), "A"),
            ((0.0, 1.0), (0.0, 0.0), "A"),
        ]
        self.assertEqual(len(bounded_faces(segments, 0.02)), 1)

    def test_gap_larger_than_tolerance_stays_open(self) -> None:
        segments: list[Segment] = [
            ((0.0, 0.0), (1.0, 0.0), "A"),
            ((1.0, 0.0), (1.0, 1.0), "A"),
            ((0.97, 1.0), (0.0, 1.0), "A"),
            ((0.0, 1.0), (0.0, 0.0), "A"),
        ]
        self.assertEqual(bounded_faces(segments, 0.02), [])

    def test_coincident_boundaries_keep_all_layer_candidates(self) -> None:
        faces = bounded_faces(square("A") + square("B"), 0.02)
        self.assertEqual(len(faces), 1)
        lengths = faces[0][2]
        self.assertAlmostEqual(lengths["A"], 4.0)
        self.assertAlmostEqual(lengths["B"], 4.0)

    def test_coincident_boundary_tie_uses_category_priority(self) -> None:
        lengths = bounded_faces(square("A") + square("B"), 0.02)[0][2]
        chosen = _layer_by_boundary(lengths, {"A": "green", "B": "building"})
        self.assertEqual(chosen, "B")

    def test_hole_boundary_contributes_to_layer_candidates(self) -> None:
        inner: list[Segment] = [
            ((0.25, 0.25), (0.75, 0.25), "B"),
            ((0.75, 0.25), (0.75, 0.75), "B"),
            ((0.75, 0.75), (0.25, 0.75), "B"),
            ((0.25, 0.75), (0.25, 0.25), "B"),
        ]
        faces = bounded_faces(square("A") + inner, 0.02)
        face_with_hole = next(face for face in faces if face[1])
        self.assertAlmostEqual(face_with_hole[2]["A"], 4.0)
        self.assertAlmostEqual(face_with_hole[2]["B"], 2.0)


class ContourArrangementModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.category_by_layer = {"A": "green", "B": "pavement"}

    def category_of(self, layer: str) -> str | None:
        return self.category_by_layer.get(layer)

    def pieces(self, segments: list[Segment], mode: str):
        settings = PipelineSettings(
            contour_snap_tolerance_m=0.02,
            contour_arrangement_mode=mode,
        )
        with use_settings(settings):
            return _curve_pieces_from_segments(
                segments,
                self.category_of,
                self.category_by_layer,
            )[0]

    def test_layer_mode_does_not_close_shape_with_another_layer(self) -> None:
        segments: list[Segment] = [
            ((0.0, 0.0), (1.0, 0.0), "A"),
            ((1.0, 0.0), (1.0, 1.0), "A"),
            ((0.0, 1.0), (0.0, 0.0), "A"),
            ((1.0, 1.0), (0.0, 1.0), "B"),
        ]
        self.assertEqual(self.pieces(segments, "layer"), [])

    def test_combined_mode_can_close_shape_across_layers(self) -> None:
        segments: list[Segment] = [
            ((0.0, 0.0), (1.0, 0.0), "A"),
            ((1.0, 0.0), (1.0, 1.0), "A"),
            ((0.0, 1.0), (0.0, 0.0), "A"),
            ((1.0, 1.0), (0.0, 1.0), "B"),
        ]
        pieces = self.pieces(segments, "combined")
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].layer, "A")

    def test_layer_mode_builds_each_closed_layer_independently(self) -> None:
        shifted = [
            ((start[0] + 2.0, start[1]), (end[0] + 2.0, end[1]), "B")
            for start, end, _layer in square("B")
        ]
        pieces = self.pieces(square("A") + shifted, "layer")
        self.assertEqual({piece.layer for piece in pieces}, {"A", "B"})

    def test_cached_segments_use_the_same_layer_mode(self) -> None:
        segments = square("A")
        payload = {
            "layers": ["A"],
            "fills": [],
            "curves": [
                [layer, "", start[0], start[1], end[0], end[1]]
                for start, end, layer in segments
            ],
        }
        settings = PipelineSettings(
            contour_snap_tolerance_m=0.02,
            contour_arrangement_mode="layer",
        )
        with use_settings(settings):
            cached = pieces_from_cache(
                payload,
                {"green": ["A"]},
                "curves",
                {"A": "curves"},
            )
        direct = self.pieces(segments, "layer")
        self.assertEqual([(piece.layer, piece.category) for piece in cached], [
            (piece.layer, piece.category) for piece in direct
        ])


def _hatch_cache(outer, holes=None) -> dict:
    return {
        "layers": ["Газон"],
        "fills": [["h", "Газон", "", 1, "AB", outer, holes or []]],
        "curves": [],
    }


class HatchContourTests(unittest.TestCase):
    def _pieces(self, payload: dict, source: str, use_fills: bool):
        settings = PipelineSettings(contour_from_fills=use_fills, contour_snap_tolerance_m=0.02)
        with use_settings(settings):
            return pieces_from_cache(payload, {"green": ["Газон"]}, source, {"Газон": source})

    def test_hatch_boundary_is_ignored_until_the_option_is_on(self) -> None:
        payload = _hatch_cache([[0, 0], [10, 0], [10, 10], [0, 10]])
        self.assertEqual(self._pieces(payload, "curves", False), [])

    def test_hatch_boundary_closes_a_contour_face(self) -> None:
        payload = _hatch_cache(
            [[0, 0], [10, 0], [10, 10], [0, 10]],
            [[[4, 4], [6, 4], [6, 6], [4, 6]]],
        )
        pieces = self._pieces(payload, "curves", True)
        self.assertEqual([(piece.layer, piece.category) for piece in pieces], [("Газон", "green")])
        self.assertEqual(len(pieces[0].holes), 1)
        from shapely.geometry import Polygon

        area = Polygon(
            [(point[0], point[1]) for point in pieces[0].vertices],
            [[(point[0], point[1]) for point in hole] for hole in pieces[0].holes],
        ).area
        self.assertAlmostEqual(area, 96.0, places=3)

    def test_fill_mode_stays_a_single_hatch_when_the_option_is_on(self) -> None:
        payload = _hatch_cache([[0, 0], [10, 0], [10, 10], [0, 10]])
        pieces = self._pieces(payload, "fills", True)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].layer, "Газон")


def _ring(x: float, y: float, size: float) -> list[tuple[float, float, float]]:
    return [
        (x, y, 0.0),
        (x + size, y, 0.0),
        (x + size, y + size, 0.0),
        (x, y + size, 0.0),
    ]


def _areas(pieces: list[HatchPiece], cut: bool) -> dict[str, float]:
    settings = PipelineSettings(cut_vegetation=cut, site_cluster_radius_m=400000.0)
    with use_settings(settings):
        arranged = arrange_hatch_faces_by_layer(pieces)
    return {layer: float(geometry.area) for geometry, _category, layer in arranged}


class SymbolCutoutTests(unittest.TestCase):
    def test_maf_type_stays_on_top_of_whole_ground(self) -> None:
        pieces = [
            HatchPiece("green", "Газон", _ring(0, 0, 10), [_ring(4, 4, 2)]),
            HatchPiece("furniture", "Площадки", _ring(4, 4, 2)),
            HatchPiece("pavement", "Дорога", _ring(20, 0, 10)),
            HatchPiece("furniture", "Лавки", _ring(22, 2, 2)),
        ]
        areas = _areas(pieces, cut=False)
        self.assertAlmostEqual(areas["Газон"], 100.0, places=3)
        self.assertAlmostEqual(areas["Площадки"], 4.0, places=3)
        self.assertAlmostEqual(areas["Дорога"], 100.0, places=3)
        self.assertAlmostEqual(areas["Лавки"], 4.0, places=3)

    def test_playground_layer_stays_on_top_of_whole_ground(self) -> None:
        pieces = [
            HatchPiece("green", "Газон", _ring(0, 0, 10), [_ring(4, 4, 2)]),
            HatchPiece("pavement", "Детские площадки", _ring(4, 4, 2)),
            HatchPiece("pavement", "Тротуар", _ring(20, 0, 10)),
        ]
        areas = _areas(pieces, cut=False)
        self.assertAlmostEqual(areas["Газон"], 100.0, places=3)
        self.assertAlmostEqual(areas["Детские площадки"], 4.0, places=3)
        self.assertAlmostEqual(areas["Тротуар"], 100.0, places=3)

    def test_playground_cuts_ground_when_cutout_is_on(self) -> None:
        pieces = [
            HatchPiece("green", "Газон", _ring(0, 0, 10)),
            HatchPiece("pavement", "Площадки", _ring(4, 4, 2)),
        ]
        areas = _areas(pieces, cut=True)
        self.assertAlmostEqual(areas["Газон"], 96.0, places=3)

    def test_plain_pavement_still_cuts_ground(self) -> None:
        pieces = [
            HatchPiece("green", "Газон", _ring(0, 0, 10)),
            HatchPiece("pavement", "Тротуар", _ring(4, 4, 2)),
        ]
        areas = _areas(pieces, cut=False)
        self.assertAlmostEqual(areas["Газон"], 96.0, places=3)

    def test_plantings_type_still_cuts_ground(self) -> None:
        pieces = [
            HatchPiece("green", "Газон", _ring(0, 0, 10)),
            HatchPiece("vegetation", "Кусты", _ring(4, 4, 2)),
        ]
        areas = _areas(pieces, cut=False)
        self.assertAlmostEqual(areas["Газон"], 96.0, places=3)

    def test_ai_maf_type_is_kept_when_layer_name_looks_like_pavement(self) -> None:
        groups = refine_layer_groups(
            {"furniture": ["1 ГП_Площадки МАФ"]},
            ["1 ГП_Площадки МАФ"],
        )
        self.assertEqual(groups["furniture"], ["1 ГП_Площадки МАФ"])
        self.assertNotIn("pavement", groups)

    def test_enabled_cutout_removes_symbol_area_from_ground(self) -> None:
        pieces = [
            HatchPiece("pavement", "Дорога", _ring(0, 0, 10)),
            HatchPiece("furniture", "Лавки", _ring(2, 2, 2)),
        ]
        areas = _areas(pieces, cut=True)
        self.assertAlmostEqual(areas["Дорога"], 96.0, places=3)
        self.assertAlmostEqual(areas["Лавки"], 4.0, places=3)

    def test_building_hole_stays_when_symbol_cutout_is_off(self) -> None:
        pieces = [
            HatchPiece("green", "Газон", _ring(0, 0, 10), [_ring(4, 4, 2)]),
            HatchPiece("building", "Здание", _ring(4, 4, 2)),
        ]
        areas = _areas(pieces, cut=False)
        self.assertAlmostEqual(areas["Газон"], 96.0, places=3)
        self.assertAlmostEqual(areas["Здание"], 4.0, places=3)


if __name__ == "__main__":
    unittest.main()
