from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ADDON = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"
ROOT = Path(__file__).resolve().parents[1]


def _load_build_scene():
    mathutils = types.ModuleType("mathutils")
    mathutils.Quaternion = type("Quaternion", (), {})
    mathutils.Vector = type("Vector", (), {})
    sys.modules["mathutils"] = mathutils
    if "bpy" not in sys.modules:
        sys.modules["bpy"] = types.ModuleType("bpy")
    package = sys.modules.get("dwgreader_underlay")
    if package is None:
        package = types.ModuleType("dwgreader_underlay")
        package.__path__ = [str(ADDON)]
        package.__package__ = "dwgreader_underlay"
        sys.modules["dwgreader_underlay"] = package

    def load(name: str):
        full = f"dwgreader_underlay.{name}"
        if full in sys.modules:
            return sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, ADDON / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "dwgreader_underlay"
        sys.modules[full] = module
        spec.loader.exec_module(module)
        return module

    load("placement_math")
    return load("build_scene")


build_scene = _load_build_scene()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dxf_read.config import TEXTURE_INSTRUCTIONS  # noqa: E402
from dxf_read.dxf import (  # noqa: E402
    _parse_texture_assignment,
    build_texture_prompt,
    normalize_name,
)


class SurfaceTextureTests(unittest.TestCase):
    def test_only_a_known_key_from_the_model_is_used(self) -> None:
        key = build_scene.surface_texture_key
        for name in ("grass", "tile", "sidewalk", "asphalt", "rubber"):
            self.assertEqual(key(name), name)
        self.assertEqual(key(" Asphalt "), "asphalt")
        for name in ("none", "", "pavement", "ПЛИТКА", None):
            self.assertIsNone(key(name), name)

    def test_tile_size_and_files(self) -> None:
        self.assertEqual(build_scene.SURFACE_TEXTURES["grass"], ("grass.jpg", 4.0))
        self.assertEqual(build_scene.SURFACE_TEXTURES["asphalt"], ("asphalt.png", 2.0))
        self.assertEqual(build_scene.SURFACE_TEXTURES["tile"], ("pavers.jpg", 2.0))
        self.assertEqual(build_scene.SURFACE_TEXTURES["sidewalk"], ("sidewalk.jpg", 2.0))
        self.assertEqual(build_scene.SURFACE_TEXTURES["rubber"], ("rubber.jpg", 2.0))
        names = {item[0] for item in build_scene.SURFACE_TEXTURES.values()}
        self.assertNotIn("асфальт1.jpg", names)

    def test_layer_name_is_not_matched_in_code(self) -> None:
        source = Path(build_scene.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_ASPHALT_TOKENS", source)
        self.assertNotIn("ПЛИТК", source)
        self.assertIn('space.shading.color_type = "TEXTURE"', source)


class TexturePromptTests(unittest.TestCase):
    def test_the_leftmost_material_word_wins(self) -> None:
        text = TEXTURE_INSTRUCTIONS
        self.assertIn("«асфальт_тротуар» — asphalt", text)
        self.assertIn("«тротуар_асфальт» — sidewalk", text)
        self.assertIn("«тротуарная плитка» — sidewalk", text)
        self.assertIn("«плитка_тротуар» — tile", text)
        self.assertIn("«резиновая_крошка_плитка» — rubber", text)
        self.assertIn("слева направо", text)
        self.assertIn("rubber — резиновое покрытие", text)

    def test_prompt_keeps_the_layer_name_verbatim(self) -> None:
        prompt = build_texture_prompt(["1 ГП_асфальт_тротуар", "Резиновая крошка"])
        self.assertIn('"1 ГП_асфальт_тротуар"', prompt)
        self.assertIn('"Резиновая крошка"', prompt)
        self.assertNotIn("__LAYERS__", prompt)

    def test_parser_keeps_the_model_choice_and_drops_the_rest(self) -> None:
        allowed = {
            normalize_name(name): name
            for name in ("1 ГП_асфальт_тротуар", "Газон", "Борт", "Резина")
        }
        reply = (
            "<think>асфальт раньше тротуара</think>\n"
            '{"1 ГП_асфальт_тротуар": "asphalt", "Газон": "grass", '
            '"Борт": "none", "Резина": "rubber", "Выдумка": "tile", '
            '"Газон2": "brick"}'
        )
        assigned = _parse_texture_assignment(reply, allowed)
        self.assertEqual(
            assigned,
            {
                "1 ГП_асфальт_тротуар": "asphalt",
                "Газон": "grass",
                "Резина": "rubber",
            },
        )
