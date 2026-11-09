from __future__ import annotations

import json
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "maf_blender_models"
NEW_MODELS = {
    "play_04": "Игровой комплекс_1.glb",
    "play_05": "Игровой комплекс_2.glb",
    "play_06": "Игровой комплекс_3.glb",
    "play_07": "Игровой комплекс_4.glb",
    "play_08": "Игровой комплекс_5.glb",
    "play_09": "Игровой комплекс_6.glb",
    "play_10": "Игровой комплекс_7.glb",
    "play_11": "Игровой комплекс_8.glb",
    "play_12": "Игровой комплекс_9.glb",
    "play_13": "Игровой комплекс_10.glb",
    "play_14": "Игровой комплекс_11.glb",
    "play_15": "Игровой комплекс_12.glb",
    "play_16": "Игровой комплекс_13.glb",
    "play_17": "Игровой комплекс_14.glb",
    "slide_01": "горка.glb",
    "slide_02": "горка_2.glb",
    "sandbox_01": "Песочница.glb",
    "house_01": "домик.glb",
    "rocker_01": "Качалка_1.glb",
    "rocker_02": "Качалка_2.glb",
    "rocker_03": "Качалка_3.glb",
    "rocker_04": "Качалка_4.glb",
    "swing_02": "качель_1.glb",
    "swing_03": "качель_2.glb",
}


class MafCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads((LIBRARY / "catalog.json").read_text(encoding="utf-8"))
        cls.by_id = {item["id"]: item for item in cls.catalog}

    def test_ids_are_unique_and_every_model_file_exists(self) -> None:
        self.assertEqual(len(self.by_id), len(self.catalog))
        for item in self.catalog:
            with self.subTest(asset=item["id"]):
                self.assertTrue((LIBRARY / item["file"]).is_file())

    def test_all_24_new_models_are_connected(self) -> None:
        self.assertEqual(len(NEW_MODELS), 24)
        for asset_id, filename in NEW_MODELS.items():
            with self.subTest(asset=asset_id):
                self.assertIn(asset_id, self.by_id)
                self.assertEqual(self.by_id[asset_id]["file"], filename)
                self.assertEqual(self.by_id[asset_id]["family"], "playground")
                self.assertEqual(self.by_id[asset_id]["placement"], "area")
                self.assertEqual(self.by_id[asset_id]["playground_type"], "children")

    def test_children_and_sports_never_share_an_asset(self) -> None:
        playgrounds = [item for item in self.catalog if item["family"] == "playground"]
        sports = {item["id"] for item in playgrounds if item.get("playground_type") == "sports"}
        children = {item["id"] for item in playgrounds if item.get("playground_type") == "children"}
        self.assertEqual(sports, {
            "play_big",
            "gym_01", "gym_02", "gym_03", "gym_04",
            "bar_01", "bar_02", "bar_03", "bar_04", "bar_05",
            "bar_06", "bar_07", "bar_08", "bar_09", "bar_11",
        })
        self.assertEqual(children, {item["id"] for item in playgrounds} - sports)
        self.assertFalse(sports & children)

    def test_dimensions_and_clearance_are_valid(self) -> None:
        heroes = {"children": set(), "sports": set()}
        for item in self.catalog:
            with self.subTest(asset=item["id"]):
                self.assertEqual(len(item["footprint"]), 3)
                self.assertTrue(all(math.isfinite(value) and value > 0 for value in item["footprint"]))
                if item["family"] != "playground":
                    continue
                self.assertIn(item.get("playground_type"), {"children", "sports"})
                self.assertIn(item.get("role"), {"hero", "activity", "accent"})
                self.assertGreater(float(item["fall_height"]), 0.0)
                self.assertLessEqual(float(item["fall_height"]), 3.0)
                if item["role"] == "hero":
                    heroes[item["playground_type"]].add(item["id"])
                if item.get("motion") == "swing":
                    self.assertEqual(len(item["clearance"]), 4)
                    self.assertTrue(all(math.isfinite(value) and value >= 0 for value in item["clearance"]))
                if item.get("motion") == "slide":
                    self.assertEqual(item["runout"], 2.0)
                    self.assertIn(item["runout_side"], {"front", "back", "left", "right"})
        self.assertEqual(heroes["sports"], {"play_big"})
        self.assertEqual(heroes["children"], {"play_08"})


if __name__ == "__main__":
    unittest.main()
