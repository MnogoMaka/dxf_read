"""Что ответ чата изменил в сцене, и шаг назад.

Снимок держится в памяти, в журнал сцены пишутся только строки карточки.
"""

from __future__ import annotations

import uuid

import bpy


_CHECKPOINTS: dict[str, dict] = {}
_PENDING: dict = {"token": "", "before": None}
_TYPE = {
    "MESH": "меш",
    "CURVE": "кривая",
    "SURFACE": "поверхность",
    "FONT": "текст",
    "LIGHT": "свет",
    "CAMERA": "камера",
    "EMPTY": "пусто",
    "ARMATURE": "арматура",
    "GPENCIL": "карандаш",
    "GREASEPENCIL": "карандаш",
}


def type_label(kind: str) -> str:
    return _TYPE.get(str(kind or "").upper(), str(kind or "объект").lower())


def diff_snapshots(before: dict, after: dict) -> dict:
    """Добавленные, удалённые, переименованные и сдвинутые объекты. Ключ — session_uid."""
    old = before.get("objects") or {}
    new = after.get("objects") or {}
    added, removed, renamed, moved = [], [], [], []
    for uid, item in new.items():
        prev = old.get(uid)
        if prev is None:
            added.append(item)
            continue
        if prev.get("name") != item.get("name"):
            renamed.append((prev, item))
        elif prev.get("loc") != item.get("loc") or prev.get("collection") != item.get("collection"):
            moved.append((prev, item))
    for uid, item in old.items():
        if uid not in new:
            removed.append(item)
    camera = ""
    if before.get("camera") != after.get("camera") and after.get("camera"):
        camera = str(after.get("camera") or "")
    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "moved": moved,
        "camera": camera,
    }


def is_empty(changes: dict) -> bool:
    return not any(changes.get(key) for key in ("added", "removed", "renamed", "moved", "camera"))


def headline(changes: dict) -> str:
    parts = []
    if changes.get("added"):
        parts.append(f"{len(changes['added'])} добавлено")
    if changes.get("removed"):
        parts.append(f"{len(changes['removed'])} удалено")
    if changes.get("renamed"):
        parts.append(f"{len(changes['renamed'])} переименовано")
    if changes.get("moved"):
        parts.append(f"{len(changes['moved'])} изменено")
    return ", ".join(parts) or "без изменений"


def change_lines(changes: dict) -> list[str]:
    lines: list[str] = []
    groups: dict[str, list] = {}
    for item in changes.get("added") or []:
        groups.setdefault(str(item.get("collection") or "Сцена"), []).append(item)
    for collection, items in groups.items():
        lines.append(f"+ {collection}")
        for item in items:
            lines.append(f"  + {item.get('name')} ({type_label(item.get('type'))})")
    for item in changes.get("removed") or []:
        lines.append(f"- {item.get('name')} ({type_label(item.get('type'))})")
    for prev, item in changes.get("renamed") or []:
        lines.append(f"~ {prev.get('name')} → {item.get('name')}")
    for prev, item in changes.get("moved") or []:
        if prev.get("collection") != item.get("collection"):
            lines.append(f"~ {item.get('name')} → {item.get('collection') or 'сцена'}")
        else:
            lines.append(f"~ {item.get('name')}")
    if changes.get("camera"):
        lines.append(f"~ {changes['camera']} (камера), теперь камера сцены")
    if len(lines) > 80:
        extra = len(lines) - 80
        lines = lines[:80] + [f"~ ещё {extra}"]
    return lines


def _collection_name(obj) -> str:
    users = getattr(obj, "users_collection", None) or []
    return users[0].name if users else ""


def _location(obj) -> tuple:
    loc = obj.matrix_world.translation
    return (round(float(loc.x), 3), round(float(loc.y), 3), round(float(loc.z), 3))


def capture_scene() -> dict:
    objects = {}
    for obj in bpy.data.objects:
        name = str(obj.name)
        if name.startswith("."):
            continue
        objects[int(obj.session_uid)] = {
            "name": name,
            "type": str(obj.type),
            "collection": _collection_name(obj),
            "loc": _location(obj),
        }
    camera = getattr(bpy.context.scene, "camera", None)
    return {"objects": objects, "camera": camera.name if camera else ""}


def _clear_token(scene) -> None:
    try:
        if "dwgreader_undo_token" in scene.keys():
            del scene["dwgreader_undo_token"]
    except Exception:
        pass


def arm(scene) -> None:
    """Запоминает сцену до инструментов ответа. Старые карточки остаются, откат есть у последней."""
    token = uuid.uuid4().hex[:8]
    try:
        scene["dwgreader_undo_token"] = token
    except Exception:
        pass
    try:
        bpy.ops.ed.undo_push(message="Подложка: до ответа чата")
    except Exception:
        pass
    _PENDING["token"] = token
    _PENDING["before"] = capture_scene()


def take_result() -> dict | None:
    before = _PENDING.get("before")
    token = str(_PENDING.get("token") or "")
    _PENDING["before"] = None
    _PENDING["token"] = ""
    if not before or not token:
        return None
    changes = diff_snapshots(before, capture_scene())
    if is_empty(changes):
        scene = getattr(bpy.context, "scene", None)
        if scene is not None:
            _clear_token(scene)
        return None
    _CHECKPOINTS[token] = {
        "before": before,
        "token": token,
        "added_names": [item["name"] for item in changes["added"]],
        "renames": [(prev["name"], item["name"]) for prev, item in changes["renamed"]],
        "moves": [(item["name"], item["loc"]) for _prev, item in changes["moved"]],
    }
    return {
        "role": "changes",
        "text": headline(changes),
        "lines": change_lines(changes),
        "checkpoint": token,
        "resolved": "",
    }


def has_checkpoint(checkpoint: str) -> bool:
    return checkpoint in _CHECKPOINTS


def keep(checkpoint: str) -> None:
    _CHECKPOINTS.pop(checkpoint, None)
    scene = getattr(bpy.context, "scene", None)
    if scene is not None and str(scene.get("dwgreader_undo_token") or "") == checkpoint:
        _clear_token(scene)


def _same(before: dict) -> bool:
    return is_empty(diff_snapshots(before, capture_scene()))


def _force(snap: dict) -> None:
    """Убирает то, что ответ добавил, если шаг отмены Blender это не забрал."""
    added = set(snap.get("added_names") or [])
    for obj in list(bpy.data.objects):
        if obj.name in added and getattr(obj, "library", None) is None:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
    by_name = {obj.name: obj for obj in bpy.data.objects}
    for old, new in snap.get("renames") or []:
        obj = by_name.get(new)
        if obj is None:
            continue
        try:
            obj.name = old
        except Exception:
            pass
    by_name = {obj.name: obj for obj in bpy.data.objects}
    for name, loc in snap.get("moves") or []:
        obj = by_name.get(name)
        if obj is None:
            continue
        try:
            obj.matrix_world.translation = loc
        except Exception:
            pass


def revert(checkpoint: str) -> bool:
    snap = _CHECKPOINTS.get(checkpoint)
    if snap is None:
        return False
    before = snap["before"]
    token = snap["token"]
    for _ in range(24):
        if _same(before):
            break
        try:
            result = bpy.ops.ed.undo()
        except Exception:
            break
        if "FINISHED" not in result:
            break
        scene = getattr(bpy.context, "scene", None)
        if scene is None or str(scene.get("dwgreader_undo_token") or "") != token:
            try:
                bpy.ops.ed.redo()
            except Exception:
                pass
            break
    if not _same(before):
        _force(snap)
    _CHECKPOINTS.pop(checkpoint, None)
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        _clear_token(scene)
    return True
