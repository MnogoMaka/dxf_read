from __future__ import annotations

import importlib.util
import json
import base64
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ADDON = Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"


def _load_scene_chat():
    bpy = types.ModuleType("bpy")
    props = types.ModuleType("bpy.props")
    props.BoolProperty = lambda **_kwargs: None
    props.StringProperty = lambda **_kwargs: None
    bpy_types = types.ModuleType("bpy.types")

    class Operator:
        pass

    bpy_types.Operator = Operator
    app = types.ModuleType("bpy.app")
    app.timers = types.SimpleNamespace(is_registered=lambda _fn: False)
    bpy.props = props
    bpy.types = bpy_types
    bpy.app = app
    sys.modules["bpy"] = bpy
    sys.modules["bpy.props"] = props
    sys.modules["bpy.types"] = bpy_types
    sys.modules["bpy.app"] = app

    package = types.ModuleType("dwgreader_underlay")
    package.__path__ = [str(ADDON)]
    package.__package__ = "dwgreader_underlay"
    sys.modules["dwgreader_underlay"] = package

    def load(name: str):
        spec = importlib.util.spec_from_file_location(
            f"dwgreader_underlay.{name}",
            ADDON / f"{name}.py",
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "dwgreader_underlay"
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    load("chatgpt_auth")
    load("mcp_bridge")
    load("scene_changes")
    return load("scene_chat")


scene_chat = _load_scene_chat()


def _sse(output: list) -> bytes:
    payload = json.dumps(
        {"type": "response.completed", "response": {"output": output}},
        ensure_ascii=False,
    )
    return f"event: response.completed\ndata: {payload}\n\n".encode("utf-8")


class LoopTests(unittest.TestCase):
    def test_tools_then_text_and_image(self) -> None:
        seen_http: list[dict] = []
        seen_socket: list[tuple] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen_http.append(json.loads(raw.decode("utf-8")))
                if len(seen_http) == 1:
                    output = [
                        {
                            "type": "function_call",
                            "call_id": "obj",
                            "name": "get_object_info",
                            "arguments": json.dumps({"object_name": "Газон"}),
                        },
                        {
                            "type": "function_call",
                            "call_id": "shot",
                            "name": "look",
                            "arguments": "{}",
                        },
                    ]
                else:
                    output = [{
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Вижу газон"}],
                    }]
                body = _sse(output)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def fake_command(command_type, params=None, **_kwargs):
            params = params or {}
            seen_socket.append((command_type, dict(params)))
            if command_type == "get_viewport_screenshot":
                Path(params["filepath"]).write_bytes(b"\x89PNG")
            return {"ok": True}

        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        scene_chat.send_command = fake_command
        try:
            text, image = scene_chat.complete_turn("token", "gpt-test", [], "покажи газон")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if image:
                Path(image).unlink(missing_ok=True)

        self.assertEqual(text, "Вижу газон")
        self.assertTrue(seen_http[0]["stream"])
        self.assertEqual(seen_http[0]["tools"][0]["type"], "namespace")
        self.assertFalse(seen_http[0]["tools"][0]["tools"][0]["strict"])
        self.assertEqual(seen_http[0]["reasoning"]["summary"], "auto")
        self.assertEqual(seen_http[0]["reasoning"]["effort"], "high")
        self.assertIn("reasoning.encrypted_content", seen_http[0]["include"])
        names = [item[0] for item in seen_socket]
        self.assertEqual(names, ["get_object_info", "get_viewport_screenshot"])
        self.assertEqual(seen_socket[0][1]["name"], "Газон")
        self.assertEqual(seen_socket[1][1]["max_size"], 1400)
        blob = json.dumps(seen_http[1], ensure_ascii=False)
        self.assertIn("function_call_output", blob)
        self.assertIn("data:image/png;base64,", blob)
        tool_outputs = [item for item in seen_http[1]["input"] if item.get("type") == "function_call_output"]
        self.assertTrue(any(
            isinstance(item.get("output"), list)
            and any(part.get("type") == "input_image" for part in item["output"] if isinstance(part, dict))
            for item in tool_outputs
        ))
        names_for_model = [item["name"] for item in seen_http[0]["tools"][0]["tools"]]
        self.assertIn("search_polyhaven_assets", names_for_model)
        self.assertIn("create_meshy_job", names_for_model)

    def test_closed_port_stops_the_turn(self) -> None:
        def fake_command(*_args, **_kwargs):
            raise scene_chat.BridgeError("Сервер MCP не запущен")

        scene_chat.send_command = fake_command

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                _raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = _sse([{
                    "type": "function_call",
                    "call_id": "code",
                    "name": "blender.execute_blender_code",
                    "arguments": json.dumps({"code": "print(1)"}),
                }])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        try:
            with self.assertRaises(scene_chat.ChatError):
                scene_chat.complete_turn("token", "gpt-test", [], "построй куб")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_detail_is_the_visible_error(self) -> None:
        text = scene_chat._failure_text('{"detail":"Streaming is required for this model"}', 400)
        self.assertEqual(text, "Streaming is required for this model")

    def test_tool_call_is_taken_from_the_stream_when_the_final_event_is_empty(self) -> None:
        seen: list[tuple] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body_json = json.loads(raw.decode("utf-8"))
                call = {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "execute_blender_code",
                    "arguments": "",
                }
                if not any(item.get("type") == "function_call" for item in body_json["input"] if isinstance(item, dict)):
                    events = [
                        {"type": "response.output_item.added", "output_index": 0, "item": call},
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": "fc_1",
                            "arguments": json.dumps({"code": "print(1)"}),
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {**call, "arguments": json.dumps({"code": "print(1)"})},
                        },
                        {"type": "response.completed", "response": {"output": [], "status": "completed"}},
                    ]
                else:
                    events = [
                        {"type": "response.output_text.delta", "delta": "Куб удалён"},
                        {"type": "response.completed", "response": {"output": [], "status": "completed"}},
                    ]
                payload = b"".join(
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8") for event in events
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _fmt, *_args):
                return

        def fake_command(command_type, params=None, **_kwargs):
            seen.append((command_type, params or {}))
            return {"result": "1\n"}

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        scene_chat.send_command = fake_command
        try:
            text, _image = scene_chat.complete_turn("token", "gpt-test", [], "удали куб")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(seen[0][0], "execute_code")
        self.assertIn("print(1)", seen[0][1]["code"])
        self.assertEqual(text, "Куб удалён")

    def test_later_request_keeps_the_saved_output(self) -> None:
        seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append(json.loads(raw.decode("utf-8")))
                if len(seen) == 1:
                    output = [
                        {"type": "reasoning", "id": "rs_1", "encrypted_content": "keep-me", "summary": []},
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "execute_blender_code",
                            "arguments": json.dumps({"code": "print(1)"}),
                        },
                    ]
                else:
                    output = [{"type": "message", "content": [{"type": "output_text", "text": "готово"}]}]
                body = _sse(output)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        def fake_command(command_type, params=None, **_kwargs):
            return {"result": "1\n"}

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        scene_chat.send_command = fake_command
        try:
            scene_chat.complete_turn("token", "gpt-test", [], "удали куб")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        kinds = [item.get("type") for item in seen[1]["input"] if isinstance(item, dict)]
        self.assertIn("reasoning", kinds)
        reasoning = next(item for item in seen[1]["input"] if item.get("type") == "reasoning")
        self.assertEqual(reasoning["encrypted_content"], "keep-me")

    def test_plan_limit_names_the_usage_page(self) -> None:
        text = scene_chat._failure_text(
            '{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"limit"}}',
            429,
        )
        self.assertIn("chatgpt.com/codex/settings/usage", text)

    def test_register_does_not_rewrite_the_spare_api_address(self) -> None:
        source = (ADDON / "__init__.py").read_text(encoding="utf-8")
        config = (ADDON.parents[1] / "dxf_read" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn('prefs.llm_base_url = "https://llm-router.cxm.dev/v1/"', source)
        for blob in (source, config):
            self.assertNotIn("llm-router.cxm.dev", blob)
            self.assertNotIn("qwen/qwen3-vl-30b-a3b-instruct", blob)
            self.assertNotIn("google/gemma-4-e4b", blob)

    def test_rebuild_clears_placed_models_before_the_new_underlay(self) -> None:
        source = (ADDON / "__init__.py").read_text(encoding="utf-8")
        body = source.split("def _schedule_underlay_replace", 1)[1].split("def build_underlay", 1)[0]
        self.assertLess(body.find("placement.clear_placement()"), body.find("build_underlay("))
        self.assertLess(body.find("placement.clear_placement()"), body.find("furnish_underlay("))

    def test_status_keeps_the_building_skip_count(self) -> None:
        source = (ADDON / "__init__.py").read_text(encoding="utf-8")
        start = source.index("def _fit_status")
        end = source.index("\ndef _schedule_underlay_replace", start)
        namespace: dict = {}
        exec(source[start:end], namespace)
        text = "Готово: объектов в сцене 40. " + ("деревья 12, " * 30) + "здания 3, мельче порога 12"
        fitted = namespace["_fit_status"](text)
        self.assertLessEqual(len(fitted), 180)
        self.assertIn("здания 3, мельче порога 12", fitted)

    def test_tool_row_is_sent_as_a_tool_result(self) -> None:
        scene_chat._REPLAY.put("сделал", ["call_1"], [
            {"type": "reasoning", "id": "rs_9", "encrypted_content": "keep-me", "summary": []},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "execute_blender_code",
                "arguments": "{\"code\":\"print(1)\"}",
            },
        ])
        items = scene_chat._history_items([
            {"role": "user", "text": "удали куб"},
            {"role": "assistant", "text": "сделал", "call_ids": ["call_1"]},
            {
                "role": "tool",
                "text": "execute_blender_code",
                "name": "execute_blender_code",
                "call_id": "call_1",
                "arguments": "{\"code\":\"print(1)\"}",
                "output": "1",
            },
        ])
        blob = json.dumps(items, ensure_ascii=False)
        self.assertIn("keep-me", blob)
        self.assertIn("function_call_output", blob)
        user_blob = json.dumps(
            [item for item in items if item.get("role") == "user"],
            ensure_ascii=False,
        )
        self.assertNotIn("execute_blender_code", user_blob)

    def test_next_user_message_receives_the_saved_output(self) -> None:
        scene_chat._REPLAY._items.clear()
        seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append(json.loads(raw.decode("utf-8")))
                if len(seen) == 1:
                    output = [
                        {"type": "reasoning", "id": "rs_2", "encrypted_content": "keep-me", "summary": []},
                        {"type": "message", "content": [{"type": "output_text", "text": "Куб удалён"}]},
                    ]
                else:
                    output = [{"type": "message", "content": [{"type": "output_text", "text": "Дальше"}]}]
                body = _sse(output)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        try:
            scene_chat.complete_turn("token", "gpt-test", [], "удали куб")
            history = [{"role": "user", "text": "удали куб"}, *scene_chat._TRANSCRIPT]
            scene_chat.complete_turn("token", "gpt-test", history, "теперь шар")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        blob = json.dumps(seen[1]["input"], ensure_ascii=False)
        self.assertIn("keep-me", blob)
        self.assertIn("reasoning", blob)

    def test_refused_include_is_dropped_for_the_session(self) -> None:
        scene_chat._DISABLED.clear()
        seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body_json = json.loads(raw.decode("utf-8"))
                seen.append(body_json)
                if "include" in body_json:
                    payload = json.dumps({
                        "error": {"message": "Unknown parameter: include", "param": "include", "code": "unknown_parameter"},
                    }).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                body = _sse([{"type": "message", "content": [{"type": "output_text", "text": "ок"}]}])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        try:
            text, _image = scene_chat.complete_turn("token", "gpt-test", [], "привет")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            scene_chat._DISABLED.clear()

        self.assertEqual(text, "ок")
        self.assertIn("include", seen[0])
        self.assertNotIn("include", seen[1])

    def test_refused_reasoning_effort_falls_back_without_losing_summary(self) -> None:
        scene_chat._DISABLED.clear()
        seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body_json = json.loads(raw.decode("utf-8"))
                seen.append(body_json)
                if body_json.get("reasoning", {}).get("effort"):
                    payload = json.dumps({
                        "error": {
                            "message": "Unsupported reasoning effort for this model",
                            "param": "reasoning.effort",
                            "code": "unsupported_parameter",
                        },
                    }).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                body = _sse([{"type": "message", "content": [{"type": "output_text", "text": "ок"}]}])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scene_chat.RESPONSES_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        try:
            text, _image = scene_chat.complete_turn("token", "gpt-test", [], "привет")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            scene_chat._DISABLED.clear()

        self.assertEqual(text, "ок")
        self.assertEqual(seen[0]["reasoning"]["effort"], "high")
        self.assertNotIn("effort", seen[1]["reasoning"])
        self.assertEqual(seen[1]["reasoning"]["summary"], "auto")

    def test_library_command_reaches_the_plugin(self) -> None:
        seen: list[tuple] = []

        def fake_command(command_type, params=None, **_kwargs):
            seen.append((command_type, params or {}))
            if command_type == "get_polyhaven_asset_preview":
                return {"image_data": base64.b64encode(b"png").decode("ascii"), "format": "png", "name": "Rock"}
            return {"ok": True}

        scene_chat.send_command = fake_command
        text, _image = scene_chat._run_tool(
            "search_polyhaven_assets",
            {"query": "диван", "asset_type": "models", "extra": "нет"},
        )
        self.assertEqual(seen[0][0], "search_polyhaven_assets")
        self.assertEqual(seen[0][1]["query"], "диван")
        self.assertNotIn("extra", seen[0][1])
        self.assertIn("ok", text)

        preview, path = scene_chat._run_tool("get_polyhaven_asset_preview", {"asset_id": "rock"})
        try:
            self.assertNotIn("image_data", preview)
            self.assertTrue(path and Path(path).is_file())
        finally:
            if path:
                Path(path).unlink(missing_ok=True)


class SceneChangeTests(unittest.TestCase):
    def test_diff_groups_added_lights_and_a_rename(self) -> None:
        changes = sys.modules["dwgreader_underlay.scene_changes"]
        before = {
            "objects": {1: {"name": "Куб", "type": "MESH", "collection": "Сцена", "loc": (0, 0, 0)}},
            "camera": "",
        }
        after = {
            "objects": {
                1: {"name": "Основа", "type": "MESH", "collection": "Сцена", "loc": (0, 0, 0)},
                2: {"name": "Key", "type": "LIGHT", "collection": "Lights", "loc": (1, 2, 3)},
                3: {"name": "Fill", "type": "LIGHT", "collection": "Lights", "loc": (0, 0, 1)},
            },
            "camera": "Camera",
        }
        diff = changes.diff_snapshots(before, after)
        self.assertEqual(changes.headline(diff), "2 добавлено, 1 переименовано")
        lines = changes.change_lines(diff)
        self.assertIn("+ Lights", lines)
        self.assertIn("  + Key (свет)", lines)
        self.assertIn("~ Куб → Основа", lines)
        self.assertIn("~ Camera (камера), теперь камера сцены", lines)
        history = scene_chat._history_items([
            {"role": "changes", "text": "2 добавлено", "lines": ["+ Lights"], "checkpoint": "abc"},
            {"role": "user", "text": "дальше"},
        ])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"][0]["text"], "дальше")


class UserPhotoTests(unittest.TestCase):
    def test_photo_is_sent_as_input_image_and_not_stored_inline(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "ask.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nphoto")
            parts = scene_chat.user_parts("поставь как тут", [str(path)])
            self.assertEqual(parts[0]["text"], "поставь как тут")
            self.assertEqual(parts[1]["type"], "input_image")
            self.assertTrue(parts[1]["image_url"].startswith("data:image/png;base64,"))
            history = scene_chat._history_items([
                {"role": "user", "text": "поставь как тут", "images": [str(path)]},
            ])
            self.assertEqual(history[0]["content"][1]["type"], "input_image")
            saved = json.dumps({"text": "поставь как тут", "images": [str(path)]}, ensure_ascii=False)
            self.assertNotIn("base64", saved)


class SpareApiTests(unittest.TestCase):
    def tearDown(self) -> None:
        scene_chat.chatgpt_auth.public_profile = self._profile

    def setUp(self) -> None:
        self._profile = scene_chat.chatgpt_auth.public_profile

    def test_without_chatgpt_login_the_api_fields_are_enough(self) -> None:
        scene_chat.chatgpt_auth.public_profile = lambda: None
        ready = types.SimpleNamespace(
            chatgpt_model="",
            llm_base_url="https://llm.example/v1/",
            llm_model="qwen/qwen3",
            llm_api_key="",
            llm_timeout=120.0,
        )
        self.assertEqual(scene_chat.login_hint(ready), "")
        self.assertEqual(scene_chat.chosen_model(ready), "qwen/qwen3")
        empty = types.SimpleNamespace(
            chatgpt_model="",
            llm_base_url="",
            llm_model="",
            llm_api_key="",
            llm_timeout=120.0,
        )
        self.assertIn("API", scene_chat.login_hint(empty))

    def test_chatgpt_session_does_not_fall_back_to_api(self) -> None:
        scene_chat.chatgpt_auth.public_profile = lambda: {"email": "a@b.c", "sharing": True}
        prefs = types.SimpleNamespace(
            chatgpt_model="none",
            llm_base_url="https://llm.example/v1/",
            llm_model="qwen/qwen3",
            llm_api_key="k",
            llm_timeout=120.0,
        )
        self.assertEqual(scene_chat.login_hint(prefs), "Модель ChatGPT не выбрана")

    def test_completions_tools_then_text(self) -> None:
        seen_http: list[dict] = []
        seen_socket: list[tuple] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen_http.append(json.loads(raw.decode("utf-8")))
                if len(seen_http) == 1:
                    message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_look",
                            "type": "function",
                            "function": {"name": "look", "arguments": "{}"},
                        }],
                    }
                else:
                    message = {"role": "assistant", "content": "Сцену вижу"}
                payload = json.dumps({"choices": [{"message": message}]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _fmt, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def fake_command(command_type, params=None, **_kwargs):
            params = params or {}
            seen_socket.append((command_type, dict(params)))
            if command_type == "get_viewport_screenshot":
                Path(params["filepath"]).write_bytes(b"\x89PNG")
            return {"ok": True}

        previous = scene_chat.send_command
        scene_chat.send_command = fake_command
        api = {
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "api_key": "router-key",
            "timeout": 5.0,
        }
        try:
            text, image = scene_chat.complete_turn(
                "", "qwen/qwen3", [], "покажи сцену", api=api,
            )
        finally:
            scene_chat.send_command = previous
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if image:
                Path(image).unlink(missing_ok=True)

        self.assertEqual(text, "Сцену вижу")
        self.assertEqual(seen_http[0]["model"], "qwen/qwen3")
        self.assertFalse(seen_http[0]["stream"])
        self.assertEqual(seen_http[0]["tools"][0]["type"], "function")
        self.assertEqual(seen_http[0]["tools"][0]["function"]["name"], "execute_blender_code")
        self.assertEqual([item[0] for item in seen_socket], ["get_viewport_screenshot"])
        roles = [item.get("role") for item in seen_http[1]["messages"]]
        self.assertIn("tool", roles)
        self.assertEqual(seen_http[1]["messages"][0]["role"], "system")


class ChatWindowTests(unittest.TestCase):
    def test_chat_uses_loopcut_rect_and_its_own_window(self) -> None:
        source = (ADDON / "chat_view.py").read_text(encoding="utf-8")
        self.assertIn("ModelViewProjectionMatrix", source)
        self.assertIn('bpy.ops.screen.area_dupli("INVOKE_DEFAULT")', source)
        self.assertIn("request_chat_window", source)
        self.assertIn("CHAT_WIDTH = 420", source)
        self.assertIn("CHAT_HEIGHT = 700", source)
        self.assertIn("Опишите, что сделать со сценой", source)
        self.assertIn('SetWindowTextW(hwnd, "Чат")', source)
        self.assertNotIn("Отправить", source)
        self.assertIn("Спросите, что сделать со сценой", source)
        self.assertIn("Думаю", source)
        self.assertIn("IMAGE_EDITOR", source)
        self.assertIn("poll_drop", source)
        self.assertIn("clipboard_image_path", source)
        self.assertIn("caret_at", source)
        self.assertIn("def _fit_box", source)
        self.assertIn("def _draw_spinner", source)
        self.assertIn("time.monotonic()", source)
        self.assertNotIn("pen_top - 68", source)
        self.assertIn("_draw_clip", source)
        self.assertIn("clip_x = field[0] + field[2]", source)
        self.assertIn("clip = 26 * scale", source)
        self.assertIn("Оставить", source)
        self.assertIn("Откатить", source)
        self.assertNotIn("_pause_main_view", source)
        self.assertNotIn("_sync_drag", source)
        draw = source.split("def _draw() -> None:", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("show_object_viewport_mesh", draw)
        self.assertNotIn("_hide_editor_flags", draw)
        self.assertNotIn("_schedule_close", draw)
        self.assertNotIn("localview", source)
        self.assertNotIn("load_projection_matrix", source)
        self.assertNotIn("UNIFORM_COLOR", source)
        self.assertIn('return 0.0, 0.0, float(region.width), float(region.height)', source)


class McpReadyTests(unittest.TestCase):
    def test_missing_server_operator_is_not_ready(self) -> None:
        self.assertFalse(scene_chat.mcp_operator_ready())

    def test_registered_server_operator_is_ready(self) -> None:
        scene_chat.bpy.types.BLENDERMCP_OT_StartServer = object()
        try:
            self.assertTrue(scene_chat.mcp_operator_ready())
        finally:
            del scene_chat.bpy.types.BLENDERMCP_OT_StartServer

    def test_undo_point_is_set_before_the_busy_flag(self) -> None:
        source = Path(scene_chat.__file__).read_text(encoding="utf-8")
        send = source.split("class DWGREADER_OT_chat_send", 1)[1].split("class DWGREADER_OT_chat_stop", 1)[0]
        self.assertLess(send.find("scene_changes.arm(scene)"), send.find("dwgreader_chat_busy = True"))
        self.assertIn("def release_turn", source)

    def test_chat_turn_restores_the_selection_outline(self) -> None:
        source = Path(scene_chat.__file__).read_text(encoding="utf-8")
        chat = (ADDON / "chat_view.py").read_text(encoding="utf-8")
        viewport = (ADDON / "build_scene.py").read_text(encoding="utf-8")
        self.assertIn("def _restore_work_view", source)
        self.assertIn("_restore_work_view()", source)
        self.assertIn("overlay.show_outline_selected = True", source)
        self.assertIn("shading.show_object_outline = True", source)
        self.assertNotIn("def _quiet_space", chat)
        self.assertNotIn("overlay.show_overlays = False", chat)
        self.assertIn("space.shading.show_object_outline = True", viewport)
        self.assertNotIn("space.shading.show_object_outline = False", viewport)


class _Scene:
    def __init__(self) -> None:
        self.dwgreader_chat_log = "[]"
        self.dwgreader_chat_id = ""
        self.dwgreader_chat_busy = False


class ChatHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(scene_chat.chatgpt_auth, "storage_dir", return_value=Path(self.tmp.name))
        self.patcher.start()
        self.clock = 1_000.0
        self.time_patch = patch.object(scene_chat.time, "time", self._now)
        self.time_patch.start()

    def tearDown(self) -> None:
        self.time_patch.stop()
        self.patcher.stop()
        self.tmp.cleanup()

    def _now(self) -> float:
        self.clock += 1.0
        return self.clock

    def test_saved_chat_keeps_the_first_user_line_as_its_title(self) -> None:
        scene = _Scene()
        scene_chat.save_log(scene, [
            {"role": "assistant", "text": "Сначала ответ"},
            {"role": "user", "text": "Поставь лавки вдоль дорожки и добавь деревья у площадки"},
            {"role": "user", "text": "Второе сообщение"},
        ])
        loaded = scene_chat.load_chat(scene.dwgreader_chat_id)
        assert loaded is not None
        self.assertEqual(loaded[1]["text"], "Поставь лавки вдоль дорожки и добавь деревья у площадки")
        title = scene_chat.list_chats()[0]["title"]
        self.assertTrue(title.startswith("Поставь лавки вдоль дорожки"))
        self.assertNotIn("Второе", title)
        self.assertLessEqual(len(title), 42)
        self.assertTrue(title.endswith("…"))

    def test_picture_without_text_is_titled_snapshot(self) -> None:
        scene = _Scene()
        scene_chat.save_log(scene, [{"role": "user", "text": "", "images": ["shot.png"]}])
        self.assertEqual(scene_chat.list_chats()[0]["title"], "Снимок")

    def test_empty_log_does_not_erase_the_saved_chat(self) -> None:
        scene = _Scene()
        scene_chat.save_log(scene, [{"role": "user", "text": "Первый"}])
        saved_id = scene.dwgreader_chat_id
        scene_chat.save_log(scene, [])
        self.assertEqual(scene.dwgreader_chat_log, "[]")
        self.assertEqual(scene.dwgreader_chat_id, saved_id)
        loaded = scene_chat.load_chat(saved_id)
        assert loaded is not None
        self.assertEqual(loaded[0]["text"], "Первый")

    def test_new_chat_keeps_the_previous_one(self) -> None:
        scene = _Scene()
        scene_chat.save_log(scene, [{"role": "user", "text": "Первый"}])
        previous = scene.dwgreader_chat_id
        scene_chat.begin_chat(scene)
        self.assertNotEqual(scene.dwgreader_chat_id, previous)
        self.assertEqual(json.loads(scene.dwgreader_chat_log), [])
        scene_chat.save_log(scene, [{"role": "user", "text": "Второй"}])
        self.assertEqual(scene_chat.load_chat(previous)[0]["text"], "Первый")
        self.assertEqual(scene_chat.load_chat(scene.dwgreader_chat_id)[0]["text"], "Второй")
        self.assertEqual([row["title"] for row in scene_chat.list_chats()], ["Второй", "Первый"])

    def test_opening_a_saved_chat_replaces_the_scene_buffer(self) -> None:
        scene = _Scene()
        scene_chat.save_log(scene, [{"role": "user", "text": "Первый"}])
        first = scene.dwgreader_chat_id
        scene_chat.begin_chat(scene)
        scene_chat.save_log(scene, [{"role": "user", "text": "Второй"}])
        self.assertTrue(scene_chat.use_chat(scene, first))
        self.assertEqual(scene.dwgreader_chat_id, first)
        self.assertEqual(json.loads(scene.dwgreader_chat_log)[0]["text"], "Первый")
        second = next(row for row in scene_chat.list_chats() if row["id"] != first)
        self.assertEqual(scene_chat.load_chat(second["id"])[0]["text"], "Второй")

    def test_history_keeps_the_forty_newest_chats(self) -> None:
        scene = _Scene()
        for index in range(45):
            scene.dwgreader_chat_id = f"id-{index}"
            scene_chat.save_log(scene, [{"role": "user", "text": f"Чат {index}"}])
        rows = scene_chat.list_chats()
        self.assertEqual(len(rows), 40)
        ids = {row["id"] for row in rows}
        self.assertNotIn("id-0", ids)
        self.assertIn("id-44", ids)
        self.assertEqual(rows[0]["id"], "id-44")

    def test_delete_removes_the_chat(self) -> None:
        scene = _Scene()
        scene_chat.save_log(scene, [{"role": "user", "text": "Удалить"}])
        chat_id = scene.dwgreader_chat_id
        scene_chat.forget_chat(scene, chat_id)
        self.assertIsNone(scene_chat.load_chat(chat_id))
        self.assertEqual(scene_chat.list_chats(), [])
        self.assertEqual(scene.dwgreader_chat_log, "[]")
        self.assertNotEqual(scene.dwgreader_chat_id, chat_id)


if __name__ == "__main__":
    unittest.main()
