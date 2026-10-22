"""Кэш чертежей и замена подложки без живого Blender."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ADDON = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"


class Links:
    def __init__(self) -> None:
        self._items: list = []

    def link(self, item) -> None:
        if item not in self._items:
            self._items.append(item)

    def unlink(self, item) -> None:
        if item in self._items:
            self._items.remove(item)

    def __contains__(self, item) -> bool:
        if isinstance(item, str):
            return any(getattr(entry, "name", None) == item for entry in self._items)
        return item in self._items

    def __iter__(self):
        return iter(list(self._items))


class Block:
    def __init__(self, library, name: str) -> None:
        self.library = library
        self._name = name
        self._props: dict = {}
        self.data = None
        self.users = 1
        self.marker = ""
        self.node_tree = None
        self.diffuse_color = (1.0, 1.0, 1.0, 1.0)
        self.type = "MESH"

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        if value == self._name:
            return
        unique = value
        index = 1
        while True:
            owner = self.library.get(unique) if self.library is not None else None
            if owner is None or owner is self:
                break
            unique = f"{value}.{index:03d}"
            index += 1
        self._name = unique

    def as_pointer(self) -> int:
        return id(self)

    def keys(self):
        return self._props.keys()

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value) -> None:
        self._props[key] = value

    def __delitem__(self, key) -> None:
        del self._props[key]


class Mesh(Block):
    pass


class CollectionBlock(Block):
    def __init__(self, library, name: str) -> None:
        super().__init__(library, name)
        self.children = Links()
        self.objects = Links()

    @property
    def all_objects(self):
        found = list(self.objects)
        for child in self.children:
            found.extend(child.all_objects)
        return found


class Library:
    def __init__(self, kind: str, factory) -> None:
        self.kind = kind
        self.factory = factory
        self._items: list = []

    def get(self, name: str):
        for item in self._items:
            if item.name == name:
                return item
        return None

    def new(self, name: str, data=None):
        unique = name
        index = 1
        while self.get(unique) is not None:
            unique = f"{name}.{index:03d}"
            index += 1
        item = self.factory(self, unique)
        if data is not None:
            item.data = data
        self._items.append(item)
        return item

    def remove(self, item, do_unlink: bool = False) -> None:
        if item in self._items:
            self._items.remove(item)
        data = getattr(item, "data", None)
        if self.kind == "object" and data is not None and hasattr(data, "users"):
            data.users = max(0, data.users - 1)
        if do_unlink and self.kind == "object":
            for collection in list(bpy.data.collections):
                collection.objects.unlink(item)

    def __iter__(self):
        return iter(list(self._items))


def _load_build_scene():
    bpy = types.ModuleType("bpy")
    bpy_types = types.ModuleType("bpy.types")
    bpy_types.Mesh = Mesh
    bpy_types.Object = Block
    bpy_types.Collection = CollectionBlock
    bpy_types.Material = Block
    bpy.types = bpy_types
    bpy.data = types.SimpleNamespace()
    bpy.context = types.SimpleNamespace(view_layer=types.SimpleNamespace(update=lambda: None), scene=None)
    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy_types

    mathutils = types.ModuleType("mathutils")

    class Vector:
        def __init__(self, coords=(0.0, 0.0, 0.0)) -> None:
            values = list(coords)
            self.x = float(values[0])
            self.y = float(values[1])
            self.z = float(values[2]) if len(values) > 2 else 0.0

    mathutils.Vector = Vector
    mathutils.Quaternion = lambda *_args, **_kwargs: None
    sys.modules["mathutils"] = mathutils

    package = sys.modules.get("dwgreader_underlay")
    if package is None:
        package = types.ModuleType("dwgreader_underlay")
        package.__path__ = [str(ADDON)]
        package.__package__ = "dwgreader_underlay"
        sys.modules["dwgreader_underlay"] = package

    def load(name: str):
        existing = sys.modules.get(f"dwgreader_underlay.{name}")
        if existing is not None and name != "build_scene":
            return existing
        spec = importlib.util.spec_from_file_location(
            f"dwgreader_underlay.{name}",
            ADDON / f"{name}.py",
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "dwgreader_underlay"
        sys.modules[spec.name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    load("placement_math")
    return bpy, load("build_scene")


bpy, build_scene = _load_build_scene()


def _reset_data() -> None:
    bpy.data.objects = Library("object", Block)
    bpy.data.meshes = Library("mesh", Mesh)
    bpy.data.collections = Library("collection", CollectionBlock)
    bpy.data.materials = Library("material", Block)
    bpy.data.scenes = []
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)


class Sheet:
    def __init__(self, name: str, x: float, y: float) -> None:
        self.name = name
        self.type = "MESH"
        self.bound_box = None
        self.matrix_world = types.SimpleNamespace(translation=types.SimpleNamespace(x=x, y=y, z=0.0))

    def as_pointer(self) -> int:
        return id(self)


class DeadSheet(Sheet):
    def as_pointer(self) -> int:
        raise ReferenceError("removed")


class SheetCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_data()
        build_scene.clear_sheet_cache()

    def test_rebuild_with_the_same_names_uses_the_new_objects(self) -> None:
        first_a, first_b = Sheet("A", 0.0, 0.0), Sheet("B", 5000.0, 0.0)
        root = types.SimpleNamespace(all_objects=[first_a, first_b])
        first = build_scene.sheet_groups(root, 200.0)
        self.assertIs(build_scene.sheet_groups(root, 200.0), first)
        self.assertEqual([[item.name for item in group] for group in first], [["A"], ["B"]])

        wide = build_scene.sheet_groups(root, 6000.0)
        self.assertEqual(len(wide), 1)
        self.assertEqual({item.name for item in wide[0]}, {"A", "B"})

        new_a, new_b = Sheet("A", 0.0, 0.0), Sheet("B", 5000.0, 0.0)
        root.all_objects = [new_a, new_b]
        fresh = build_scene.sheet_groups(root, 200.0)
        self.assertIs(fresh[0][0], new_a)
        self.assertIs(fresh[1][0], new_b)

    def test_dead_cached_objects_are_regrouped(self) -> None:
        live_a, live_b = Sheet("A", 0.0, 0.0), Sheet("B", 5000.0, 0.0)
        root = types.SimpleNamespace(all_objects=[live_a, live_b])
        build_scene.sheet_groups(root, 200.0)
        build_scene._SHEET_CACHE["groups"] = [[DeadSheet("A", 0.0, 0.0)]]
        fresh = build_scene.sheet_groups(root, 200.0)
        self.assertIs(fresh[0][0], live_a)
        self.assertIs(fresh[1][0], live_b)


class ReplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_data()
        build_scene.clear_sheet_cache()
        self._build = build_scene.build
        self.directory = tempfile.TemporaryDirectory()
        self.json_path = Path(self.directory.name) / "underlay.json"
        self.json_path.write_text(
            json.dumps({"root_collection": "SITE_UNDERLAY", "layers": []}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        build_scene.build = self._build
        self.directory.cleanup()

    def _seed(self):
        root = bpy.data.collections.new("SITE_UNDERLAY")
        root["dxf_source"] = "old-drawing"
        root.marker = "old"
        child = bpy.data.collections.new("Газон")
        child.marker = "old-child"
        root.children.link(child)
        mesh = bpy.data.meshes.new("HATCH_Road")
        mesh.marker = "old-mesh"
        obj = bpy.data.objects.new("Road")
        obj["dxf_layer"] = "Road"
        obj.marker = "old"
        obj.data = mesh
        child.objects.link(obj)
        stray = bpy.data.objects.new("Stray")
        stray["dxf_layer"] = "notes"
        stray.marker = "stray"
        material = bpy.data.materials.new("MAT_old")
        material.users = 1
        material.diffuse_color = (0.2, 0.3, 0.4, 1.0)
        base = types.SimpleNamespace(default_value=(0.2, 0.3, 0.4, 1.0))
        rough = types.SimpleNamespace(default_value=0.2)
        material.node_tree = types.SimpleNamespace(
            nodes=[types.SimpleNamespace(type="BSDF_PRINCIPLED", inputs={"Base Color": base, "Roughness": rough})],
        )
        self.base = base
        return root, child, obj, mesh, stray, material

    def _assert_no_parked_names(self) -> None:
        for library in (bpy.data.objects, bpy.data.meshes, bpy.data.collections):
            for block in library:
                self.assertNotIn(build_scene._PARK_KEY, block.keys())
                self.assertFalse(str(block.name).startswith("~p~"))

    def test_invalid_json_does_not_touch_the_current_underlay(self) -> None:
        root, _child, obj, _mesh, stray, _material = self._seed()
        self.json_path.write_text("{", encoding="utf-8")
        called = []

        def boom(*_args, **_kwargs):
            called.append(True)
            raise AssertionError("сборка не должна стартовать")

        build_scene.build = boom
        with self.assertRaises(json.JSONDecodeError):
            build_scene.commit_underlay(self.json_path)
        self.assertEqual(called, [])
        self.assertEqual(root.name, "SITE_UNDERLAY")
        self.assertEqual(obj.name, "Road")
        self.assertEqual(stray.name, "Stray")
        self._assert_no_parked_names()

    def test_failed_replace_restores_the_previous_underlay(self) -> None:
        root, child, obj, mesh, stray, material = self._seed()
        seen = {}

        def fail_build(*_args, **kwargs):
            seen.update(kwargs)
            fresh = bpy.data.collections.new("SITE_UNDERLAY")
            fresh[build_scene._BUILD_ROOT_KEY] = "SITE_UNDERLAY"
            fresh.marker = "new"
            new_obj = bpy.data.objects.new("Road")
            new_obj["dxf_layer"] = "Road"
            new_obj.marker = "new"
            fresh.objects.link(new_obj)
            material.diffuse_color = (0.0, 1.0, 0.0, 1.0)
            self.base.default_value = (0.0, 1.0, 0.0, 1.0)
            extra = bpy.data.materials.new("MAT_NEW")
            extra.users = 0
            raise RuntimeError("mesh failed")

        build_scene.build = fail_build
        with self.assertRaisesRegex(RuntimeError, "mesh failed"):
            build_scene.commit_underlay(self.json_path, scene=types.SimpleNamespace(collection=Links()))

        self.assertFalse(seen["clear_existing"])
        self.assertEqual(seen["root_name"], "SITE_UNDERLAY")
        self.assertIs(bpy.data.collections.get("SITE_UNDERLAY"), root)
        self.assertIs(bpy.data.collections.get("Газон"), child)
        self.assertIs(bpy.data.objects.get("Road"), obj)
        self.assertIs(bpy.data.objects.get("Stray"), stray)
        self.assertIs(bpy.data.meshes.get("HATCH_Road"), mesh)
        self.assertIsNone(bpy.data.materials.get("MAT_NEW"))
        self.assertEqual(material.diffuse_color, (0.2, 0.3, 0.4, 1.0))
        self.assertEqual(self.base.default_value, (0.2, 0.3, 0.4, 1.0))
        self.assertEqual(sorted(block.marker for block in bpy.data.collections), ["old", "old-child"])
        self._assert_no_parked_names()

    def test_failed_first_import_removes_the_partial_underlay(self) -> None:
        def fail_build(*_args, **_kwargs):
            fresh = bpy.data.collections.new("SITE_UNDERLAY")
            fresh[build_scene._BUILD_ROOT_KEY] = "SITE_UNDERLAY"
            new_obj = bpy.data.objects.new("Road")
            new_obj["dxf_layer"] = "Road"
            fresh.objects.link(new_obj)
            raise RuntimeError("mesh failed")

        build_scene.build = fail_build
        with self.assertRaisesRegex(RuntimeError, "mesh failed"):
            build_scene.commit_underlay(self.json_path)
        self.assertEqual(list(bpy.data.collections), [])
        self.assertEqual(list(bpy.data.objects), [])

    def test_successful_replace_drops_the_previous_underlay(self) -> None:
        _root, child, obj, mesh, stray, _material = self._seed()

        def ok_build(*_args, **kwargs):
            self.assertFalse(kwargs["clear_existing"])
            fresh = bpy.data.collections.new("SITE_UNDERLAY")
            fresh[build_scene._BUILD_ROOT_KEY] = "SITE_UNDERLAY"
            fresh["dxf_source"] = "new-drawing"
            fresh.marker = "new"
            new_obj = bpy.data.objects.new("Road")
            new_obj["dxf_layer"] = "Road"
            new_obj.marker = "new"
            fresh.objects.link(new_obj)
            return 4

        build_scene.build = ok_build
        count = build_scene.commit_underlay(self.json_path)
        self.assertEqual(count, 4)
        self.assertEqual([block.name for block in bpy.data.collections], ["SITE_UNDERLAY"])
        current = bpy.data.collections.get("SITE_UNDERLAY")
        self.assertEqual(current.marker, "new")
        self.assertNotIn(build_scene._BUILD_ROOT_KEY, current.keys())
        self.assertIs(bpy.data.objects.get("Road").marker, "new")
        self.assertNotIn(obj, list(bpy.data.objects))
        self.assertNotIn(stray, list(bpy.data.objects))
        self.assertNotIn(mesh, list(bpy.data.meshes))
        self.assertNotIn(child, list(bpy.data.collections))
        self._assert_no_parked_names()


if __name__ == "__main__":
    unittest.main()
