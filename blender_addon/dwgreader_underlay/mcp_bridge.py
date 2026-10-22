"""Клиент сокета MCP for Blender. Без bpy, чтобы это можно было проверить отдельно."""

from __future__ import annotations

import json
import socket
from typing import Any


HOST = "127.0.0.1"
PORT = 9876


class BridgeError(Exception):
    """Сокет MCP не ответил или вернул ошибку."""


def port_open(host: str = HOST, port: int = PORT, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_json(sock: socket.socket, timeout: float) -> dict[str, Any]:
    chunks: list[bytes] = []
    sock.settimeout(timeout)
    while True:
        try:
            chunk = sock.recv(8192)
        except socket.timeout as error:
            if chunks:
                break
            raise BridgeError("MCP не ответил вовремя") from error
        if not chunk:
            if not chunks:
                raise BridgeError("Соединение с MCP закрыто")
            break
        chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            raise BridgeError("MCP вернул не объект")
        return payload
    raise BridgeError("Ответ MCP оборвался")


def send_command(
    command_type: str,
    params: dict[str, Any] | None = None,
    host: str = HOST,
    port: int = PORT,
    timeout: float = 180.0,
) -> Any:
    """Один JSON-запрос и ответ до целого объекта. Формат BlenderMCP."""
    body = json.dumps(
        {"type": command_type, "params": params or {}},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        sock = socket.create_connection((host, port), timeout=min(timeout, 10.0))
    except OSError as error:
        raise BridgeError("Сервер MCP не запущен") from error
    try:
        sock.sendall(body)
        payload = _read_json(sock, timeout)
    finally:
        sock.close()
    if payload.get("status") == "error":
        raise BridgeError(str(payload.get("message") or "Ошибка MCP"))
    return payload.get("result", {})
