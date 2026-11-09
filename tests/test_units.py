from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ezdxf

from dxf_read.config import PipelineSettings, use_settings
from dxf_read.dxf import (
    _flattened_paths,
    count_layer_objects,
    drawing_to_meters,
    export_blender_underlay,
    extract_classified_hatches,
    extract_curve_pieces,
    hidden_sheet_copies,
    is_junk_layer,
    pieces_from_cache,
    refine_layer_groups,
    unit_scale,
    _path_span,
)


def _square(document, layer: str, origin: tuple[float, float], size: float):
    x, y = origin
    document.modelspace().add_lwpolyline(
        [(x, y), (x + size, y), (x + size, y + size), (x, y + size)],
        close=True,
        dxfattribs={"layer": layer},
    )


class UnitScaleTests(unittest.TestCase):
    def test_millimetre_header_scales_regardless_of_segment_lengths(self) -> None:
        self.assertEqual(unit_scale(4, 488.0), 0.001)
        self.assertEqual(unit_scale(4, 1.41), 0.001)
        self.assertEqual(unit_scale(6, 488.0), 1.0)
        self.assertEqual(unit_scale(0, 488.0), 1.0)

    def test_closed_millimetre_square_becomes_one_face(self) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        _square(document, "Контур", (0.0, 0.0), 59000.0)
        self.assertEqual(drawing_to_meters(document), 0.001)
        pieces = extract_curve_pieces(
            document, {"pavement": ["Контур"]}, lock_groups=True,
        )
        self.assertEqual(len(pieces), 1)
        self.assertAlmostEqual(_path_span(pieces[0].vertices), 59.0, places=3)

    def test_short_millimetre_header_is_converted_to_metres(self) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        _square(document, "Контур", (0.0, 0.0), 10.0)
        self.assertEqual(drawing_to_meters(document), 0.001)
        with use_settings(PipelineSettings(min_arrangement_area_m2=0.0)):
            pieces = extract_curve_pieces(
                document, {"pavement": ["Контур"]}, lock_groups=True,
            )
        self.assertEqual(len(pieces), 1)
        self.assertAlmostEqual(_path_span(pieces[0].vertices), 0.01, places=3)

    def test_hatch_only_millimetre_drawing_uses_header(self) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        hatch = document.modelspace().add_hatch(dxfattribs={"layer": "Газон"})
        hatch.set_solid_fill()
        hatch.paths.add_polyline_path([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], is_closed=True)
        self.assertEqual(drawing_to_meters(document), 0.001)
        pieces = extract_classified_hatches(document, {"green": ["Газон"]}, lock_groups=True)
        self.assertEqual(len(pieces), 1)
        self.assertAlmostEqual(_path_span(pieces[0].vertices), 1.0, places=3)

    def test_fill_request_ignores_closed_polyline(self) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        _square(document, "Контур", (0.0, 0.0), 59000.0)
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "underlay.json"
            result = export_blender_underlay(
                document,
                {"pavement": ["Контур"]},
                json_path=target,
                run_blender=False,
                lock_groups=True,
                geometry_source="fills",
            )
        self.assertEqual(result["layers"], 0)

    def test_counts_use_metres(self) -> None:
        document = ezdxf.new()
        document.header["$INSUNITS"] = 4
        _square(document, "A", (0.0, 0.0), 1000.0)
        _square(document, "B", (900000.0, 0.0), 1000.0)
        counts = count_layer_objects(document, {"pavement": ["A", "B"]})
        self.assertEqual(counts["A"]["curves"], 1)
        self.assertEqual(counts["B"]["curves"], 1)


class SheetCopyTests(unittest.TestCase):
    def test_plain_layer_hides_numbered_copies(self) -> None:
        hidden = hidden_sheet_copies(["Газон", "Газон_Ном._пера__2", "Газон_Ном._пера__6"])
        self.assertEqual(hidden, {"Газон_Ном._пера__2", "Газон_Ном._пера__6"})
        groups = refine_layer_groups(
            {"green": ["Газон", "Газон_Ном._пера__2"]},
            ["Газон", "Газон_Ном._пера__2"],
        )
        self.assertEqual(groups["green"], ["Газон"])

    def test_richest_copy_stays_when_no_plain_layer(self) -> None:
        names = ["Газон_Ном._пера__2", "Газон_Ном._пера__6"]
        hidden = hidden_sheet_copies(names, {"Газон_Ном._пера__2": 10, "Газон_Ном._пера__6": 40})
        self.assertEqual(hidden, {"Газон_Ном._пера__2"})
        groups = refine_layer_groups({}, names, {"Газон_Ном._пера__2": 10, "Газон_Ном._пера__6": 40})
        self.assertEqual(groups["green"], ["Газон_Ном._пера__6"])

    def test_flower_beds_become_plantings_and_red_lines_drop(self) -> None:
        groups = refine_layer_groups(
            {
                "green": ["Б_цветники_1", "Леса и газоны", "3 Озел_цветники"],
                "site_boundary": ["Красные линии_4", "Граница участка", "ГПЗУ", "Красные линии ГПЗУ"],
            },
            [
                "Б_цветники_1", "Леса и газоны", "3 Озел_цветники",
                "Красные линии_4", "Граница участка", "ГПЗУ", "Красные линии ГПЗУ",
            ],
        )
        self.assertEqual(groups["green"], ["Леса и газоны"])
        self.assertEqual(groups["vegetation"], ["3 Озел_цветники", "Б_цветники_1"])
        self.assertEqual(
            groups["site_boundary"],
            ["ГПЗУ", "Граница участка", "Красные линии ГПЗУ"],
        )
        self.assertTrue(is_junk_layer("Красные линии_4"))
        self.assertFalse(is_junk_layer("Граница участка"))

    def test_interior_copy_stays_out_of_automatic_groups(self) -> None:
        name = "Интерьер - Перегородка_Ном._пера__150"
        self.assertTrue(is_junk_layer(name))
        groups = refine_layer_groups({"building": [name]}, [name], {name: 1})
        self.assertEqual(groups, {})


def _solid_hatch(document, layer: str, origin: tuple[float, float], size: float):
    x, y = origin
    hatch = document.modelspace().add_hatch(dxfattribs={"layer": layer})
    hatch.set_solid_fill(color=7)
    hatch.paths.add_polyline_path(
        [(x, y), (x + size, y), (x + size, y + size), (x, y + size)],
        is_closed=True,
    )
    return hatch


class BuildSettingTests(unittest.TestCase):
    def test_saved_parse_attaches_layer_zero_fill_to_building(self) -> None:
        payload = {
            "layers": ["Стены", "0"],
            "fills": [
                ["h", "Стены", "", 1, "B", [[0, 0], [10, 0], [10, 10], [0, 10]], []],
                ["h", "0", "", 1, "Z", [[1, 1], [2, 1], [2, 2], [1, 2]], []],
            ],
            "curves": [],
        }
        pieces = pieces_from_cache(payload, {"building": ["Стены"]}, "fills")
        self.assertEqual(len(pieces), 2)
        self.assertTrue(all(piece.category == "building" for piece in pieces))

    def test_saved_parse_leaves_a_distant_layer_zero_fill(self) -> None:
        payload = {
            "layers": ["Стены", "0"],
            "fills": [
                ["h", "Стены", "", 1, "B", [[0, 0], [10, 0], [10, 10], [0, 10]], []],
                ["h", "0", "", 1, "Z", [[100, 100], [101, 100], [101, 101], [100, 101]], []],
            ],
            "curves": [],
        }
        pieces = pieces_from_cache(payload, {"building": ["Стены"]}, "fills")
        self.assertEqual(len(pieces), 1)

    def test_confirmed_layers_still_attach_layer_zero_fill(self) -> None:
        document = ezdxf.new()
        _solid_hatch(document, "Стены", (0.0, 0.0), 10.0)
        _solid_hatch(document, "0", (1.0, 1.0), 1.0)
        pieces = extract_classified_hatches(
            document, {"building": ["Стены"]}, lock_groups=True,
        )
        self.assertEqual(len(pieces), 2)

    def test_scene_scale_is_applied_when_the_underlay_is_written(self) -> None:
        document = ezdxf.new()
        _square(document, "Контур", (0.0, 0.0), 10.0)
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "underlay.json"
            with use_settings(PipelineSettings(scene_scale=2.0, origin_mode="zero")):
                export_blender_underlay(
                    document,
                    {"pavement": ["Контур"]},
                    json_path=target,
                    run_blender=False,
                    lock_groups=True,
                    geometry_source="curves",
                )
            payload = json.loads(target.read_text(encoding="utf-8"))
        xs = [vertex[0] for vertex in payload["layers"][0]["vertices"]]
        self.assertAlmostEqual(max(xs) - min(xs), 20.0, places=2)

    def test_flattening_distance_changes_an_arc(self) -> None:
        document = ezdxf.new()
        arc = document.modelspace().add_arc(
            (0.0, 0.0), radius=10.0, start_angle=0.0, end_angle=90.0,
            dxfattribs={"layer": "Дуга"},
        )
        with use_settings(PipelineSettings(flattening_distance_m=1.0)):
            coarse = _flattened_paths(arc, 1.0)
        with use_settings(PipelineSettings(flattening_distance_m=0.01)):
            fine = _flattened_paths(arc, 1.0)
        self.assertGreater(len(fine[0][0]), len(coarse[0][0]))

class CacheContourTests(unittest.TestCase):
    def test_fill_mode_skips_closed_polyline_without_hatch(self) -> None:
        payload = {
            "layers": ["Контур"],
            "fills": [["p", "Контур", "", 0, "AB", [[0, 0], [10, 0], [10, 10], [0, 10]], []]],
            "curves": [],
        }
        pieces = pieces_from_cache(payload, {"pavement": ["Контур"]}, "fills")
        self.assertEqual(pieces, [])

    def test_hatch_layer_does_not_add_its_polyline(self) -> None:
        payload = {
            "layers": ["Контур"],
            "fills": [
                ["h", "Контур", "", 1, "H1", [[0, 0], [4, 0], [4, 4], [0, 4]], []],
                ["p", "Контур", "", 0, "P1", [[0, 0], [4, 0], [4, 4], [0, 4]], []],
            ],
            "curves": [],
        }
        pieces = pieces_from_cache(payload, {"pavement": ["Контур"]}, "fills")
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].handle, "H1")


if __name__ == "__main__":
    unittest.main()
