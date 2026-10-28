from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay" / "placement.py"


def _functions(*names: str, globals_: dict | None = None) -> dict:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    namespace = dict(globals_ or {})
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


def _impact_margin(height: float) -> float:
    fall = min(max(float(height), 0.0), 3.0)
    if fall <= 1.5:
        return 1.5
    return min(1.5 + (2.0 / 3.0) * (fall - 1.5), 2.5)


class _Props:
    def __init__(self, **values) -> None:
        self._props = dict(values)
        for key, value in values.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return self._props.get(key, default)

    def keys(self):
        return self._props.keys()

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value) -> None:
        self._props[key] = value


class PlaygroundOrientationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _functions("_play_spec", "_playground_orientation", globals_={"impact_margin": _impact_margin})
        self.asset = {
            "id": "test",
            "footprint": [2.0, 4.0, 1.0],
            "clearance": [0.0, 0.0, 2.0, 2.0],
        }

    def test_clearance_can_change_the_best_orientation(self) -> None:
        # Габарит 2×4 сам по себе выбрал бы B; зона 6×4 требует A.
        self.assertEqual(self.module["_playground_orientation"](5.0, 3.0, self.asset), "A")

    def test_scale_is_part_of_orientation_choice(self) -> None:
        self.assertEqual(self.module["_playground_orientation"](3.0, 5.0, self.asset, scale=0.1), "A")
        self.assertEqual(self.module["_playground_orientation"](3.0, 5.0, self.asset, scale=2.0), "B")


class PlaygroundTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assets = [
            {"id": "child", "family": "playground", "playground_type": "children", "name": "Детская", "file": "child.glb"},
            {"id": "sport", "family": "playground", "playground_type": "sports", "name": "Спортивная", "file": "sport.glb"},
        ]
        self.module = _functions(
            "_play_assets", "_play_items", "_on_playground_type",
            globals_={"_assets": lambda _context, _family: self.assets, "PlacementError": RuntimeError},
        )

    def test_only_the_selected_type_is_available(self) -> None:
        scene = SimpleNamespace(dwgreader_playground_type="sports")
        context = SimpleNamespace(scene=scene)
        self.assertEqual([item["id"] for item in self.module["_play_assets"](context)], ["sport"])
        self.assertEqual([item[0] for item in self.module["_play_items"](None, context)], ["sport"])

    def test_switching_type_resets_an_incompatible_selection(self) -> None:
        scene = SimpleNamespace(dwgreader_playground_type="sports", dwgreader_play_asset="child")
        context = SimpleNamespace(scene=scene)
        self.module["_on_playground_type"](scene, context)
        self.assertEqual(scene.dwgreader_play_asset, "sport")


class PlaygroundSeedTests(unittest.TestCase):
    def test_fill_passes_the_scene_seed_to_the_packer(self) -> None:
        calls = []
        asset = {"id": "child", "footprint": [1.0, 1.0, 1.0]}
        module = _functions(
            "_fill_playground_on",
            globals_={
                "_play_assets": lambda _context: [asset],
                "PlacementError": RuntimeError,
                "_play_spec": lambda current: current,
                "pack_playground": lambda *args, **kwargs: calls.append(kwargs) or [],
                "polygon_centroid_xy": lambda _vertices, _faces: (0.0, 0.0, 0.0),
            },
        )
        context = SimpleNamespace(scene=SimpleNamespace(dwgreader_play_seed=73))
        module["_fill_playground_on"](context, [], [], "zone", blocked=())
        self.assertEqual(calls[0]["seed"], 73)


class BenchReplacementTests(unittest.TestCase):
    def test_replacement_updates_depth_used_by_reshape(self) -> None:
        asset = {
            "id": "new_bench",
            "family": "bench",
            "name": "Новая лавка",
            "footprint": [1.8, 0.75, 0.9],
        }
        source = SimpleNamespace(data=object())
        module = _functions(
            "_replace_benches",
            globals_={
                "_assets": lambda _context, _family: [asset],
                "_import_mesh": lambda _context, _asset: source,
                "_model_scale": lambda _context, _prop: 1.25,
                "_status": lambda _context, _text: None,
                "PlacementError": RuntimeError,
            },
        )
        context = SimpleNamespace(scene=SimpleNamespace(dwgreader_bench_asset="new_bench"))
        child = SimpleNamespace(type="MESH", data=None, scale=None)
        parent = _Props(children=[child])
        module["_replace_benches"](context, parent)
        self.assertEqual(parent["asset_id"], "new_bench")
        self.assertEqual(parent["depth"], 0.75)
        self.assertIs(child.data, source.data)
        self.assertEqual(child.scale, (1.25, 1.25, 1.25))


class PlaygroundScaleTests(unittest.TestCase):
    def test_scaling_revalidates_clearance_and_reports_failure(self) -> None:
        obj = _Props(placement="area")
        obj.type = "MESH"
        obj.scale = (1.0, 1.0, 1.0)
        root = SimpleNamespace(all_objects=[obj])
        bpy = SimpleNamespace(
            data=SimpleNamespace(collections=SimpleNamespace(get=lambda _name: root)),
            context=object(),
        )
        validated = []
        statuses = []
        module = _functions(
            "_scale_placed",
            globals_={
                "bpy": bpy,
                "ROOT_NAME": "SITE_PLACEMENT",
                "_validate_playground": lambda _context, current: validated.append(current) or False,
                "_status": lambda _context, text: statuses.append(text),
            },
        )
        module["_scale_placed"]("area", 2.0)
        self.assertEqual(obj.scale, (2.0, 2.0, 2.0))
        self.assertEqual(validated, [obj])
        self.assertEqual(len(statuses), 1)
        self.assertIn("зона безопасности", statuses[0])


if __name__ == "__main__":
    unittest.main()
