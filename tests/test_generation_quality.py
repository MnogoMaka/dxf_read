from __future__ import annotations

import ast
import base64
import json
import types
import unittest
from pathlib import Path


ADDON = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay" / "mcp_for_blender" / "addon.py"
TREE = ast.parse(ADDON.read_text(encoding="utf-8"), filename=str(ADDON))


def _function(name: str):
    node = next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == name)
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ADDON), "exec"), namespace)
    return namespace[name]


def _methods(*names: str):
    server = next(item for item in TREE.body if isinstance(item, ast.ClassDef) and item.name == "BlenderMCPServer")
    namespace = {}
    for name in names:
        node = next(item for item in server.body if isinstance(item, ast.FunctionDef) and item.name == name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ADDON), "exec"), namespace)
    return namespace


class RodinQualityTests(unittest.TestCase):
    def test_full_quality_is_the_default_and_draft_is_explicit(self) -> None:
        settings = _function("rodin_quality_settings")
        self.assertEqual(settings(), {"tier": "Regular", "quality": "high"})
        self.assertEqual(settings("standard"), {"tier": "Regular", "quality": "medium"})
        self.assertEqual(settings("draft"), {"tier": "Sketch", "quality": "low"})
        with self.assertRaises(ValueError):
            settings("unknown")

    def test_both_rodin_apis_send_the_selected_quality(self) -> None:
        calls = []

        class Response:
            @staticmethod
            def json():
                return {"ok": True}

        requests = types.SimpleNamespace(post=lambda *args, **kwargs: calls.append((args, kwargs)) or Response())
        methods = _methods("create_rodin_job_main_site", "create_rodin_job_fal_ai")
        methods.update(rodin_quality_settings=_function("rodin_quality_settings"), requests=requests,
                       base64=base64, json=json)
        harness = type("RodinHarness", (), methods)()
        harness._get_hyper3d_api_key = lambda: "key"

        self.assertEqual(harness.create_rodin_job_main_site(text_prompt="chair"), {"ok": True})
        files = dict((name, value[1]) for name, value in calls[-1][1]["files"] if name != "images")
        self.assertEqual(files["tier"], "Regular")
        self.assertEqual(files["quality"], "high")

        self.assertEqual(harness.create_rodin_job_fal_ai(text_prompt="chair"), {"ok": True})
        body = calls[-1][1]["json"]
        self.assertEqual(body["tier"], "Regular")
        self.assertEqual(body["quality"], "high")

        self.assertEqual(harness.create_rodin_job_fal_ai(text_prompt="chair", quality="draft"), {"ok": True})
        body = calls[-1][1]["json"]
        self.assertEqual(body["tier"], "Sketch")
        self.assertEqual(body["quality"], "low")


class MeshyQualityTests(unittest.TestCase):
    def test_text_generation_requests_detail_and_explains_required_refine(self) -> None:
        calls = []
        methods = _methods("create_meshy_job", "refine_meshy_job")
        methods.update(MESHY_PROMPT_MAX=800, MESHY_OUTPUT={"target_formats": ["glb"]}, _meshy_refines={})
        harness = type("MeshyHarness", (), methods)()
        harness._resolve_meshy_model = lambda: ("latest", None)

        def post(_path, body):
            calls.append(body)
            return f"task-{len(calls)}", None

        harness._meshy_post = post
        preview = harness.create_meshy_job(text_prompt="chair", quality="high")
        self.assertEqual(calls[0]["geometry_resolution"], "4k")
        self.assertEqual(preview["kind"], "t2p")
        self.assertIn("no textures", preview["next_step"])
        self.assertIn("refine_meshy_job", preview["next_step"])

        refine = harness.refine_meshy_job(preview_task_id=preview["task_id"], quality="high")
        self.assertEqual(calls[1]["texture_resolution"], "4k")
        self.assertTrue(calls[1]["enable_pbr"])
        self.assertEqual(refine["kind"], "t2r")

    def test_invalid_meshy_quality_does_not_start_a_paid_task(self) -> None:
        methods = _methods("create_meshy_job")
        methods.update(MESHY_PROMPT_MAX=800, MESHY_OUTPUT={})
        harness = type("MeshyHarness", (), methods)()
        result = harness.create_meshy_job(text_prompt="chair", quality="ultra")
        self.assertIn("error", result)


class HunyuanDefaultsTests(unittest.TestCase):
    def test_local_generation_defaults_include_detail_and_textures(self) -> None:
        wanted = {
            "blendermcp_hunyuan3d_octree_resolution": 384,
            "blendermcp_hunyuan3d_num_inference_steps": 30,
            "blendermcp_hunyuan3d_texture": True,
        }
        found = {}
        for node in ast.walk(TREE):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in wanted:
                    default = next(arg.value for arg in node.value.keywords if arg.arg == "default")
                    found[target.attr] = ast.literal_eval(default)
        self.assertEqual(found, wanted)


if __name__ == "__main__":
    unittest.main()
