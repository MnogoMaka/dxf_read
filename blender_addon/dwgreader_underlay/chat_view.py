"""Окно чата: отдельное окно Blender, которое можно двигать.

Тип редактора из дополнения не регистрируется. Прямоугольники рисуются
шейдером Loopcut: квадрат 0..1 и уже готовая матрица вида, без своей проекции.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

import blf
import bpy
import gpu
from bpy.props import CollectionProperty, StringProperty
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader


BG = (0x18 / 255, 0x18 / 255, 0x18 / 255, 1.0)
CARD = (0x21 / 255, 0x21 / 255, 0x21 / 255, 1.0)
CARD_BORDER = (0x33 / 255, 0x33 / 255, 0x33 / 255, 1.0)
CODE_BG = (0x14 / 255, 0x14 / 255, 0x14 / 255, 1.0)
INPUT_BG = (0x1F / 255, 0x1F / 255, 0x1F / 255, 1.0)
INPUT_BORDER = (0x3A / 255, 0x3A / 255, 0x3A / 255, 1.0)
ACCENT = (0x4D / 255, 0x9C / 255, 0xF6 / 255, 1.0)
TEXT = (0xEE / 255, 0xEE / 255, 0xEE / 255, 1.0)
TEXT_MUTED = (0xC4 / 255, 0xC4 / 255, 0xC4 / 255, 1.0)
TEXT_FAINT = (0xA0 / 255, 0xA0 / 255, 0xA0 / 255, 1.0)
CODE_TEXT = (0xC5 / 255, 0xC8 / 255, 0xC6 / 255, 1.0)
OK = (0x56 / 255, 0xB9 / 255, 0x7F / 255, 1.0)
WARN = (0xD9 / 255, 0xA4 / 255, 0x41 / 255, 1.0)
ERROR = (0xE5 / 255, 0x64 / 255, 0x6A / 255, 1.0)
ERROR_BG = (0x2A / 255, 0x1B / 255, 0x1C / 255, 1.0)
WHITE = (1.0, 1.0, 1.0, 1.0)
BUTTON = (0x2D / 255, 0x6F / 255, 0xCF / 255, 1.0)
GHOST = (0x2A / 255, 0x2A / 255, 0x2A / 255, 1.0)
CHAT_WIDTH = 420
CHAT_HEIGHT = 700
_MARKER = ".dwgreader.chat"

_VERTEX = """
void main()
{
  vec2 p = rect.xy - vec2(1.0) + pos * (rect.zw + vec2(2.0));
  local = p - (rect.xy + rect.zw * 0.5);
  gl_Position = ModelViewProjectionMatrix * vec4(p, 0.0, 1.0);
}
"""

_FRAGMENT = """
void main()
{
  vec2 half_size = rect.zw * 0.5;
  float r = min(shape.x, min(half_size.x, half_size.y));
  vec2 q = abs(local) - half_size + vec2(r);
  float dist = length(max(q, vec2(0.0))) + min(max(q.x, q.y), 0.0) - r;
  float coverage = 1.0 - smoothstep(-0.5, 0.5, dist);
  float inside = 1.0 - smoothstep(-0.5, 0.5, dist + shape.y);
  vec4 col = mix(border_color, color, shape.y > 0.0 ? inside : 1.0);
  fragColor = vec4(col.rgb, col.a * coverage);
}
"""

_HANDLE = None
_HANDLE_IMAGE = None
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_SHADER = None
_BATCH = None
_HITS: dict[int, list] = {}
_PAINTED = False
_CLIP = {"on": False}
_SHOTS: dict = {}
_FRAME = {"key": None, "laid": None}
_MODAL = {"running": False, "window": 0}
_TOKEN = re.compile(r"\S+\s*|\s+")
_UI = {
    "lines": [""],
    "row": 0,
    "col": 0,
    "focus": True,
    "scroll": 0.0,
    "stick": True,
    "hover": "",
    "max_scroll": 0.0,
    "window": 0,
    "anchor": None,
    "undo": [],
    "open": set(),
    "field": None,
    "photos": [],
    "caret_at": 0.0,
    "changes_open": set(),
    "history": False,
    "history_scroll": 0.0,
    "history_max": 0.0,
}


def split_blocks(text: str) -> list[tuple[str, str]]:
    """Обычный текст и ограждённые блоки кода."""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    code: list[str] = []
    fenced = False
    for line in str(text or "").split("\n"):
        if line.strip().startswith("```"):
            if fenced:
                parts.append(("code", "\n".join(code)))
                code = []
                fenced = False
            else:
                if any(piece.strip() for piece in buf):
                    parts.append(("text", "\n".join(buf).strip("\n")))
                buf = []
                fenced = True
            continue
        (code if fenced else buf).append(line)
    if any(piece.strip() for piece in buf):
        parts.append(("text", "\n".join(buf).strip("\n")))
    if code:
        parts.append(("code", "\n".join(code)))
    return parts or [("text", str(text or ""))]


def _chat():
    from . import scene_chat
    return scene_chat


def _scale(context) -> float:
    system = context.preferences.system
    ui = float(getattr(system, "ui_scale", 1.0) or 1.0)
    pixels = float(getattr(system, "pixel_size", 1.0) or 1.0)
    return max(0.75, ui * pixels)


def _panel(region, scale: float) -> tuple[float, float, float, float]:
    del scale
    return 0.0, 0.0, float(region.width), float(region.height)


def _rect_shader():
    global _SHADER, _BATCH
    if _SHADER is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant("MAT4", "ModelViewProjectionMatrix")
        info.push_constant("VEC4", "rect")
        info.push_constant("VEC4", "color")
        info.push_constant("VEC4", "border_color")
        info.push_constant("VEC4", "shape")
        info.vertex_in(0, "VEC2", "pos")
        interface = gpu.types.GPUStageInterfaceInfo("dwgreader_rect_iface")
        interface.smooth("VEC2", "local")
        info.vertex_out(interface)
        info.fragment_out(0, "VEC4", "fragColor")
        info.vertex_source(_VERTEX)
        info.fragment_source(_FRAGMENT)
        _SHADER = gpu.shader.create_from_info(info)
        _BATCH = batch_for_shader(
            _SHADER,
            "TRIS",
            {"pos": ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))},
            indices=((0, 1, 2), (0, 2, 3)),
        )
    return _SHADER, _BATCH


def _color4(color) -> tuple[float, float, float, float]:
    channels = tuple(float(channel) for channel in color)
    if len(channels) >= 4:
        return channels[:4]
    return channels[0], channels[1], channels[2], 1.0


def _rect(x, y, width, height, radius, color, border: float = 0.0, border_color=None) -> None:
    if width <= 1 or height <= 1:
        return
    shader, batch = _rect_shader()
    matrix = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("ModelViewProjectionMatrix", matrix)
    shader.uniform_float("rect", (float(x), float(y), float(width), float(height)))
    shader.uniform_float("color", _color4(color))
    shader.uniform_float("border_color", _color4(border_color or color))
    shader.uniform_float("shape", (float(radius), float(border), 0.0, 0.0))
    batch.draw(shader)


def _fill(x, y, width, height, radius, color) -> None:
    _rect(x, y, width, height, radius, color)


def _stroke(x, y, width, height, radius, color) -> None:
    _rect(x, y, width, height, radius, (color[0], color[1], color[2], 0.0), 1.0, color)


def _hit(hits: list, name: str, rect: tuple[float, float, float, float]) -> None:
    hits.append({"id": name, "rect": rect})


def _contains(rect, x: float, y: float) -> bool:
    left, bottom, width, height = rect
    return left <= x <= left + width and bottom <= y <= bottom + height


def _font(size: int, color) -> None:
    blf.size(0, max(9, int(size)))
    blf.color(0, color[0], color[1], color[2], color[3] if len(color) > 3 else 1.0)


def _text_width(text: str, size: int) -> float:
    if not text:
        return 0.0
    # При включённой обрезке dimensions возвращает ширину видимого куска, а не строки.
    clipped = _CLIP["on"]
    if clipped:
        blf.disable(0, blf.CLIPPING)
    try:
        _font(size, TEXT)
        measured = float(blf.dimensions(0, text)[0] or 0.0)
    finally:
        if clipped:
            blf.enable(0, blf.CLIPPING)
    if measured <= 1.0:
        return len(text) * float(size) * 0.55
    return measured


def _ellipsize(text: str, width: float, size: int) -> str:
    if not text or _text_width(text, size) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(text[:mid].rstrip() + "…", size) <= width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "…" if low else ""


def _draw_line(text: str, x: float, y: float, size: int, color) -> None:
    if not text:
        return
    _font(size, color)
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _wrap(text: str, width: float, size: int) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            piece = word if not current else f"{current} {word}"
            if current and _text_width(piece, size) > width:
                lines.append(current)
                current = word
                while current and _text_width(current, size) > width:
                    cut = max(1, len(current) - 1)
                    while cut > 1 and _text_width(current[:cut], size) > width:
                        cut -= 1
                    lines.append(current[:cut])
                    current = current[cut:]
            else:
                current = piece
        lines.append(current)
    return lines or [""]


def _wrap_spans(text: str, width: float, size: int) -> list[tuple[int, str]]:
    """Строки переноса и индекс первой буквы, чтобы курсор попадал в нужную строку."""
    lines: list[tuple[int, str]] = []
    width = max(8.0, float(width))
    offset = 0
    for paragraph in str(text).split("\n"):
        start, line = offset, ""
        for token in _TOKEN.findall(paragraph):
            if line and _text_width((line + token).rstrip(), size) > width:
                lines.append((start, line))
                start += len(line)
                line = ""
            while len(token) > 1 and _text_width(token.rstrip() or token[:1], size) > width:
                low, high = 1, len(token) - 1
                while low < high:
                    mid = (low + high + 1) // 2
                    if _text_width(token[:mid], size) <= width:
                        low = mid
                    else:
                        high = mid - 1
                lines.append((start, token[:low]))
                start += low
                token = token[low:]
            line += token
        lines.append((start, line))
        offset += len(paragraph) + 1
    return lines or [(0, "")]


def _input_rows(width: float, size: int) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    for index, line in enumerate(_UI["lines"]):
        spans = _wrap_spans(line, width, size)
        rows.extend((index, start, piece) for start, piece in spans)
    return rows or [(0, 0, "")]


def _visual_index(rows: list[tuple[int, int, str]], src_row: int, src_col: int) -> int:
    chosen = 0
    for index, (line_i, start, piece) in enumerate(rows):
        if line_i < src_row:
            chosen = index
            continue
        if line_i > src_row:
            break
        chosen = index
        end = start + len(piece)
        if src_col < end or not piece:
            return index
        nxt = rows[index + 1] if index + 1 < len(rows) else None
        if src_col == end and nxt and nxt[0] == line_i and nxt[1] == end:
            continue
        if src_col <= end:
            return index
    return chosen


def _line_height(size: int) -> float:
    return size * 1.45


def _blocks_height(text: str, width: float, size: int) -> tuple[list, float]:
    rows = []
    height = 0.0
    step = _line_height(size)
    for kind, body in split_blocks(text):
        wrapped = _wrap(body, width - (16 if kind == "code" else 0), size)
        block_h = max(step, len(wrapped) * step + (8 if kind == "code" else 0))
        rows.append((kind, wrapped, block_h))
        height += block_h + 4
    return rows, max(step, height)


def _error_text(text: str) -> bool:
    opening = str(text or "").strip()
    return opening.startswith((
        "Отменено",
        "Лимит",
        "Нет соединения",
        "Сессия",
        "ChatGPT ответил",
        "Модель ничего",
    ))


def _tool_summary(name: str, output: str) -> str:
    line = ""
    for raw in str(output or "").splitlines():
        piece = raw.strip()
        if piece:
            line = piece
            break
    if not line or line == name:
        return name
    return line[:72] + ("…" if len(line) > 72 else "")


def _tool_code(arguments: str) -> str:
    raw = str(arguments or "").strip()
    if not raw or raw == "{}":
        return ""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return raw


def _tool_failed(output: str) -> bool:
    opening = str(output or "").lstrip()
    return _error_text(opening) or opening.startswith(("Error", "Traceback", "Ошибка"))


def _items(context) -> list[dict]:
    chat = _chat()
    scene = context.scene
    rows = []
    for index, item in enumerate(chat.load_log(scene)[-40:]):
        role = item.get("role")
        if role == "tool":
            name = str(item.get("name") or item.get("text") or "Инструмент")
            output = str(item.get("output") or "")
            rows.append({
                "kind": "tool",
                "key": str(item.get("call_id") or f"{index}:{name}"),
                "name": name,
                "summary": _tool_summary(name, output),
                "code": _tool_code(str(item.get("arguments") or "")),
                "output": output,
                "image": str(item.get("image") or ""),
                "failed": _tool_failed(output),
            })
            continue
        if role == "changes":
            rows.append({
                "kind": "changes",
                "text": str(item.get("text") or ""),
                "lines": [str(line) for line in (item.get("lines") or [])],
                "checkpoint": str(item.get("checkpoint") or ""),
                "resolved": str(item.get("resolved") or ""),
            })
            continue
        if role == "assistant" and not item.get("text") and not item.get("summary") and not item.get("image"):
            continue
        pictures = item.get("images") or []
        if isinstance(pictures, str):
            pictures = [pictures]
        rows.append({
            "kind": "user" if role == "user" else "assistant",
            "text": str(item.get("text") or ""),
            "summary": str(item.get("summary") or ""),
            "image": str(item.get("image") or ""),
            "images": [str(path) for path in pictures if path],
        })
    if getattr(scene, "dwgreader_chat_busy", False):
        live = chat._LIVE
        tool = str(live.get("tool") or "")
        summary = str(live.get("summary") or "")
        partial = str(live.get("text") or "")
        if tool:
            rows.append({
                "kind": "tool",
                "key": "live",
                "name": tool,
                "summary": tool,
                "code": "",
                "output": "",
                "image": "",
                "live": True,
                "failed": False,
            })
        if partial or summary or not tool:
            rows.append({
                "kind": "live",
                "text": partial,
                "summary": summary,
                "thinking": not tool and not partial and not summary,
            })
    return rows


def _laid_rows(context, inner: float, size: int, scale: float) -> list[dict]:
    """Вёрстка кэшируется, пока не изменились журнал, черновик или ширина окна."""
    scene = context.scene
    live = ""
    try:
        live = repr(_chat()._LIVE)
    except Exception:
        live = ""
    key = (
        str(getattr(scene, "dwgreader_chat_log", "") or ""),
        bool(getattr(scene, "dwgreader_chat_busy", False)),
        live,
        int(inner),
        int(size),
        tuple(sorted(_UI.get("open") or ())),
        tuple(sorted(_UI.get("changes_open") or ())),
    )
    if _FRAME.get("key") == key and _FRAME.get("laid") is not None:
        return _FRAME["laid"]
    laid = _measure(_items(context), inner, size)
    small = max(10, size - 2)
    for row in laid:
        if row.get("kind") != "tool":
            continue
        summary = str(row.get("summary") or row.get("name") or "")
        name = str(row.get("name") or "")
        show_name = name.strip().casefold() != summary.strip().casefold()
        name_w = _text_width(name, small) if show_name else 0.0
        label_w = max(20.0, row["width"] - 36 * scale - (name_w + 16 * scale if show_name else 0))
        row["fit_label"] = _ellipsize(summary, label_w, size)
        row["fit_name"] = name if show_name else ""
        row["fit_name_w"] = name_w
    _FRAME["key"] = key
    _FRAME["laid"] = laid
    return laid


def _measure(rows: list[dict], inner: float, size: int) -> list[dict]:
    laid = []
    cursor = 0.0
    for row in rows:
        if row["kind"] == "changes":
            lines = row.get("lines") or []
            opened = row.get("checkpoint") in _UI["changes_open"]
            shown = lines if opened or len(lines) <= 6 else lines[:6]
            step = _line_height(max(10, size - 2))
            extra = step if len(lines) > 6 else 0.0
            buttons = 32.0 if not row.get("resolved") and _changes_live(str(row.get("checkpoint") or "")) else 0.0
            height = 16 + _line_height(size) + 6 + len(shown) * step + extra + buttons
            laid.append({
                **row,
                "top": cursor,
                "height": height,
                "width": inner,
                "shown": shown,
                "hidden": max(0, len(lines) - len(shown)),
            })
            cursor += height + 10
            continue
        if row["kind"] == "tool":
            head = _line_height(size) + 16
            extra = 0.0
            code_rows, out_rows = [], []
            if row.get("key") in _UI["open"] and not row.get("live"):
                code_text = "\n".join(str(row.get("code") or "").splitlines()[:12])
                code_rows, code_h = _blocks_height(code_text, inner - 28, max(10, size - 2)) if code_text else ([], 0.0)
                out_rows, out_h = _blocks_height((row.get("output") or "")[:900], inner - 28, max(10, size - 2)) if row.get("output") else ([], 0.0)
                image_h = 140.0 if row.get("image") and os.path.isfile(row["image"]) else 0.0
                extra = code_h + out_h + image_h + (8 if code_h or out_h or image_h else 0)
            else:
                code_h = out_h = 0.0
            laid.append({
                **row,
                "top": cursor,
                "height": head + extra,
                "width": inner,
                "head": head,
                "code_rows": code_rows,
                "out_rows": out_rows,
            })
            cursor += head + extra + 8
            continue
        summary = row.get("summary") or ""
        body = row.get("text") or ""
        if row.get("thinking"):
            body = "Думаю"
        card = row["kind"] == "user"
        text_w = max(40.0, inner - (20 if card else 4))
        pictures = [path for path in (row.get("images") or []) if os.path.isfile(path)]
        if not pictures and row.get("image") and os.path.isfile(str(row.get("image"))):
            pictures = [str(row["image"])]
        summary_rows, summary_h = _blocks_height(summary, text_w, size - 2) if summary else ([], 0.0)
        blank = not str(body).strip() and bool(pictures) and not row.get("thinking")
        text_rows, text_h = ([], 0.0) if blank else _blocks_height(body or " ", text_w, size)
        shot_boxes = _shot_boxes(pictures[:4], text_w, max(96.0, float(size) * 9))
        image_h = (max(box[2] for box in shot_boxes) + 8) if shot_boxes else 0
        pad = 16 if card else 2
        height = pad + summary_h + text_h + image_h
        laid.append({
            **row,
            "top": cursor,
            "height": height,
            "width": inner,
            "summary_rows": summary_rows,
            "text_rows": text_rows,
            "shot_boxes": shot_boxes,
        })
        cursor += height + 10
    return laid


def _draw_lines(rows, x: float, top: float, width: float, size: int, color) -> None:
    step = _line_height(size)
    y = top
    for kind, lines, block_h in rows:
        if kind == "code":
            _fill(x - 4, y - block_h, max(12.0, width - 8), block_h, 4, CODE_BG)
        y -= 4
        ink = CODE_TEXT if kind == "code" else color
        for line in lines:
            y -= step
            _font(size, ink)
            blf.position(0, x, y + 3, 0)
            blf.draw(0, line)


def _draw_spinner(cx: float, cy: float, radius: float, color) -> None:
    """Короткая дуга точек. Угол от времени, поэтому она крутится, пока чат занят."""
    count = 8
    phase = time.monotonic() * 5.0
    radius = max(4.0, float(radius))
    dot = max(1.6, radius * 0.34)
    orbit = max(dot, radius - dot)
    for index in range(count):
        angle = phase - index * (2.0 * math.pi / count)
        fade = 1.0 - index / count
        ink = tuple(color[channel] * (0.22 + 0.78 * fade) for channel in range(3)) + (1.0,)
        _fill(
            cx + math.cos(angle) * orbit - dot,
            cy + math.sin(angle) * orbit - dot,
            dot * 2.0,
            dot * 2.0,
            dot,
            ink,
        )


def _draw_shimmer(text: str, x: float, y: float, size: int) -> None:
    now = time.time()
    sweep = (now % 1.8) / 1.8 * 1.6 - 0.3
    cursor = x
    last = max(1, len(text) - 1)
    for index, char in enumerate(text):
        near = max(0.0, 1.0 - abs(index / last - sweep) / 0.3)
        color = tuple(TEXT_FAINT[channel] + (TEXT[channel] - TEXT_FAINT[channel]) * near for channel in range(3)) + (1.0,)
        _font(size, color)
        blf.position(0, cursor, y, 0)
        blf.draw(0, char)
        cursor += _text_width(char, size)
    pulse = 0.5 - 0.5 * math.cos(2 * math.pi * (now % 0.6) / 0.6)
    dots = tuple(TEXT_FAINT[channel] + (TEXT[channel] - TEXT_FAINT[channel]) * pulse for channel in range(3)) + (1.0,)
    _font(size, dots)
    blf.position(0, cursor, y, 0)
    blf.draw(0, "…")


def _fit_box(src_w: float, src_h: float, max_w: float, max_h: float) -> tuple[float, float]:
    """Вписывает картинку в рамку, не меняя пропорции."""
    src_w = max(1.0, float(src_w))
    src_h = max(1.0, float(src_h))
    max_w = max(1.0, float(max_w))
    max_h = max(1.0, float(max_h))
    factor = min(max_w / src_w, max_h / src_h)
    return src_w * factor, src_h * factor


def _shot_info(path: str):
    cached = _SHOTS.get(path)
    if cached is False:
        return None
    if isinstance(cached, tuple):
        return cached
    if not path or not os.path.isfile(path):
        return None
    try:
        image = bpy.data.images.load(path, check_existing=True)
        texture = gpu.texture.from_image(image)
        width, height = int(image.size[0]), int(image.size[1])
        info = (texture, max(1, width), max(1, height))
        _SHOTS[path] = info
        return info
    except Exception:
        _SHOTS[path] = False
        return None


def _shot_boxes(paths: list, max_w: float, max_h: float) -> list:
    boxes = []
    for path in paths:
        info = _shot_info(path)
        src_w, src_h = (info[1], info[2]) if info else (1, 1)
        width, height = _fit_box(src_w, src_h, max_w, max_h)
        boxes.append([path, width, height])
    if len(boxes) > 1:
        gap = 6.0 * max(0, len(boxes) - 1)
        total = sum(box[1] for box in boxes) + gap
        if total > max_w:
            factor = max_w / total
            for box in boxes:
                box[1] *= factor
                box[2] *= factor
    return [(path, width, height) for path, width, height in boxes]


def _draw_shot(path: str, x: float, y: float, width: float, height: float) -> None:
    info = _shot_info(path)
    if not info:
        return
    try:
        from gpu_extras.presets import draw_texture_2d
        gpu.state.blend_set("ALPHA")
        draw_texture_2d(info[0], (x, y), width, height)
    except Exception:
        _SHOTS[path] = False


def _changes_live(checkpoint: str) -> bool:
    if not checkpoint:
        return False
    from . import scene_changes
    return scene_changes.has_checkpoint(checkpoint)


def _draw_clip(x: float, y: float, box: float, color) -> None:
    """Тонкая скрепка с наклоном, как в обычном чате. В шрифте Blender значка нет."""
    angle = math.radians(-28)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    cx = x + box * 0.50
    cy = y + box * 0.48
    t = max(1.5, box * 0.10)

    def put(px: float, py: float) -> None:
        rx = cx + px * cos_a - py * sin_a
        ry = cy + px * sin_a + py * cos_a
        _fill(rx - t * 0.5, ry - t * 0.5, t, t, t * 0.5, color)

    def line(x0: float, y0: float, x1: float, y1: float, n: int) -> None:
        for i in range(n + 1):
            k = i / n
            put(x0 + (x1 - x0) * k, y0 + (y1 - y0) * k)

    def arc(acx: float, acy: float, radius: float, a0: float, a1: float, n: int) -> None:
        for i in range(n + 1):
            a = math.radians(a0 + (a1 - a0) * i / n)
            put(acx + radius * math.cos(a), acy + radius * math.sin(a))

    inner = box * 0.07
    outer = box * 0.18
    top = box * 0.36
    bot = -box * 0.38
    end = -box * 0.06
    line(inner, end, inner, top - inner, 7)
    arc(0.0, top - inner, inner, 0, 180, 8)
    line(-inner, top - inner, -inner, bot + outer, 8)
    line(-inner, bot + outer, -outer, bot + outer, 3)
    arc(0.0, bot + outer, outer, 180, 360, 10)
    line(outer, bot + outer, outer, top - outer, 8)
    arc(0.0, top - outer, outer, 0, 180, 8)
    line(-outer, top - outer, -outer, top - outer - box * 0.16, 4)


def _draw_changes(row: dict, x: float, bottom: float, scale: float, size: int, small: int, hits: list) -> None:
    width = row["width"]
    height = row["height"]
    _fill(x, bottom, width, height, 8 * scale, CARD)
    _stroke(x, bottom, width, height, 8 * scale, CARD_BORDER)
    title_y = bottom + height - _line_height(size) - 8 * scale
    _font(size, TEXT)
    blf.position(0, x + 10 * scale, title_y, 0)
    blf.draw(0, "Изменения сцены")
    count = str(row.get("text") or "")
    count_w = _text_width(count, small)
    _font(small, TEXT_MUTED)
    blf.position(0, x + width - count_w - 10 * scale, title_y + 1 * scale, 0)
    blf.draw(0, count)
    pen = title_y - 4 * scale
    step = _line_height(small)
    for line in row.get("shown") or []:
        pen -= step
        sign = line.lstrip()[:1]
        ink = OK if sign == "+" else ERROR if sign == "-" else TEXT_MUTED
        _font(small, ink)
        blf.position(0, x + 10 * scale, pen, 0)
        blf.draw(0, line[:80])
    hidden = int(row.get("hidden") or 0)
    checkpoint = str(row.get("checkpoint") or "")
    if hidden or (checkpoint in _UI["changes_open"] and hidden == 0 and len(row.get("lines") or []) > 6):
        pen -= step
        label = "Свернуть" if checkpoint in _UI["changes_open"] else f"Показать ещё {hidden}"
        _font(small, ACCENT)
        blf.position(0, x + 24 * scale, pen, 0)
        blf.draw(0, label)
        _hit(hits, f"more:{checkpoint}", (x + 10 * scale, pen - 2 * scale, width - 20 * scale, step))
    if not row.get("resolved") and _changes_live(checkpoint):
        button_y = bottom + 8 * scale
        button_h = 22 * scale
        keep = "Оставить"
        undo = "Откатить"
        keep_w = _text_width(keep, small) + 18 * scale
        undo_w = _text_width(undo, small) + 18 * scale
        _fill(x + 10 * scale, button_y, keep_w, button_h, 6 * scale, BUTTON)
        _font(small, WHITE)
        blf.position(0, x + 10 * scale + 9 * scale, button_y + 5 * scale, 0)
        blf.draw(0, keep)
        _hit(hits, f"keep:{checkpoint}", (x + 10 * scale, button_y, keep_w, button_h))
        undo_x = x + 10 * scale + keep_w + 8 * scale
        _fill(undo_x, button_y, undo_w, button_h, 6 * scale, GHOST)
        _font(small, TEXT)
        blf.position(0, undo_x + 9 * scale, button_y + 5 * scale, 0)
        blf.draw(0, undo)
        _hit(hits, f"undo:{checkpoint}", (undo_x, button_y, undo_w, button_h))


def _use_saved(context, chat_id: str) -> None:
    if _chat_busy(context):
        return
    if not _chat().use_chat(context.scene, chat_id):
        return
    _show_thread(context)


def _forget_saved(context, chat_id: str) -> None:
    if _chat_busy(context):
        return
    active = str(getattr(context.scene, "dwgreader_chat_id", "") or "")
    _chat().forget_chat(context.scene, chat_id)
    _FRAME["key"] = None
    if active != chat_id:
        return
    _UI["open"] = set()
    _UI["changes_open"] = set()
    _UI["stick"] = True
    _UI["scroll"] = 0.0


def _paint_history(context, area, scale: float, size: int, small: int, hits: list) -> None:
    x, y, width, height = area
    _fill(x, y, width, height, 8 * scale, CARD)
    _hit(hits, "history-sheet", area)
    try:
        rows = _chat().list_chats()
        active = str(getattr(context.scene, "dwgreader_chat_id", "") or "")
    except Exception:
        rows = []
        active = ""
    busy = _chat_busy(context)
    if not rows:
        _UI["history_max"] = 0.0
        _UI["history_scroll"] = 0.0
        _font(size, TEXT_MUTED)
        label = "Нет сохранённых чатов"
        blf.position(0, x + (width - _text_width(label, size)) * 0.5, y + height * 0.5, 0)
        blf.draw(0, label)
        return
    row_h = 46 * scale
    gap = 4 * scale
    step = row_h + gap
    max_scroll = max(0.0, len(rows) * step - gap - height)
    _UI["history_max"] = max_scroll
    _UI["history_scroll"] = min(max_scroll, max(0.0, float(_UI.get("history_scroll") or 0.0)))
    previous = gpu.state.scissor_get()
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(x), int(y), int(width), int(max(1.0, height)))
    blf.enable(0, blf.CLIPPING)
    _CLIP["on"] = True
    blf.clipping(0, x, y, x + width, y + height)
    try:
        for index, row in enumerate(rows):
            bottom = y + height - (index + 1) * step + _UI["history_scroll"]
            top = bottom + row_h
            if top < y or bottom > y + height:
                continue
            chat_id = str(row.get("id") or "")
            chosen = chat_id == active
            hovered = _UI["hover"] in {f"hist:{chat_id}", f"forget:{chat_id}"}
            if chosen or hovered:
                _fill(x + 4 * scale, bottom, width - 8 * scale, row_h, 6 * scale, GHOST)
            if chosen:
                _fill(x + 6 * scale, bottom + 8 * scale, 2 * scale, row_h - 16 * scale, 1 * scale, ACCENT)
            title = _ellipsize(str(row.get("title") or "Чат"), max(24.0, width - 52 * scale), size)
            when = _chat_when(float(row.get("updated") or 0))
            _font(size, TEXT)
            blf.position(0, x + 16 * scale, bottom + row_h - _line_height(size) - 6 * scale, 0)
            blf.draw(0, title)
            if when:
                _font(small, TEXT_FAINT)
                blf.position(0, x + 16 * scale, bottom + 6 * scale, 0)
                blf.draw(0, when)
            _hit(hits, f"hist:{chat_id}", (x + 4 * scale, bottom, width - 8 * scale, row_h))
            if hovered and not busy:
                knob = 16 * scale
                kx = x + width - knob - 10 * scale
                ky = bottom + (row_h - knob) * 0.5
                _font(size, TEXT_MUTED)
                blf.position(0, kx + 3 * scale, ky + 1 * scale, 0)
                blf.draw(0, "×")
                _hit(hits, f"forget:{chat_id}", (kx - 4 * scale, ky - 4 * scale, knob + 8 * scale, knob + 8 * scale))
    finally:
        blf.disable(0, blf.CLIPPING)
        _CLIP["on"] = False
        gpu.state.scissor_set(previous[0], previous[1], previous[2], previous[3])
        gpu.state.scissor_test_set(False)


def _version_label() -> str:
    module = sys.modules.get(__package__ or "")
    parts = getattr(module, "bl_info", {}).get("version", ())
    return ".".join(str(part) for part in parts)


def _model_label(context) -> str:
    try:
        addon = context.preferences.addons.get(__package__)
        prefs = addon.preferences if addon is not None else None
        return _chat().model_label(prefs)
    except Exception:
        return "ChatGPT"


def _draw_history_icon(x: float, y: float, box: float, color) -> None:
    """Три полоски. В шрифте Blender нет значка списка."""
    bar_w = box * 0.56
    bar_h = max(1.5, box * 0.09)
    left = x + (box - bar_w) * 0.5
    gap = (box - 3 * bar_h) / 4
    for index in range(3):
        bar_y = y + gap + index * (bar_h + gap)
        _fill(left, bar_y, bar_w, bar_h, bar_h * 0.5, color)


def _chat_busy(context) -> bool:
    scene = getattr(context, "scene", None)
    return bool(scene is not None and getattr(scene, "dwgreader_chat_busy", False))


def _chat_when(stamp: float) -> str:
    try:
        moment = time.localtime(float(stamp))
        now = time.localtime()
    except (OverflowError, OSError, TypeError, ValueError):
        return ""
    if (moment.tm_year, moment.tm_yday) == (now.tm_year, now.tm_yday):
        return "сегодня"
    yesterday = time.localtime(time.time() - 86400)
    if (moment.tm_year, moment.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return "вчера"
    months = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
    return f"{moment.tm_mday} {months[moment.tm_mon - 1]}"


def _show_thread(context) -> None:
    _UI["history"] = False
    _UI["open"] = set()
    _UI["changes_open"] = set()
    _UI["stick"] = True
    _UI["scroll"] = 0.0
    _FRAME["key"] = None
    if context.area is not None:
        context.area.tag_redraw()


def _clear_log(context) -> None:
    if _chat_busy(context):
        return
    chat = _chat()
    chat.begin_chat(context.scene)
    _UI["lines"] = [""]
    _UI["row"] = 0
    _UI["col"] = 0
    _UI["anchor"] = None
    _UI["undo"] = []
    _UI["undo_mark"] = None
    _UI["photos"] = []
    _show_thread(context)


def _paint(context) -> None:
    region = context.region
    scale = _scale(context)
    x, y, width, height = _panel(region, scale)
    size = int(13 * scale)
    small = max(10, size - 2)
    pad = 12 * scale
    hits: list = []
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    try:
        _fill(x, y, width, height, 0, BG)

        header_h = 34 * scale
        header_y = y + height - header_h
        _fill(x, header_y, width, 1, 0, CARD_BORDER)
        _font(size, TEXT)
        title_x = x + pad
        blf.position(0, title_x, header_y + 10 * scale, 0)
        blf.draw(0, "Чат")
        version = _version_label()
        version_x = title_x + _text_width("Чат", size) + 8 * scale
        _font(small, TEXT_MUTED)
        blf.position(0, version_x, header_y + 11 * scale, 0)
        blf.draw(0, version)

        chat = _chat()
        mcp_open = bool(_UI.get("mcp"))
        dot = 8 * scale
        dot_x = version_x + _text_width(version, small) + 8 * scale
        dot_y = header_y + (header_h - dot) * 0.5
        _fill(dot_x, dot_y, dot, dot, dot * 0.5, OK if mcp_open else WARN)
        if not mcp_open:
            _hit(hits, "mcp", (dot_x - 4 * scale, header_y, dot + 8 * scale, header_h))

        box = 24 * scale
        gap = 2 * scale
        right = x + width - pad
        for hit_id, glyph in (("close", "×"), ("new", "+"), ("history", "")):
            left = right - box
            top = header_y + (header_h - box) * 0.5
            active = hit_id == "history" and bool(_UI.get("history"))
            if _UI["hover"] == hit_id or active:
                _fill(left, top, box, box, 5 * scale, GHOST)
            ink = TEXT if _UI["hover"] == hit_id or active else TEXT_MUTED
            if hit_id == "history":
                _draw_history_icon(left, top, box, ink)
            else:
                _font(size, ink)
                glyph_w = _text_width(glyph, size)
                blf.position(0, left + (box - glyph_w) * 0.5, header_y + 9 * scale, 0)
                blf.draw(0, glyph)
            _hit(hits, hit_id, (left, header_y, box, header_h))
            right = left - gap

        hint = ""
        try:
            addon = context.preferences.addons.get(__package__)
            prefs = addon.preferences if addon is not None else None
            hint = chat.login_hint(prefs)
        except Exception:
            hint = ""
        banner_h = 0.0
        if hint:
            banner_h = 28 * scale
            banner = (x, header_y - banner_h, width, banner_h)
            _fill(*banner, 0, ERROR_BG)
            _font(small, ERROR)
            blf.position(0, x + pad, banner[1] + 7 * scale, 0)
            blf.draw(0, hint[:80])

        typing = any(piece.strip() for piece in _UI["lines"])
        inner_pad = 10 * scale
        clip = 26 * scale
        inner_w = max(40.0, width - 2 * pad - 2 * inner_pad - clip - 8 * scale)
        line_h = _line_height(size)
        rows = _input_rows(inner_w, size) if typing else []
        visible_cap = 6
        caret_i = _visual_index(rows, _UI["row"], _UI["col"]) if rows else 0
        first = int(_UI.get("visual_first") or 0)
        if rows:
            if caret_i < first:
                first = caret_i
            elif caret_i >= first + visible_cap:
                first = caret_i - visible_cap + 1
            first = max(0, min(first, max(0, len(rows) - visible_cap)))
        _UI["visual"] = rows
        _UI["visual_first"] = first
        shown = rows[first:first + visible_cap] if rows else []
        footer_h = 26 * scale
        pad_top = 10 * scale
        pad_bottom = 8 * scale
        gap = 10 * scale
        photos = [path for path in (_UI.get("photos") or []) if os.path.isfile(path)]
        _UI["photos"] = photos
        photo_h = 44 * scale if photos else 0.0
        text_rows = max(2, len(shown) if typing else 1)
        input_h = pad_top + text_rows * line_h + gap + photo_h + footer_h + pad_bottom
        input_h = min(input_h, (240 if photos else 190) * scale)
        input_y = y + pad
        field = (x + pad, input_y, width - 2 * pad, input_h)
        border = ACCENT if _UI["focus"] else INPUT_BORDER
        _fill(*field, 8 * scale, INPUT_BG)
        _stroke(*field, 8 * scale, border)
        _hit(hits, "input", field)
        text_left = field[0] + inner_pad
        text_top = field[1] + field[3] - pad_top
        text_bottom = field[1] + pad_bottom + footer_h + gap + photo_h
        _UI["field"] = field
        _UI["input_size"] = size
        _UI["footer_h"] = footer_h
        _UI["scale"] = scale
        _UI["line_h"] = line_h
        _UI["text_top"] = text_top
        _UI["inner_x"] = text_left
        caret_on = bool(_UI["focus"]) and (time.monotonic() - float(_UI.get("caret_at") or 0.0)) % 1.06 < 0.53
        span = _selection_span() if typing else None
        previous = gpu.state.scissor_get()
        gpu.state.scissor_test_set(True)
        gpu.state.scissor_set(int(text_left), int(text_bottom), int(max(1.0, inner_w)), int(max(1.0, text_top - text_bottom)))
        blf.enable(0, blf.CLIPPING)
        _CLIP["on"] = True
        blf.clipping(0, text_left, text_bottom, text_left + inner_w, text_top)
        try:
            if not typing:
                _draw_line("Опишите, что сделать со сценой", text_left, text_top - line_h + 3 * scale, size, TEXT_MUTED)
            else:
                for index, (line_i, start, piece) in enumerate(shown):
                    baseline = text_top - (index + 1) * line_h + 3 * scale
                    if span is not None:
                        start_row, start_col, end_row, end_col = span
                        lo = max((line_i, start), (start_row, start_col))
                        hi = min((line_i, start + len(piece)), (end_row, end_col))
                        if lo < hi:
                            left = text_left + _text_width(piece[:lo[1] - start], size)
                            right = min(text_left + inner_w, text_left + _text_width(piece[:hi[1] - start], size))
                            if right > left:
                                _fill(left, baseline - 1, right - left, line_h - 4, 0, (0x26 / 255, 0x4F / 255, 0x78 / 255, 1.0))
                    _draw_line(piece.rstrip(), text_left, baseline, size, TEXT)
            if caret_on:
                if not typing or not shown:
                    caret_x, baseline = text_left, text_top - line_h + 3 * scale
                else:
                    index = min(max(0, caret_i - first), len(shown) - 1)
                    line_i, start, piece = shown[index]
                    column = min(max(0, _UI["col"] - start), len(piece))
                    caret_x = min(text_left + inner_w - 2 * scale, text_left + _text_width(piece[:column], size))
                    baseline = text_top - (index + 1) * line_h + 3 * scale
                _fill(caret_x, baseline - 1, max(1.5, 1.5 * scale), line_h - 4, 0, ACCENT)
        finally:
            blf.disable(0, blf.CLIPPING)
            _CLIP["on"] = False
            gpu.state.scissor_set(previous[0], previous[1], previous[2], previous[3])
            gpu.state.scissor_test_set(False)

        if photos:
            thumb = 36 * scale
            px = field[0] + 10 * scale
            py = field[1] + pad_bottom + footer_h
            for index, path in enumerate(photos[:6]):
                info = _shot_info(path)
                src_w, src_h = (info[1], info[2]) if info else (1, 1)
                fit_w, fit_h = _fit_box(src_w, src_h, thumb, thumb)
                _draw_shot(path, px + (thumb - fit_w) * 0.5, py + (thumb - fit_h) * 0.5, fit_w, fit_h)
                _fill(px + thumb - 14 * scale, py + thumb - 14 * scale, 14 * scale, 14 * scale, 3 * scale, CARD)
                _font(small, TEXT)
                blf.position(0, px + thumb - 11 * scale, py + thumb - 13 * scale, 0)
                blf.draw(0, "×")
                _hit(hits, f"unphoto:{index}", (px, py, thumb, thumb))
                px += thumb + 6 * scale
        busy = bool(getattr(context.scene, "dwgreader_chat_busy", False))
        footer_y = field[1] + pad_bottom
        action = "Остановить" if busy else "Enter — отправить"
        action_w = _text_width(action, small)
        action_x = field[0] + field[2] - action_w - 10 * scale
        clip_x = field[0] + field[2] - inner_pad - clip
        clip_y = field[1] + field[3] - pad_top - clip
        _draw_clip(clip_x, clip_y, clip, ACCENT if _UI["hover"] == "attach" else TEXT)
        _hit(hits, "attach", (clip_x - 4 * scale, clip_y - 4 * scale, clip + 8 * scale, clip + 8 * scale))
        model = _ellipsize(_model_label(context), max(24.0, action_x - field[0] - 18 * scale), small)
        _font(small, TEXT_MUTED)
        blf.position(0, field[0] + 10 * scale, footer_y, 0)
        blf.draw(0, model)
        _font(small, ERROR if busy else TEXT_MUTED)
        blf.position(0, action_x, footer_y, 0)
        blf.draw(0, action)
        if busy:
            _hit(hits, "stop", (action_x - 4 * scale, field[1], action_w + 14 * scale, footer_h + 4 * scale))

        area_bottom = input_y + input_h + 8 * scale
        area_top = header_y - banner_h - 8 * scale
        area_h = max(20.0, area_top - area_bottom)
        area = (x + pad, area_bottom, width - 2 * pad, area_h)
        if _UI.get("history"):
            _paint_history(context, area, scale, size, small, hits)
        else:
            laid = _laid_rows(context, area[2], size, scale)
            content_h = laid[-1]["top"] + laid[-1]["height"] if laid else 0.0
            max_scroll = max(0.0, content_h - area_h)
            if _UI["stick"]:
                _UI["scroll"] = 0.0
            _UI["scroll"] = min(max_scroll, max(0.0, float(_UI["scroll"])))
            _UI["max_scroll"] = max_scroll
            scroll_top = max_scroll - _UI["scroll"]
            content_top = area_bottom + area_h + scroll_top

            if not laid:
                _font(size, TEXT)
                blf.position(0, area[0], area_bottom + area_h - _line_height(size) - 4 * scale, 0)
                blf.draw(0, "Спросите, что сделать со сценой")
            if laid:
                previous = gpu.state.scissor_get()
                gpu.state.scissor_test_set(True)
                gpu.state.scissor_set(int(area[0]), int(area[1]), int(area[2]), int(area[3]))
                blf.enable(0, blf.CLIPPING)
                _CLIP["on"] = True
                blf.clipping(0, area[0], area[1], area[0] + area[2], area[1] + area[3])
                try:
                    for row in laid:
                        top = content_top - row["top"]
                        bottom = top - row["height"]
                        if top < area_bottom or bottom > area_bottom + area_h:
                            continue
                        if row["kind"] == "changes":
                            _draw_changes(row, area[0], bottom, scale, size, small, hits)
                            continue
                        if row["kind"] == "tool":
                            _stroke(area[0], bottom, row["width"], row["height"], 8 * scale, WARN if row.get("live") else CARD_BORDER)
                            text_y = bottom + row["height"] - _line_height(size) - 6 * scale
                            blf.clipping(0, area[0] + 4 * scale, bottom, area[0] + row["width"] - 6 * scale, bottom + row["height"])
                            if row.get("live"):
                                _draw_spinner(area[0] + 16 * scale, text_y + size * 0.35, 5.5 * scale, WARN)
                            else:
                                glyph, glyph_color = ("✕", ERROR) if row.get("failed") else ("✓", OK)
                                _font(size, glyph_color)
                                blf.position(0, area[0] + 10 * scale, text_y, 0)
                                blf.draw(0, glyph)
                            label = str(row.get("fit_label") or "")
                            name = str(row.get("fit_name") or "")
                            name_w = float(row.get("fit_name_w") or 0.0)
                            _font(size, TEXT_MUTED)
                            blf.position(0, area[0] + 28 * scale, text_y, 0)
                            blf.draw(0, label)
                            if name:
                                _font(small, TEXT_FAINT)
                                blf.position(0, area[0] + row["width"] - name_w - 10 * scale, text_y + 1, 0)
                                blf.draw(0, name)
                            blf.clipping(0, area[0], area[1], area[0] + area[2], area[1] + area[3])
                            _hit(hits, "tool:" + str(row.get("key") or ""), (area[0], bottom + row["height"] - row["head"], row["width"], row["head"]))
                            pen_y = bottom + row["height"] - row["head"] - 4 * scale
                            if row.get("code_rows"):
                                code_h = sum(block[2] + 4 for block in row["code_rows"])
                                _fill(area[0] + 8 * scale, pen_y - code_h, row["width"] - 16 * scale, code_h, 4 * scale, CODE_BG)
                                _draw_lines(row["code_rows"], area[0] + 12 * scale, pen_y, row["width"] - 24, small, CODE_TEXT)
                                pen_y -= sum(block[2] + 4 for block in row["code_rows"])
                            if row.get("out_rows"):
                                _draw_lines(row["out_rows"], area[0] + 12 * scale, pen_y, row["width"] - 24, small, ERROR if row.get("failed") else TEXT_MUTED)
                                pen_y -= sum(block[2] + 4 for block in row["out_rows"])
                            opened = row.get("key") in _UI["open"] and not row.get("live")
                            if opened and row.get("image") and os.path.isfile(row["image"]):
                                info = _shot_info(row["image"])
                                src_w, src_h = (info[1], info[2]) if info else (1, 1)
                                fit_w, fit_h = _fit_box(src_w, src_h, max(12.0, row["width"] - 24), 132)
                                _draw_shot(row["image"], area[0] + 12 * scale, pen_y - fit_h - 8 * scale, fit_w, fit_h)
                            continue
                        card = row["kind"] == "user"
                        error = row["kind"] != "user" and _error_text(row.get("text") or "")
                        if card:
                            _fill(area[0], bottom, row["width"], row["height"], 8 * scale, CARD)
                        elif error:
                            _fill(area[0], bottom, row["width"], row["height"], 8 * scale, ERROR_BG)
                        pen_x = area[0] + (10 * scale if card or error else 0)
                        pen_top = top - (8 * scale if card or error else 0)
                        ink = ERROR if error else TEXT
                        if row["kind"] == "live" and not row.get("text"):
                            ink = TEXT_MUTED
                        if row.get("summary_rows"):
                            _draw_lines(row["summary_rows"], pen_x, pen_top, row["width"] - 20, small, TEXT_MUTED)
                            summary_h = sum(block[2] + 4 for block in row["summary_rows"])
                            pen_top -= summary_h
                        if row.get("thinking"):
                            base = pen_top - _line_height(size)
                            radius = 5.5 * scale
                            _draw_spinner(pen_x + radius, base + size * 0.35, radius, TEXT)
                            _draw_shimmer("Думаю", pen_x + radius * 2 + 8 * scale, base, size)
                        else:
                            _draw_lines(row.get("text_rows") or [], pen_x, pen_top, row["width"] - 20, size, ink)
                            if row.get("text_rows"):
                                pen_top -= sum(block[2] + 4 for block in row["text_rows"])
                        if row.get("shot_boxes"):
                            sx = pen_x
                            for path, fit_w, fit_h in row["shot_boxes"]:
                                _draw_shot(path, sx, pen_top - fit_h - 6, fit_w, fit_h)
                                sx += fit_w + 6
                finally:
                    blf.disable(0, blf.CLIPPING)
                    _CLIP["on"] = False
                    gpu.state.scissor_set(previous[0], previous[1], previous[2], previous[3])
                    gpu.state.scissor_test_set(False)
                if max_scroll > 1:
                    bar_h = max(24 * scale, area_h * area_h / content_h)
                    travel = area_h - bar_h
                    bar_y = area_bottom + travel * (1.0 - _UI["scroll"] / max_scroll)
                    _fill(x + width - 7 * scale, bar_y, 3 * scale, bar_h, 2 * scale, TEXT_FAINT)
        _HITS[region.as_pointer()] = hits
    finally:
        gpu.state.blend_set("NONE")


def _find_window(context, pointer: int):
    if not pointer or context is None:
        return None
    manager = getattr(context, "window_manager", None)
    if manager is None:
        return None
    for window in manager.windows:
        try:
            if window.as_pointer() == pointer:
                return window
        except ReferenceError:
            continue
    return None


def window_open(context) -> bool:
    return _find_window(context, int(_UI.get("window") or 0)) is not None


def _view3d_area(window):
    screen = getattr(window, "screen", None)
    if screen is None:
        return None
    for area in screen.areas:
        if area.type == "VIEW_3D":
            return area
    return None


def _window_region(area):
    if area is None:
        return None
    for region in area.regions:
        if region.type == "WINDOW":
            return region
    return None


def _drop_marker() -> None:
    obj = bpy.data.objects.get(_MARKER)
    if obj is None:
        return
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass


def _chat_area(window):
    """Редактор изображений: при переносе окна сцена площадки не перерисовывается."""
    screen = getattr(window, "screen", None)
    if screen is None:
        return None
    image = None
    view = None
    for area in screen.areas:
        if area.type == "IMAGE_EDITOR" and image is None:
            image = area
        elif area.type == "VIEW_3D" and view is None:
            view = area
    return image or view


_CHROME = (
    "show_region_ui",
    "show_region_toolbar",
    "show_region_header",
    "show_region_tool_header",
    "show_region_hud",
    "show_region_asset_shelf",
    "show_region_channels",
)
_CHROME_REGIONS = {"TOOLS", "TOOL_HEADER", "HEADER", "UI", "HUD", "ASSET_SHELF", "CHANNELS"}
_CHROME_DONE: set[int] = set()


def _hide_editor_flags(area) -> None:
    if area is None or not area.spaces:
        return
    space = area.spaces.active
    if space is None:
        return
    for name in _CHROME:
        try:
            if hasattr(space, name) and getattr(space, name):
                setattr(space, name, False)
        except Exception:
            pass
    image = getattr(space, "image", None) if hasattr(space, "image") else None
    if image is not None:
        try:
            space.image = None
        except Exception:
            pass
    if hasattr(space, "ui_mode") and getattr(space, "ui_mode", "") != "VIEW":
        try:
            space.ui_mode = "VIEW"
        except (TypeError, ValueError):
            pass


def _hide_editor_chrome(window, area) -> None:
    """У редактора изображений свои панели. В чате их быть не должно."""
    _hide_editor_flags(area)
    if area is None or window is None:
        return
    pointer = area.as_pointer()
    if pointer in _CHROME_DONE:
        return
    visible = [
        region for region in area.regions
        if region.type in _CHROME_REGIONS and region.width > 1 and region.height > 1
    ]
    if not visible:
        return
    _CHROME_DONE.add(pointer)
    for region in visible:
        try:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.screen.region_toggle(region_type=region.type)
        except Exception:
            pass


def _make_chat_space(window) -> None:
    _drop_marker()
    area = _view3d_area(window) or _chat_area(window)
    if area is None:
        return
    if area.type != "IMAGE_EDITOR":
        try:
            area.type = "IMAGE_EDITOR"
        except Exception as error:
            print(f"Подложка DWG: лёгкий вид чата: {error}")
            return
    _hide_editor_chrome(window, _chat_area(window) or area)


def _process_hwnds() -> list[int]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = os.getpid()
    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                found.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc(collect), 0)
    return found


def _resize_hwnds(before: list[int], anchor: int) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    fresh = [hwnd for hwnd in _process_hwnds() if hwnd not in set(before)]
    if not fresh:
        return
    hwnd = fresh[-1]
    user32.ShowWindow(hwnd, 9)
    dpi = 96
    try:
        dpi = int(user32.GetDpiForWindow(hwnd) or 96)
    except Exception:
        dpi = 96
    scale = max(1.0, dpi / 96.0)
    width = int(CHAT_WIDTH * scale)
    height = int(CHAT_HEIGHT * scale)
    left, top = 48, 48
    if anchor:
        rect = wintypes.RECT()
        if user32.GetWindowRect(anchor, ctypes.byref(rect)):
            left = max(0, int(rect.right) - width - int(20 * scale))
            top = int(rect.top) + int(64 * scale)
    user32.SetWindowPos(hwnd, 0, left, top, width, height, 0x0004)
    _UI["hwnd"] = int(hwnd)
    try:
        user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.SetWindowTextW.restype = wintypes.BOOL
        user32.SetWindowTextW(hwnd, "Чат")
    except Exception:
        pass


def _any_view3d(context):
    manager = getattr(context, "window_manager", None)
    if manager is None:
        return None, None
    for window in manager.windows:
        area = _view3d_area(window)
        if area is not None:
            return window, area
    return None, None


_PENDING: dict = {}


def _duplicate_area(window, area):
    """area_dupli в Blender 5.2 только через invoke и не из чужого оператора."""
    region = _window_region(area)
    if window is None or area is None or region is None:
        return None
    before_hwnds = _process_hwnds()
    anchor = 0
    if os.name == "nt":
        import ctypes
        anchor = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    before = {item.as_pointer() for item in bpy.context.window_manager.windows}
    screen = window.screen
    try:
        with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
            result = bpy.ops.screen.area_dupli("INVOKE_DEFAULT")
    except Exception as error:
        print(f"Подложка DWG: area_dupli: {error}")
        result = set()
    if "FINISHED" not in result:
        try:
            with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
                result = bpy.ops.wm.window_new("INVOKE_DEFAULT")
        except Exception as error:
            print(f"Подложка DWG: window_new: {error}")
            return None
        if "FINISHED" not in result:
            return None
    created = [item for item in bpy.context.window_manager.windows if item.as_pointer() not in before]
    if not created:
        return None
    chat = created[-1]
    _UI["window"] = chat.as_pointer()
    _make_chat_space(chat)
    _resize_hwnds(before_hwnds, anchor)
    _POLISH.update(before=list(before_hwnds), anchor=anchor, source_area=area, passes=0)
    if not bpy.app.timers.is_registered(_polish_timer):
        bpy.app.timers.register(_polish_timer, first_interval=0.05)
    return chat


_POLISH: dict = {}


def _polish_timer():
    chat = _find_window(bpy.context, int(_UI.get("window") or 0))
    if chat is None:
        return None
    _resize_hwnds(list(_POLISH.get("before") or []), int(_POLISH.get("anchor") or 0))
    _POLISH["passes"] = int(_POLISH.get("passes") or 0) + 1
    if _POLISH["passes"] < 2:
        return 0.2
    return None


def _open_timer():
    create = bool(_PENDING.get("create"))
    source_window = _PENDING.get("source_window")
    source_area = _PENDING.get("source_area")
    target = _PENDING.get("target")
    _PENDING.clear()
    try:
        chat = target if not create else _duplicate_area(source_window, source_area)
        if chat is not None:
            _make_chat_space(chat)
        if chat is None:
            print("Подложка DWG: не удалось открыть окно чата")
            return None
        area = _chat_area(chat)
        region = _window_region(area)
        if area is None or region is None:
            return None
        with bpy.context.temp_override(window=chat, area=area, region=region):
            bpy.ops.dwgreader.chat_view("INVOKE_DEFAULT")
    except Exception as error:
        print(f"Подложка DWG: окно чата: {error}")
    return None


def request_chat_window(context) -> bool:
    """Ставит открытие на следующий тик: из кнопки панели Blender окно не создаёт."""
    existing = _find_window(context, int(_UI.get("window") or 0))
    if existing is not None:
        _PENDING["create"] = False
        _PENDING["target"] = existing
    else:
        area = context.area if getattr(context.area, "type", None) == "VIEW_3D" else None
        window = context.window
        if area is None:
            window, area = _any_view3d(context)
        if window is None or area is None:
            return False
        _PENDING["create"] = True
        _PENDING["source_window"] = window
        _PENDING["source_area"] = area
    if not bpy.app.timers.is_registered(_open_timer):
        bpy.app.timers.register(_open_timer, first_interval=0.01)
    return True


_CLOSE = {"window": 0}


def _close_timer():
    pointer = int(_CLOSE.get("window") or 0)
    _CLOSE["window"] = 0
    window = _find_window(bpy.context, pointer)
    _drop_marker()
    if window is None:
        return None
    try:
        with bpy.context.temp_override(window=window):
            bpy.ops.wm.window_close()
    except Exception:
        pass
    return None


def _schedule_close(context) -> None:
    pointer = int(_UI.get("window") or 0)
    _UI["window"] = 0
    _MODAL["running"] = False
    _MODAL["window"] = 0
    if _find_window(context, pointer) is None:
        return
    _CLOSE["window"] = pointer
    if not bpy.app.timers.is_registered(_close_timer):
        bpy.app.timers.register(_close_timer, first_interval=0.01)


def _draw() -> None:
    global _PAINTED
    context = bpy.context
    if context is None or getattr(context, "region", None) is None or context.region.type != "WINDOW":
        return
    pointer = int(_UI.get("window") or 0)
    if not pointer:
        return
    current = getattr(context, "window", None)
    try:
        if current is None or current.as_pointer() != pointer:
            return
    except ReferenceError:
        return
    try:
        _paint(context)
        _PAINTED = True
    except Exception as error:
        if not _PAINTED:
            _PAINTED = True
            print(f"Подложка DWG: окно чата не нарисовалось: {error}")


def _ui_timer():
    """Курсор и «Думаю». Панели редактора прячет один раз, сцену не трогает."""
    if not _MODAL.get("running"):
        return None
    window = _find_window(bpy.context, int(_UI.get("window") or 0))
    area = _chat_area(window) if window else None
    if area is not None and area.as_pointer() not in _CHROME_DONE:
        _hide_editor_chrome(window, area)
    try:
        _UI["mcp"] = _chat().port_is_open()
    except Exception:
        _UI["mcp"] = False
    scene = getattr(bpy.context, "scene", None)
    busy = bool(scene is not None and getattr(scene, "dwgreader_chat_busy", False))
    focused = bool(_UI.get("focus"))
    if area is not None and busy:
        area.tag_redraw()
        return 1.0 / 30.0
    if area is not None and focused:
        phase = int(time.monotonic() / 0.53) % 2
        if phase != int(_UI.get("blink") or 0):
            _UI["blink"] = phase
            area.tag_redraw()
        return 0.25
    return 0.5


def ensure_draw() -> None:
    global _HANDLE, _HANDLE_IMAGE
    if _HANDLE_IMAGE is None:
        try:
            _HANDLE_IMAGE = bpy.types.SpaceImageEditor.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")
        except Exception:
            _HANDLE_IMAGE = None
    window = _find_window(bpy.context, int(_UI.get("window") or 0))
    area = _chat_area(window) if window else None
    need_3d = area is not None and area.type == "VIEW_3D"
    if need_3d and _HANDLE is None:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")
    if not need_3d and _HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        except Exception:
            pass
        _HANDLE = None


def remove_draw() -> None:
    global _HANDLE, _HANDLE_IMAGE
    if _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        _HANDLE = None
    if _HANDLE_IMAGE is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(_HANDLE_IMAGE, "WINDOW")
        except Exception:
            pass
        _HANDLE_IMAGE = None


def _remember(kind: str) -> None:
    now = time.monotonic()
    last = _UI.get("undo_mark")
    if last and last[0] == kind and now - last[1] < 0.8:
        _UI["undo_mark"] = (kind, now)
        return
    snap = ([line for line in _UI["lines"]], _UI["row"], _UI["col"])
    _UI["undo"].append(snap)
    del _UI["undo"][:-40]
    _UI["undo_mark"] = (kind, now)


def _undo() -> None:
    stack = _UI["undo"]
    if not stack:
        return
    lines, row, col = stack.pop()
    _UI["lines"] = lines
    _UI["row"] = row
    _UI["col"] = col
    _UI["anchor"] = None
    _UI["undo_mark"] = None


def _point(row: int, col: int) -> tuple[int, int]:
    lines = _UI["lines"]
    row = min(max(0, row), max(0, len(lines) - 1))
    return row, min(max(0, col), len(lines[row]))


def _selection_span():
    anchor = _UI.get("anchor")
    if anchor is None:
        return None
    start = _point(*anchor)
    end = _point(_UI["row"], _UI["col"])
    if start == end:
        return None
    return start + end if start <= end else end + start


def _delete_selection() -> bool:
    span = _selection_span()
    if span is None:
        return False
    start_row, start_col, end_row, end_col = span
    lines = _UI["lines"]
    if start_row == end_row:
        lines[start_row] = lines[start_row][:start_col] + lines[start_row][end_col:]
    else:
        lines[start_row] = lines[start_row][:start_col] + lines[end_row][end_col:]
        del lines[start_row + 1:end_row + 1]
    _UI["row"], _UI["col"] = start_row, start_col
    _UI["anchor"] = None
    return True


def _select_word_at_caret() -> None:
    lines = _UI["lines"]
    row, col = _point(_UI["row"], _UI["col"])
    line = lines[row]
    if not line:
        _UI["anchor"] = None
        return
    if col == len(line) or (col > 0 and not line[col].isalnum() and line[col - 1].isalnum()):
        col -= 1
    if line[col].isalnum():
        start = col
        while start > 0 and line[start - 1].isalnum():
            start -= 1
        end = col + 1
        while end < len(line) and line[end].isalnum():
            end += 1
    else:
        start = col
        while start > 0 and not line[start - 1].isalnum():
            start -= 1
        end = col + 1
        while end < len(line) and not line[end].isalnum():
            end += 1
    _UI["anchor"] = (row, start)
    _UI["row"], _UI["col"] = row, end


def _select_all() -> None:
    lines = _UI["lines"]
    _UI["anchor"] = (0, 0)
    _UI["row"] = len(lines) - 1
    _UI["col"] = len(lines[-1])


def _remember_anchor(extend: bool) -> None:
    if extend:
        if _UI.get("anchor") is None:
            _UI["anchor"] = (_UI["row"], _UI["col"])
        return
    _UI["anchor"] = None


def _word(direction: int, extend: bool) -> None:
    _remember_anchor(extend)
    lines = _UI["lines"]
    row, col = _point(_UI["row"], _UI["col"])
    line = lines[row]
    if direction < 0:
        if col == 0 and row > 0:
            row -= 1
            line = lines[row]
            col = len(line)
        while col > 0 and not line[col - 1].isalnum():
            col -= 1
        while col > 0 and line[col - 1].isalnum():
            col -= 1
    else:
        if col >= len(line) and row < len(lines) - 1:
            row += 1
            line = lines[row]
            col = 0
        while col < len(line) and not line[col].isalnum():
            col += 1
        while col < len(line) and line[col].isalnum():
            col += 1
    _UI["row"], _UI["col"] = row, col


def _place_caret(x: float, y: float, extend: bool) -> None:
    _UI["caret_at"] = time.monotonic()
    rows = _UI.get("visual") or []
    if not rows:
        _remember_anchor(extend)
        _UI["row"], _UI["col"] = 0, 0
        return
    _remember_anchor(extend)
    size = int(_UI.get("input_size") or 13)
    step = float(_UI.get("line_h") or _line_height(size))
    text_top = float(_UI.get("text_top") or 0)
    origin = float(_UI.get("inner_x") or 0)
    first = int(_UI.get("visual_first") or 0)
    rel = text_top - y
    offset = int(rel // step) if step > 0 and rel > 0 else 0
    index = min(max(0, first + offset), len(rows) - 1)
    line_i, start, piece = rows[index]
    col = start + len(piece)
    for column in range(len(piece) + 1):
        if origin + _text_width(piece[:column], size) >= x:
            col = start + column
            break
    _UI["row"], _UI["col"] = line_i, min(col, len(_UI["lines"][line_i]))


def _move_visual(delta: int, extend: bool) -> None:
    rows = _UI.get("visual") or []
    if not rows:
        _move(delta, 0, extend)
        return
    _remember_anchor(extend)
    index = _visual_index(rows, _UI["row"], _UI["col"])
    column = _UI["col"] - rows[index][1]
    index = min(max(0, index + delta), len(rows) - 1)
    line_i, start, piece = rows[index]
    _UI["row"] = line_i
    _UI["col"] = start + min(max(0, column), len(piece))


def _insert(text: str) -> None:
    if not text:
        return
    _remember("type" if len(text) == 1 and _selection_span() is None else "paste")
    if not _delete_selection():
        _UI["anchor"] = None
    lines = _UI["lines"]
    row = min(_UI["row"], len(lines) - 1)
    col = min(_UI["col"], len(lines[row]))
    for char in text:
        if char == "\r":
            continue
        if char == "\n":
            if len(lines) >= 8:
                continue
            rest = lines[row][col:]
            lines[row] = lines[row][:col]
            lines.insert(row + 1, rest)
            row += 1
            col = 0
            continue
        lines[row] = lines[row][:col] + char + lines[row][col:]
        col += 1
    _UI["row"] = row
    _UI["col"] = col
    _UI["caret_at"] = time.monotonic()


def _backspace() -> None:
    _remember("delete")
    if _delete_selection():
        return
    _UI["anchor"] = None
    lines = _UI["lines"]
    row = _UI["row"]
    col = _UI["col"]
    if col > 0:
        lines[row] = lines[row][:col - 1] + lines[row][col:]
        _UI["col"] = col - 1
        return
    if row <= 0:
        return
    col = len(lines[row - 1])
    lines[row - 1] += lines[row]
    del lines[row]
    _UI["row"] = row - 1
    _UI["col"] = col


def _move(delta_row: int, delta_col: int, extend: bool = False) -> None:
    _remember_anchor(extend)
    lines = _UI["lines"]
    row = min(max(0, _UI["row"] + delta_row), len(lines) - 1)
    if delta_row:
        _UI["col"] = min(_UI["col"], len(lines[row]))
    else:
        col = _UI["col"] + delta_col
        if col < 0 and row > 0:
            row -= 1
            col = len(lines[row])
        elif col > len(lines[row]) and row < len(lines) - 1:
            row += 1
            col = 0
        _UI["col"] = min(max(0, col), len(lines[row]))
    _UI["row"] = row


def _apply_changes(context, checkpoint: str, mode: str) -> None:
    from . import scene_changes

    if not checkpoint:
        return
    if mode == "undone":
        scene_changes.revert(checkpoint)
    else:
        scene_changes.keep(checkpoint)
    chat = _chat()
    if mode == "undone":
        chat.release_turn()
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return
    try:
        items = chat.load_log(scene)
    except ReferenceError:
        return
    for item in items:
        if item.get("role") == "changes" and str(item.get("checkpoint") or "") == checkpoint:
            item["resolved"] = mode
            if mode == "undone":
                item["text"] = "Откатили"
                item["lines"] = []
    try:
        chat.save_log(scene, items)
    except ReferenceError:
        return
    _FRAME["key"] = None


def _send(context) -> None:
    text = "\n".join(_UI["lines"]).strip()
    photos = [path for path in (_UI.get("photos") or []) if os.path.isfile(path)]
    if (not text and not photos) or context.scene.dwgreader_chat_busy:
        return
    chat = _chat()
    chat.set_outgoing_images(photos)
    block = bpy.data.texts.get(chat.DRAFT_NAME)
    if block is not None:
        block.clear()
    context.scene.dwgreader_chat_draft = text
    result = bpy.ops.dwgreader.chat_send()
    if "FINISHED" not in result:
        chat.set_outgoing_images([])
        return
    _UI["lines"] = [""]
    _UI["row"] = 0
    _UI["col"] = 0
    _UI["anchor"] = None
    _UI["undo"] = []
    _UI["undo_mark"] = None
    _UI["photos"] = []
    _UI["stick"] = True
    _UI["scroll"] = 0.0


def _hover(region, x: float, y: float) -> str:
    found = ""
    for hit in _HITS.get(region.as_pointer(), []):
        if _contains(hit["rect"], x, y):
            found = hit["id"]
    return found


class DWGREADER_OT_chat_view(bpy.types.Operator):
    bl_idname = "dwgreader.chat_view"
    bl_label = "Окно чата"
    bl_description = "Отдельное окно чата. Его можно двигать, основной вид остаётся свободным"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        if context.area is None or context.area.type not in {"VIEW_3D", "IMAGE_EDITOR"}:
            return {"CANCELLED"}
        window = _find_window(context, int(_UI.get("window") or 0))
        here = (
            window is not None
            and context.window is not None
            and context.window.as_pointer() == window.as_pointer()
        )
        if not here:
            if not request_chat_window(context):
                self.report({"WARNING"}, "Не удалось открыть окно чата")
                return {"CANCELLED"}
            return {"FINISHED"}
        owner = context.window.as_pointer()
        if _MODAL["running"] and int(_MODAL.get("window") or 0) == owner:
            if context.area is not None:
                context.area.tag_redraw()
            return {"FINISHED"}
        _MODAL["running"] = True
        _MODAL["window"] = owner
        self._owner = owner
        _UI["focus"] = True
        _UI["caret_at"] = time.monotonic()
        try:
            _UI["mcp"] = _chat().port_is_open()
        except Exception:
            _UI["mcp"] = False
        ensure_draw()
        if not bpy.app.timers.is_registered(_ui_timer):
            bpy.app.timers.register(_ui_timer, first_interval=0.25)
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        pointer = int(_UI.get("window") or 0)
        if context.window is None or context.window.as_pointer() != pointer:
            return {"PASS_THROUGH"}
        region = context.region
        if region is None or region.type != "WINDOW":
            return {"PASS_THROUGH"}
        if int(getattr(self, "_owner", 0) or 0) not in {0, pointer}:
            return self._finish(context, close=False)
        scene = getattr(context, "scene", None)
        scale = _scale(context)
        panel = _panel(region, scale)
        inside = _contains(panel, event.mouse_region_x, event.mouse_region_y)
        if event.type == "TIMER":
            return {"PASS_THROUGH"}
        if event.type == "ESC" and event.value == "PRESS":
            if _UI.get("history"):
                _UI["history"] = False
                if context.area is not None:
                    context.area.tag_redraw()
                return {"RUNNING_MODAL"}
            if _UI["focus"]:
                _UI["focus"] = False
                return {"RUNNING_MODAL"}
        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"} and inside:
            step = 56 * scale
            if _UI.get("history"):
                if event.type == "WHEELUPMOUSE":
                    _UI["history_scroll"] = max(0.0, _UI["history_scroll"] - step)
                else:
                    _UI["history_scroll"] = min(_UI["history_max"], _UI["history_scroll"] + step)
            elif event.type == "WHEELUPMOUSE":
                _UI["scroll"] = min(_UI["max_scroll"], _UI["scroll"] + step)
            else:
                _UI["scroll"] = max(0.0, _UI["scroll"] - step)
            if not _UI.get("history"):
                _UI["stick"] = _UI["scroll"] <= 1
            if context.area is not None:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "MOUSEMOVE":
            if _UI.get("selecting") and _UI["focus"]:
                _place_caret(event.mouse_region_x, event.mouse_region_y, True)
                if context.area is not None:
                    context.area.tag_redraw()
                return {"RUNNING_MODAL"}
            hover = _hover(region, event.mouse_region_x, event.mouse_region_y) if inside else ""
            if hover != _UI["hover"]:
                _UI["hover"] = hover
                if context.area is not None:
                    context.area.tag_redraw()
            return {"PASS_THROUGH"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            _UI["selecting"] = False
            if _selection_span() is None:
                _UI["anchor"] = None
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if not inside:
                _UI["focus"] = False
                return {"RUNNING_MODAL"}
            hover = _hover(region, event.mouse_region_x, event.mouse_region_y)
            sheet = hover == "history-sheet" or hover.startswith(("hist:", "forget:"))
            if not sheet:
                _UI["focus"] = hover in {"", "input", "attach"} or hover.startswith("unphoto:")
            _UI["selecting"] = False
            if hover == "input":
                now = time.monotonic()
                last = _UI.get("click")
                double = bool(last) and now - last[0] < 0.35 and max(abs(event.mouse_region_x - last[1]), abs(event.mouse_region_y - last[2])) < 8
                _UI["click"] = (now, event.mouse_region_x, event.mouse_region_y)
                _place_caret(event.mouse_region_x, event.mouse_region_y, event.shift and not double)
                if double:
                    _select_word_at_caret()
                else:
                    if not event.shift:
                        _UI["anchor"] = (_UI["row"], _UI["col"])
                    _UI["selecting"] = True
            elif hover.startswith("tool:"):
                key = hover.split(":", 1)[1]
                opened = _UI["open"]
                opened.discard(key) if key in opened else opened.add(key)
            if hover == "close":
                if scene is not None:
                    scene.dwgreader_screen = "home"
                return self._finish(context, close=True)
            if hover.startswith("more:"):
                key = hover.split(":", 1)[1]
                opened = _UI["changes_open"]
                opened.discard(key) if key in opened else opened.add(key)
            elif hover.startswith("keep:"):
                _apply_changes(context, hover.split(":", 1)[1], "kept")
            elif hover.startswith("undo:"):
                _apply_changes(context, hover.split(":", 1)[1], "undone")
            if hover == "attach":
                if not bpy.app.timers.is_registered(_photo_timer):
                    bpy.app.timers.register(_photo_timer, first_interval=0.01)
                return self._finish(context, close=False)
            if hover.startswith("unphoto:"):
                try:
                    index = int(hover.split(":", 1)[1])
                except ValueError:
                    index = -1
                photos = _UI.get("photos") or []
                if 0 <= index < len(photos):
                    del photos[index]
            if hover == "history":
                opening = not bool(_UI.get("history"))
                if opening:
                    try:
                        chat = _chat()
                        chat.save_log(context.scene, chat.load_log(context.scene))
                    except Exception:
                        pass
                _UI["history"] = opening
                _UI["history_scroll"] = 0.0
            elif hover == "new":
                if not _chat_busy(context):
                    _clear_log(context)
                    _UI["focus"] = True
            elif hover.startswith("hist:"):
                _use_saved(context, hover.split(":", 1)[1])
            elif hover.startswith("forget:"):
                _forget_saved(context, hover.split(":", 1)[1])
            elif hover == "stop":
                bpy.ops.dwgreader.chat_stop()
            elif hover == "mcp":
                try:
                    if _chat().mcp_operator_ready():
                        bpy.ops.dwgreader.chat_server()
                    else:
                        bpy.ops.dwgreader.chat_install()
                except Exception:
                    pass
            if context.area is not None:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.ctrl and event.type == "V" and event.value == "PRESS":
            pasted = clipboard_image_path()
            if pasted:
                attach_images([pasted])
                _UI["focus"] = True
                if context.area is not None:
                    context.area.tag_redraw()
                return {"RUNNING_MODAL"}
        if not _UI["focus"]:
            return {"PASS_THROUGH"}
        if event.value not in {"PRESS", "REPEAT"}:
            return {"RUNNING_MODAL"}
        if event.value == "REPEAT" and event.type in {"RET", "NUMPAD_ENTER"}:
            return {"RUNNING_MODAL"}
        if event.type == "RET":
            if event.shift:
                _insert("\n")
            else:
                _send(context)
        elif event.ctrl and event.type == "Z" and not event.shift:
            _undo()
        elif event.ctrl and event.type == "A":
            _select_all()
        elif event.ctrl and event.type == "LEFT_ARROW":
            _word(-1, event.shift)
        elif event.ctrl and event.type == "RIGHT_ARROW":
            _word(1, event.shift)
        elif event.type == "BACK_SPACE":
            _backspace()
        elif event.type in {"DEL", "FORWARD_DEL"}:
            if _selection_span() is None:
                _move(0, 1)
            _backspace()
        elif event.type == "LEFT_ARROW":
            _move(0, -1, event.shift)
        elif event.type == "RIGHT_ARROW":
            _move(0, 1, event.shift)
        elif event.type == "UP_ARROW":
            _move_visual(-1, event.shift)
        elif event.type == "DOWN_ARROW":
            _move_visual(1, event.shift)
        elif event.ctrl and event.type == "V":
            _insert(context.window_manager.clipboard or "")
        elif event.unicode and event.unicode.isprintable():
            _insert(event.unicode)
        else:
            return {"RUNNING_MODAL"}
        if context.area is not None:
            context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _finish(self, context, close: bool = False):
        owner = int(getattr(self, "_owner", 0) or 0)
        if int(_MODAL.get("window") or 0) in {0, owner}:
            _MODAL["running"] = False
            _MODAL["window"] = 0
        if close:
            _schedule_close(context)
        if context.area is not None:
            context.area.tag_redraw()
        return {"FINISHED"}

    def cancel(self, context):
        self._finish(context, close=False)


def _png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgba[row * width * 4:(row + 1) * width * 4] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def dib_to_png(data: bytes) -> bytes | None:
    """CF_DIB из буфера Windows в PNG. Берётся каждый пиксель, пока сторона больше 1600."""
    if len(data) < 40:
        return None
    header = int.from_bytes(data[0:4], "little")
    width = int.from_bytes(data[4:8], "little", signed=True)
    height_raw = int.from_bytes(data[8:12], "little", signed=True)
    planes = int.from_bytes(data[12:14], "little")
    bit = int.from_bytes(data[14:16], "little")
    compression = int.from_bytes(data[16:20], "little")
    if width <= 0 or width > 8000 or bit not in (24, 32) or compression not in (0, 3) or planes != 1:
        return None
    height = abs(height_raw)
    if height <= 0 or height > 8000:
        return None
    offset = header + (12 if compression == 3 else 0)
    stride = ((width * bit + 31) // 32) * 4
    if offset + stride * height > len(data):
        return None
    step = 1
    while (width // step) * (height // step) > 1600 * 1600:
        step += 1
    out_w = max(1, width // step)
    out_h = max(1, height // step)
    rgba = bytearray(out_w * out_h * 4)
    channels = bit // 8
    for y in range(out_h):
        src_y = y * step
        row = (height - 1 - src_y) if height_raw > 0 else src_y
        start = offset + row * stride
        dest = y * out_w * 4
        for x in range(out_w):
            src = start + (x * step) * channels
            blue, green, red = data[src], data[src + 1], data[src + 2]
            alpha = data[src + 3] if bit == 32 else 255
            rgba[dest:dest + 4] = bytes((red, green, blue, alpha))
            dest += 4
    return _png_bytes(out_w, out_h, bytes(rgba))


def _write_paste(data: bytes) -> str:
    folder = Path(tempfile.gettempdir()) / "dwgreader_chat"
    folder.mkdir(exist_ok=True)
    path = folder / f"paste_{int(time.time() * 1000)}.png"
    path.write_bytes(data)
    return str(path)


def clipboard_image_path() -> str | None:
    """PNG, растр или файл картинки из буфера. Текст остаётся для обычной вставки."""
    if os.name != "nt":
        return None
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return None
    try:
        for name in ("PNG", "image/png"):
            fmt = user32.RegisterClipboardFormatW(name)
            handle = user32.GetClipboardData(fmt) if fmt else None
            data = _locked_bytes(kernel32, handle)
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return _write_paste(data)
        for fmt in (8, 17):
            png = dib_to_png(_locked_bytes(kernel32, user32.GetClipboardData(fmt)))
            if png:
                return _write_paste(png)
        handle = user32.GetClipboardData(15)
        if handle:
            return _dropped_image(handle)
    finally:
        user32.CloseClipboard()
    return None


def _locked_bytes(kernel32, handle) -> bytes:
    if not handle:
        return b""
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return b""
    try:
        size = int(kernel32.GlobalSize(handle) or 0)
        return ctypes_string(pointer, size) if size else b""
    finally:
        kernel32.GlobalUnlock(handle)


def ctypes_string(pointer, size: int) -> bytes:
    import ctypes
    return ctypes.string_at(pointer, size)


def _dropped_image(handle) -> str | None:
    import ctypes

    shell32 = ctypes.windll.shell32
    count = int(shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0) or 0)
    buffer = ctypes.create_unicode_buffer(520)
    for index in range(count):
        shell32.DragQueryFileW(handle, index, buffer, 520)
        path = buffer.value
        if os.path.splitext(path)[1].lower() in _IMAGE_EXT and os.path.isfile(path):
            return path
    return None


def attach_images(paths) -> None:
    photos = _UI.setdefault("photos", [])
    for raw in paths:
        path = os.path.abspath(str(raw))
        if os.path.splitext(path)[1].lower() not in _IMAGE_EXT or not os.path.isfile(path):
            continue
        if path not in photos and len(photos) < 6:
            photos.append(path)
    _UI["caret_at"] = time.monotonic()


def _photo_timer():
    try:
        bpy.ops.dwgreader.chat_attach("INVOKE_DEFAULT")
    except Exception as error:
        print(f"Подложка DWG: выбор фото: {error}")
        _restore_chat_modal()
    return None


def _restore_chat_modal() -> None:
    if not bpy.app.timers.is_registered(_restore_timer):
        bpy.app.timers.register(_restore_timer, first_interval=0.05)


def _restore_timer():
    window = _find_window(bpy.context, int(_UI.get("window") or 0))
    area = _chat_area(window) if window else None
    region = _window_region(area)
    if window is None or area is None or region is None:
        return None
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.dwgreader.chat_view("INVOKE_DEFAULT")
    except Exception as error:
        print(f"Подложка DWG: вернуть ввод чата: {error}")
    return None


class DWGREADER_OT_chat_attach(Operator):
    bl_idname = "dwgreader.chat_attach"
    bl_label = "Приложить фото"
    bl_description = "Картинка уйдёт в чат вместе со следующим сообщением"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(subtype="FILE_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp", options={"HIDDEN"})

    def invoke(self, context, event):
        if self.files:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        paths = []
        folder = self.directory
        for item in self.files:
            name = str(getattr(item, "name", "") or "")
            if name:
                paths.append(os.path.join(folder, name))
        if self.filepath and not paths:
            paths.append(self.filepath)
        attach_images(paths)
        _restore_chat_modal()
        area = _chat_area(_find_window(context, int(_UI.get("window") or 0)))
        if area is not None:
            area.tag_redraw()
        return {"FINISHED"}

    def cancel(self, context):
        _restore_chat_modal()


class DWGREADER_FH_chat_image(bpy.types.FileHandler):
    bl_idname = "DWGREADER_FH_chat_image"
    bl_label = "Картинка в чат"
    bl_import_operator = "dwgreader.chat_attach"
    bl_file_extensions = ".png;.jpg;.jpeg;.webp;.gif;.bmp"

    @classmethod
    def poll_drop(cls, context):
        window = getattr(context, "window", None)
        pointer = int(_UI.get("window") or 0)
        return bool(window is not None and pointer and window.as_pointer() == pointer)


def register() -> None:
    bpy.utils.register_class(DWGREADER_OT_chat_view)
    bpy.utils.register_class(DWGREADER_OT_chat_attach)
    try:
        bpy.utils.register_class(DWGREADER_FH_chat_image)
    except Exception as error:
        print(f"Подложка DWG: перетаскивание фото: {error}")
    ensure_draw()


def unregister() -> None:
    remove_draw()
    _MODAL["running"] = False
    _MODAL["window"] = 0
    for cls in (DWGREADER_FH_chat_image, DWGREADER_OT_chat_attach, DWGREADER_OT_chat_view):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
