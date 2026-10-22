from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"))

from mcp_bridge import BridgeError, port_open, send_command


def _serve(reply: dict) -> tuple[socket.socket, int, threading.Thread]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def run() -> None:
        conn, _addr = server.accept()
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            try:
                json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            else:
                break
        conn.sendall(json.dumps(reply).encode("utf-8"))
        conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return server, port, thread


class BridgeTests(unittest.TestCase):
    def test_scene_info_round_trip(self) -> None:
        server, port, thread = _serve({"status": "success", "result": {"scene": "Test", "selected": ["Газон"]}})
        try:
            result = send_command("get_scene_info", host="127.0.0.1", port=port, timeout=5)
        finally:
            thread.join(timeout=2)
            server.close()
        self.assertEqual(result["scene"], "Test")
        self.assertEqual(result["selected"], ["Газон"])

    def test_screenshot_command_carries_the_filepath(self) -> None:
        seen: dict = {}

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def run() -> None:
            conn, _addr = server.accept()
            data = b""
            while True:
                chunk = conn.recv(4096)
                data += chunk
                try:
                    seen["command"] = json.loads(data.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                else:
                    break
            path = seen["command"]["params"]["filepath"]
            Path(path).write_bytes(b"png-bytes")
            conn.sendall(json.dumps({"status": "success", "result": {"filepath": path}}).encode("utf-8"))
            conn.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            result = send_command(
                "get_viewport_screenshot",
                {"max_size": 64, "filepath": str(Path(__file__).with_name("_shot.png")), "format": "png"},
                port=port,
                timeout=5,
            )
        finally:
            thread.join(timeout=2)
            server.close()
            Path(result["filepath"]).unlink(missing_ok=True)
        self.assertEqual(seen["command"]["type"], "get_viewport_screenshot")
        self.assertEqual(seen["command"]["params"]["max_size"], 64)

    def test_error_status_is_reported(self) -> None:
        server, port, thread = _serve({"status": "error", "message": "нет объекта"})
        try:
            with self.assertRaises(BridgeError) as caught:
                send_command("get_object_info", {"object_name": "нет"}, port=port, timeout=5)
        finally:
            thread.join(timeout=2)
            server.close()
        self.assertIn("нет объекта", str(caught.exception))

    def test_closed_port_is_not_open(self) -> None:
        self.assertFalse(port_open("127.0.0.1", 1, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
