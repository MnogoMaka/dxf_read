"""Чат в панели: план ChatGPT и сокет MCP for Blender."""

from __future__ import annotations

import base64
import http.client
import json
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from . import chatgpt_auth, scene_changes
from .install_support import compose_mcp_addon, deps_ready
from .mcp_bridge import BridgeError, port_open, send_command


RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_ROUNDS = 12
MAX_MESSAGES = 30
_JOB: dict = {"done": False, "error": "", "text": "", "image": "", "tools": [], "entries": []}
_OUTGOING: list[str] = []
_LIVE: dict = {"text": "", "tool": "", "tools": [], "summary": ""}
_PORT = {"at": 0.0, "open": False}
_TRANSCRIPT: list = []
_DISABLED: set[str] = set()
_TURN_STATE: dict[int, str] = {}
_CANCEL = threading.Event()
READ_TIMEOUT = 600.0
RETRY_DELAYS = (0.5, 1.5, 4.0)
_NO_RETRY_CODES = {
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_user_not_eligible",
    "subscription_sharing_unsupported_capability",
    "subscription_sharing_route_not_supported",
    "subscription_sharing_invalid_user",
    "chatpass_v2_scope_not_authorized",
    "chatpass_v2_invalid_authorization_context",
}
_RETRY_IN_STREAM = {
    "subscription_sharing_usage_unavailable",
    "subscription_sharing_user_unavailable",
    "server_error",
}
DRAFT_NAME = "DWGREADER Chat"

INSTRUCTIONS = (
    "Ты работаешь в открытом Blender пользователя. Отвечай по-русски. "
    "Сцену меняй инструментами MCP for Blender. get_scene_info даёт объекты и сразу пишет, что выделено. "
    "У объектов подложки есть свойства dxf_layer (слой чертежа) и category (тип). "
    "look показывает текущий вид. execute_blender_code выполняет Python: bpy, bmesh, mathutils. "
    "Библиотеки и генерацию вызывай их инструментами. Если ответ говорит, что сервис выключен, "
    "попроси включить его галочку в панели MCP for Blender. "
    "Печатай то, что нужно узнать. Если к сообщению приложено фото, смотри его: это референс задачи. "
    "Для моделей и объектов важен законченный результат, а не быстрая заглушка: сначала проверь референс, "
    "пропорции, размеры и посадку, затем создай полноценную геометрию со всеми видимыми частями. "
    "Используй сглаживание, bevel или subdivision только там, где это нужно форме; не оставляй лишних граней, "
    "пересечений, неправильных нормалей и случайных материалов. Назначь подходящие материалы и текстуры. "
    "После построения или импорта обязательно осмотри результат через look, сравни с задачей и исправь заметные "
    "ошибки формы, пропорций, положения и материалов до финального ответа. Не выдавай черновик за готовую модель. "
    "Для внешней 3D-генерации используй quality='high', если этот параметр доступен, кроме явной просьбы о черновике. "
    "Для Meshy по тексту дождись SUCCEEDED preview, затем запусти refine_meshy_job, дождись SUCCEEDED refine и "
    "импортируй именно текстурированный refine. Импорт preview допустим только по явной просьбе пользователя. "
    "Для остальных генераторов дождись завершения задачи, импортируй результат и тоже проверь его через look."
)

_SELECTION_CODE = """
import bpy
selected = list(bpy.context.selected_objects)
active = bpy.context.view_layer.objects.active
print("selected:", ", ".join(obj.name for obj in selected) or "-")
print("active:", active.name if active else "-")
for obj in selected:
    layer = obj.get("dxf_layer")
    category = obj.get("category")
    if layer or category:
        print(f"{obj.name}: dxf_layer={layer or '-'} category={category or '-'}")
"""

def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": False,
    }


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


def _int(description: str) -> dict:
    return {"type": "integer", "description": description}


def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _command(
    name: str,
    description: str,
    properties: dict,
    required: list[str],
    socket: str = "",
    rename: dict | None = None,
) -> dict:
    return {
        "name": name,
        "socket": socket or name,
        "description": description,
        "properties": properties,
        "required": required,
        "rename": rename or {},
    }


# Имена совпадают с командами сокета MCP for Blender. Телеметрию не отдаём.
_COMMANDS = [
    _command("execute_blender_code", "Выполнить Python в живом Blender. Печать скрипта возвращается текстом.", {"code": _text("Код bpy")}, ["code"], "execute_code"),
    _command("get_scene_info", "Сводка сцены, выделенные и активный объект. У подложки есть dxf_layer и category.", {}, []),
    _command("get_object_info", "Подробности одного объекта по имени.", {"object_name": _text("Имя объекта")}, ["object_name"], "get_object_info", {"object_name": "name"}),
    _command("look", "Снимок текущего 3D-вида. Картинка вернётся в переписку.", {"max_size": _int("Длинная сторона в пикселях")}, [], "get_viewport_screenshot"),
    _command("get_addon_info", "Версия Blender, версия MCP и какие библиотеки включены.", {}, []),
    _command("list_scene_items", "Найти объекты, материалы и коллекции по части имени.", {"query": _text("Часть имени"), "limit": _int("Сколько результатов")}, []),
    _command("get_world_state_snapshot", "Снимок состояния мира сцены.", {}, []),
    _command("describe_node_type", "Поля одного типа ноды Blender.", {"bl_idname": _text("Идентификатор ноды"), "property_overrides": {"type": "object", "description": "Подстановки свойств"}}, ["bl_idname"]),
    _command("bpy_api_lookup", "Найти имя в API bpy по короткому запросу.", {"query": _text("Что найти")}, ["query"]),
    _command("export_scene", "Экспорт сцены, выделения или названных объектов в GLB или FBX.", {"filepath": _text("Куда записать файл"), "format": _text("glb или fbx"), "object_names": {"type": "array", "items": {"type": "string"}, "description": "Имена объектов"}, "selection_only": _bool("Только выделение"), "apply_modifiers": _bool("Применить модификаторы")}, []),
    _command("get_polyhaven_status", "Включён ли Poly Haven.", {}, []),
    _command("get_polyhaven_categories", "Категории Poly Haven.", {"asset_type": _text("hdris, textures или models")}, ["asset_type"]),
    _command("search_polyhaven_assets", "Поиск HDRI, текстур и моделей Poly Haven.", {"asset_type": _text("hdris, textures, models или all"), "category": _text("Путь категории"), "attributes": {"type": "object", "description": "Фильтры вроде weather"}, "query": _text("Что искать"), "limit": _int("Сколько результатов"), "min_size_m": {"type": "number", "description": "Минимальный размер модели в метрах"}}, []),
    _command("get_polyhaven_asset_preview", "Превью ассета Poly Haven перед скачиванием.", {"asset_id": _text("Идентификатор ассета")}, ["asset_id"]),
    _command("download_polyhaven_asset", "Скачать ассет Poly Haven в сцену.", {"asset_id": _text("Идентификатор"), "asset_type": _text("hdris, textures или models"), "resolution": _text("Например 1k"), "file_format": _text("Формат файла")}, ["asset_id", "asset_type"]),
    _command("set_texture", "Назначить текстуру Poly Haven объекту.", {"object_name": _text("Имя объекта"), "texture_id": _text("Идентификатор текстуры")}, ["object_name", "texture_id"]),
    _command("get_sketchfab_status", "Включён ли Sketchfab.", {}, []),
    _command("search_sketchfab_models", "Поиск моделей Sketchfab.", {"query": _text("Что искать"), "categories": _text("Категории"), "count": _int("Сколько результатов"), "downloadable": _bool("Только скачиваемые")}, ["query"]),
    _command("get_sketchfab_model_preview", "Превью модели Sketchfab.", {"uid": _text("UID модели")}, ["uid"]),
    _command("download_sketchfab_model", "Скачать модель Sketchfab в сцену.", {"uid": _text("UID модели"), "normalize_size": _bool("Подогнать размер"), "target_size": {"type": "number", "description": "Целевой размер"}}, ["uid"]),
    _command("get_polypizza_status", "Включён ли Poly Pizza.", {}, []),
    _command("search_polypizza_models", "Поиск низкополигональных моделей Poly Pizza.", {"query": _text("Что искать"), "category": _text("Категория"), "licence": _text("Лицензия"), "animated": _bool("Только анимированные"), "limit": _int("Сколько результатов"), "page": _int("Страница")}, []),
    _command("download_polypizza_model", "Скачать модель Poly Pizza в сцену.", {"model_id": _text("Идентификатор модели"), "normalize_size": _bool("Подогнать размер"), "target_size": {"type": "number", "description": "Целевой размер"}}, ["model_id"]),
    _command("get_hyper3d_status", "Включён ли Hyper3D Rodin.", {}, []),
    _command("create_rodin_job", "Начать генерацию Hyper3D Rodin.", {"text_prompt": _text("Описание модели"), "bbox_condition": {"type": "array", "items": {"type": "number"}, "description": "Габарит"}, "quality": {"type": "string", "enum": ["standard", "high", "draft"], "description": "high — итоговая модель; draft — только явно запрошенный черновик"}}, []),
    _command("poll_rodin_job_status", "Статус генерации Hyper3D Rodin.", {"subscription_key": _text("Ключ задачи основного сайта"), "request_id": _text("Идентификатор задачи fal")}, []),
    _command("import_generated_asset", "Импортировать готовую модель Hyper3D Rodin.", {"task_uuid": _text("UUID задачи основного сайта"), "request_id": _text("Идентификатор задачи fal"), "name": _text("Имя объекта")}, ["name"]),
    _command("get_hunyuan3d_status", "Включён ли Hunyuan3D.", {}, []),
    _command("create_hunyuan_job", "Начать генерацию Hunyuan3D.", {"text_prompt": _text("Описание модели"), "image": _text("Путь к картинке"), "quality": {"type": "string", "enum": ["standard", "high"], "description": "Для итоговой модели high"}}, []),
    _command("poll_hunyuan_job_status", "Статус генерации Hunyuan3D.", {"job_id": _text("Идентификатор задачи")}, ["job_id"]),
    _command("import_generated_asset_hunyuan", "Импортировать готовую модель Hunyuan3D.", {"name": _text("Имя объекта"), "zip_file_url": _text("Ссылка на архив результата")}, ["name"]),
    _command("get_tripo_status", "Включён ли Tripo.", {}, []),
    _command("create_tripo_job", "Начать генерацию Tripo.", {"text_prompt": _text("Описание модели"), "image": _text("Путь к картинке"), "quality": {"type": "string", "enum": ["standard", "high"], "description": "Для итоговой модели high"}}, []),
    _command("poll_tripo_job_status", "Статус генерации Tripo.", {"request_id": _text("Идентификатор задачи")}, ["request_id"]),
    _command("import_generated_asset_tripo", "Импортировать готовую модель Tripo.", {"request_id": _text("Идентификатор задачи"), "name": _text("Имя объекта")}, ["request_id", "name"]),
    _command("get_meshy_status", "Включён ли Meshy.", {}, []),
    _command("create_meshy_job", "Начать генерацию Meshy. По тексту создаётся preview без текстур: затем обязателен refine_meshy_job.", {"text_prompt": _text("Описание модели"), "image": _text("Путь к картинке"), "quality": {"type": "string", "enum": ["standard", "high"], "description": "high — детальная геометрия и текстуры 4K, где поддерживается"}}, []),
    _command("refine_meshy_job", "Обязательный второй этап Meshy по тексту: наложить текстуру на завершённое preview.", {"preview_task_id": _text("Идентификатор превью"), "quality": {"type": "string", "enum": ["standard", "high"], "description": "Должно совпадать с качеством preview"}}, ["preview_task_id"]),
    _command("poll_meshy_job_status", "Статус генерации Meshy.", {"task_id": _text("Идентификатор задачи"), "kind": _text("t2p — preview, t2r — текстурированный refine, i2m — изображение")}, ["task_id"]),
    _command("import_generated_asset_meshy", "Импортировать готовую модель Meshy.", {"name": _text("Имя объекта"), "model_url": _text("Ссылка на модель")}, ["name"]),
]

_BY_NAME = {item["name"]: item for item in _COMMANDS}

TOOLS = [
    {
        "type": "namespace",
        "name": "blender",
        "description": "Команды включённого MCP for Blender: сцена, код, библиотеки и генерация.",
        "tools": [
            _function(item["name"], item["description"], item["properties"], item["required"])
            for item in _COMMANDS
        ],
    }
]

COMPLETION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": item["name"],
            "description": item["description"],
            "parameters": {
                "type": "object",
                "properties": item["properties"],
                "required": item["required"],
                "additionalProperties": False,
            },
        },
    }
    for item in _COMMANDS
]


class ChatError(Exception):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def uses_chatgpt() -> bool:
    """Вход в ChatGPT есть. Тогда запасной API для чата не подставляется."""
    return chatgpt_auth.public_profile() is not None


def api_settings(prefs) -> dict | None:
    url = str(getattr(prefs, "llm_base_url", "") or "").strip()
    model = str(getattr(prefs, "llm_model", "") or "").strip()
    if not url or not model:
        return None
    timeout = float(getattr(prefs, "llm_timeout", 300.0) or 300.0)
    return {
        "base_url": url,
        "model": model,
        "api_key": str(getattr(prefs, "llm_api_key", "") or "").strip(),
        "timeout": max(10.0, timeout),
    }


def login_hint(prefs) -> str:
    profile = chatgpt_auth.public_profile()
    if profile is not None:
        if not profile.get("sharing"):
            return "План ChatGPT не разрешён"
        slug = (getattr(prefs, "chatgpt_model", "") or chatgpt_auth.saved_model() or "").strip()
        if not slug or slug == "none":
            return "Модель ChatGPT не выбрана"
        return ""
    if api_settings(prefs) is None:
        return "Войдите в ChatGPT или укажите адрес и модель API"
    return ""


def chosen_model(prefs) -> str:
    if uses_chatgpt():
        return (getattr(prefs, "chatgpt_model", "") or chatgpt_auth.saved_model() or "").strip()
    return str(getattr(prefs, "llm_model", "") or "").strip()


def model_label(prefs) -> str:
    name = chosen_model(prefs)
    if name:
        return name
    return "ChatGPT" if uses_chatgpt() else "API"


def port_is_open() -> bool:
    """Проверка порта не чаще раза в несколько секунд: каждый заход иначе открывает сокет."""
    now = time.monotonic()
    if now - _PORT["at"] < 8.0:
        return bool(_PORT["open"])
    opened = port_open(timeout=0.15)
    _PORT["at"] = now
    _PORT["open"] = opened
    return opened


def forget_port() -> None:
    _PORT["at"] = 0.0


def mcp_operator_ready() -> bool:
    """Оператор реально зарегистрирован. hasattr(bpy.ops...) истинно и для отсутствующей команды."""
    try:
        return hasattr(bpy.types, "BLENDERMCP_OT_StartServer")
    except Exception:
        return False


def _failure_text(raw: str, status: int) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    err = payload.get("error")
    code = ""
    message = ""
    if isinstance(err, dict):
        code = str(err.get("code") or err.get("type") or "")
        message = str(err.get("message") or "")
    elif isinstance(err, str):
        message = err
    usage = getattr(chatgpt_auth, "USAGE_URL", "https://chatgpt.com/codex/settings/usage")
    plan = {
        "subscription_sharing_usage_limit_exceeded": f"Лимит плана ChatGPT для этого приложения исчерпан. Расход: {usage}",
        "subscription_sharing_usage_unavailable": "ChatGPT сейчас не может проверить расход плана. Повторите чуть позже",
        "subscription_sharing_user_unavailable": "ChatGPT сейчас не может прочитать аккаунт. Повторите чуть позже",
        "subscription_sharing_user_not_eligible": "Этот аккаунт не может отдавать план ChatGPT приложению",
        "subscription_sharing_route_not_supported": "План ChatGPT принимает только потоковый POST /v1/responses",
        "subscription_sharing_invalid_user": "ChatGPT не подтвердил вход. Войдите ещё раз",
    }
    if code in plan:
        return plan[code]
    if message and message not in {code, "Запрос отклонён"}:
        return message
    if code in {"invalid_token", "insufficient_scope"}:
        return chatgpt_auth.auth_error_text(code, message)
    if status == 401:
        return "Сессия ChatGPT недействительна. Войдите ещё раз"
    if status == 429:
        return f"Лимит плана ChatGPT исчерпан. Расход: {usage}"
    return message or f"ChatGPT ответил HTTP {status}"


def _completed_response(response, on_text=None, on_tool=None, on_summary=None) -> dict:
    """План ChatGPT отдаёт ответ потоком. Вызов инструмента лежит в output_item.done, а response.completed часто приходит пустым."""
    event = "message"
    data_lines: list[str] = []
    completed: dict | None = None
    failed = ""
    failed_code = ""
    streamed: dict[int, dict] = {}
    text_parts: list[str] = []
    summaries: list[str] = []

    def flush() -> None:
        nonlocal event, completed, failed, failed_code
        if not data_lines:
            event = "message"
            return
        raw = "\n".join(data_lines)
        data_lines.clear()
        kind = event
        event = "message"
        if raw == "[DONE]":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or kind)
        if kind == "response.completed":
            body = payload.get("response")
            if isinstance(body, dict):
                completed = body
        elif kind == "response.output_item.added":
            item = payload.get("item")
            if isinstance(item, dict):
                streamed.setdefault(int(payload.get("output_index") or 0), item)
        elif kind == "response.function_call_arguments.delta":
            item_id = payload.get("item_id")
            delta = str(payload.get("delta") or "")
            for item in streamed.values():
                if item.get("id") == item_id and delta:
                    item["arguments"] = str(item.get("arguments") or "") + delta
        elif kind == "response.function_call_arguments.done":
            item_id = payload.get("item_id")
            for item in streamed.values():
                if item.get("id") == item_id:
                    item["arguments"] = payload.get("arguments") or item.get("arguments") or ""
        elif kind == "response.reasoning_summary_text.delta":
            summaries.append(str(payload.get("delta") or ""))
            if on_summary is not None:
                on_summary("".join(summaries))
        elif kind == "response.reasoning_summary_text.done":
            summary = str(payload.get("text") or "")
            if summary and not summaries:
                summaries.append(summary)
                if on_summary is not None:
                    on_summary(summary)
        elif kind == "response.output_item.done":
            item = payload.get("item")
            if isinstance(item, dict):
                streamed[int(payload.get("output_index") or 0)] = item
                if item.get("type") == "function_call" and on_tool is not None:
                    on_tool(_tool_name(str(item.get("name") or "")))
        elif kind == "response.output_text.delta":
            text_parts.append(str(payload.get("delta") or ""))
            if on_text is not None:
                on_text("".join(text_parts))
        elif kind in {"response.failed", "error"}:
            err = payload.get("error")
            if not isinstance(err, dict):
                body = payload.get("response") if isinstance(payload.get("response"), dict) else {}
                err = body.get("error") if isinstance(body.get("error"), dict) else {}
            failed_code = str(err.get("code") or "")
            failed = str(err.get("message") or failed_code or kind)

    for line in response:
        if _CANCEL.is_set():
            raise ChatError("Отменено", "cancelled")
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            flush()
            continue
        if text.startswith(":"):
            continue
        if text.startswith("event:"):
            event = text[6:].strip()
        elif text.startswith("data:"):
            data_lines.append(text[5:].lstrip())
    flush()
    if failed and completed is None and not streamed:
        raise ChatError(
            _failure_text(json.dumps({"error": {"code": failed_code, "message": failed}}), 0),
            failed_code,
        )
    if completed is None and not streamed and not text_parts:
        raise ChatError("Ответ ChatGPT оборвался")
    if completed is None:
        completed = {}
    output = [item for item in completed.get("output") or [] if isinstance(item, dict)]
    if not output:
        output = [streamed[index] for index in sorted(streamed)]
        if text_parts and not any(item.get("type") == "message" for item in output):
            output.append({
                "type": "message",
                "content": [{"type": "output_text", "text": "".join(text_parts)}],
            })
    completed["output"] = output
    err = completed.get("error")
    if isinstance(err, dict) and (err.get("message") or err.get("code")):
        code = str(err.get("code") or "")
        raise ChatError(_failure_text(json.dumps({"error": err}), 0), code)
    if summaries:
        completed["_summary"] = "".join(summaries).strip()
    return completed


class _Replay:
    """Исходный output ответа. Следующее сообщение пользователя получает его, а не сжатый текст."""

    def __init__(self) -> None:
        self._items: dict[str, list] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(text: str, call_ids: list[str]) -> str:
        return json.dumps([text or "", list(call_ids)], ensure_ascii=False)

    def put(self, text: str, call_ids: list[str], items: list) -> None:
        with self._lock:
            self._items[self.key(text, call_ids)] = items

    def get(self, text: str, call_ids: list[str]) -> list | None:
        with self._lock:
            return self._items.get(self.key(text, call_ids))


_REPLAY = _Replay()


def _cache_key() -> str:
    return "dwgreader-chat"


def _session_id() -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "dwgreader:" + _cache_key()))


def _turn_index(items: list) -> int:
    count = 0
    for item in items:
        if item.get("role") == "user":
            count += 1
        elif item.get("type") == "message" and item.get("role") == "user":
            count += 1
    return count


def _error_code(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or "")
    return ""


def _feature_for(raw: str) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(err, dict):
        err = {}
    detail = payload.get("detail") if isinstance(payload, dict) else ""
    pieces = (str(err.get("param") or ""), str(err.get("message") or ""), str(detail or ""))
    for where in (piece.lower() for piece in pieces):
        if "prompt_cache_key" in where:
            return "prompt_cache_key"
        if "effort" in where:
            return "reasoning_effort"
        if "summary" in where:
            return "reasoning_summary"
        if where.startswith("include") or "encrypted_content" in where:
            return "include"
        if ".output" in where or "function_call_output" in where:
            return "tool_images"
        if "not found" in where and ("item" in where or "store" in where):
            return "replay_ids"
        if "turn-state" in where or "turn_state" in where:
            return "turn_state"
    return None


def _request_payload(model: str, items: list) -> dict:
    body = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": items,
        "tools": TOOLS,
        "store": False,
        "stream": True,
    }
    reasoning = {}
    if "reasoning_effort" not in _DISABLED:
        reasoning["effort"] = "high"
    if "reasoning_summary" not in _DISABLED:
        reasoning["summary"] = "auto"
    if reasoning:
        body["reasoning"] = reasoning
    if "include" not in _DISABLED:
        body["include"] = ["reasoning.encrypted_content"]
    if "prompt_cache_key" not in _DISABLED:
        body["prompt_cache_key"] = _cache_key()
    return body


def _request_headers(token: str, items: list) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {token}",
        "User-Agent": "DWGREADER/1.0",
    }
    if "prompt_cache_key" not in _DISABLED:
        headers["session_id"] = _session_id()
    if "turn_state" not in _DISABLED:
        state = _TURN_STATE.get(_turn_index(items), "")
        if state:
            headers["x-codex-turn-state"] = state
    return headers


def _strip_replay_ids(items: list) -> None:
    for item in items:
        if isinstance(item, dict):
            item.pop("id", None)


class _Watchdog:
    """Закрывает сокет, когда пользователь останавливает ответ."""

    def __init__(self, response) -> None:
        self.response = response
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            if not _CANCEL.is_set():
                continue
            try:
                raw = getattr(getattr(self.response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
                else:
                    self.response.close()
            except OSError:
                pass
            return

    def stop(self) -> None:
        self._stop.set()


def _retry_wait(attempt: int, headers=None) -> None:
    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
    retry_after = ""
    if headers is not None:
        try:
            retry_after = str(headers.get("Retry-After") or "")
        except Exception:
            retry_after = ""
    if retry_after.replace(".", "", 1).isdigit():
        delay = min(max(delay, float(retry_after)), 30.0)
    time.sleep(delay)


def _post(token: str, model: str, items: list, on_text=None, on_tool=None, on_summary=None) -> dict:
    token_now = token
    refreshed = False
    refused: set[str] = set()
    attempt = 0
    while True:
        if _CANCEL.is_set():
            raise ChatError("Отменено", "cancelled")
        if "replay_ids" in _DISABLED or "replay" in _DISABLED:
            _strip_replay_ids(items)
        payload = _request_payload(model, items)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            RESPONSES_URL,
            data=body,
            headers=_request_headers(token_now, items),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=READ_TIMEOUT)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            code = _error_code(raw)
            if error.code == 401 and not refreshed:
                refreshed = True
                fresh = ""
                try:
                    fresh = chatgpt_auth.force_refresh()
                except Exception:
                    fresh = ""
                if fresh:
                    token_now = fresh
                    continue
            feature = _feature_for(raw) if error.code == 400 else None
            if feature == "replay_ids" and "replay_ids" in _DISABLED:
                feature = "replay"
            if feature == "tool_images":
                _DISABLED.add("tool_images")
                raise ChatError(_failure_text(raw, error.code), code)
            if feature and feature not in refused:
                refused.add(feature)
                _DISABLED.add(feature)
                if feature == "turn_state":
                    _TURN_STATE.clear()
                continue
            retry_http = error.code in {429, 500, 502, 503, 504} and code not in _NO_RETRY_CODES
            if not retry_http or attempt >= len(RETRY_DELAYS):
                raise ChatError(_failure_text(raw, error.code), code)
            _retry_wait(attempt, error.headers)
            attempt += 1
            continue
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            if _CANCEL.is_set():
                raise ChatError("Отменено", "cancelled") from error
            if attempt >= len(RETRY_DELAYS):
                raise ChatError(f"Нет соединения с ChatGPT: {error}") from error
            time.sleep(RETRY_DELAYS[attempt])
            attempt += 1
            continue
        try:
            state = str(response.headers.get("x-codex-turn-state") or "")
        except Exception:
            state = ""
        if state and "turn_state" not in _DISABLED:
            _TURN_STATE[_turn_index(items)] = state
        watchdog = _Watchdog(response)
        try:
            with response:
                try:
                    return _completed_response(response, on_text=on_text, on_tool=on_tool, on_summary=on_summary)
                except ChatError as error:
                    if _CANCEL.is_set() or error.code == "cancelled":
                        raise ChatError("Отменено", "cancelled") from error
                    retry_stream = error.code in _RETRY_IN_STREAM or "оборвался" in str(error)
                    if not retry_stream or attempt >= len(RETRY_DELAYS):
                        raise
                    _retry_wait(attempt)
                    attempt += 1
        finally:
            watchdog.stop()


def _message_text(output: list) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part).strip()


def _as_text(result) -> str:
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict) and set(result) == {"result"}:
        text = str(result.get("result") or "")
    else:
        text = json.dumps(result, ensure_ascii=False)
    return text[:8000]


def set_outgoing_images(paths: list[str] | None) -> None:
    """Пути к фото текущего сообщения. В журнал сцены base64 не пишется."""
    global _OUTGOING
    _OUTGOING = [str(path) for path in (paths or []) if path]


def _take_outgoing() -> list[str]:
    global _OUTGOING
    paths = list(_OUTGOING)
    _OUTGOING = []
    return paths


def _data_url(path: str) -> str:
    file = Path(path)
    if not file.is_file():
        return ""
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(file.suffix.lower())
    if mime is None:
        return ""
    blob = file.read_bytes()
    if len(blob) > 3_500_000:
        shrunk = _shrink_for_chat(file)
        if shrunk:
            blob = shrunk
            mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _shrink_for_chat(file: Path) -> bytes | None:
    images = getattr(getattr(bpy, "data", None), "images", None)
    if images is None or not hasattr(images, "load"):
        return None
    image = None
    try:
        image = images.load(str(file), check_existing=False)
        width, height = image.size
        scale = min(1.0, 1600 / max(int(width), int(height), 1))
        if scale < 1.0:
            image.scale(max(1, int(width * scale)), max(1, int(height * scale)))
        folder = Path(tempfile.gettempdir()) / "dwgreader_chat"
        folder.mkdir(exist_ok=True)
        target = folder / f"chat_{int(time.time() * 1000)}.png"
        image.filepath_raw = str(target)
        image.file_format = "PNG"
        image.save()
        return target.read_bytes() if target.is_file() else None
    except Exception:
        return None
    finally:
        if image is not None:
            try:
                images.remove(image)
            except Exception:
                pass


def user_parts(text: str, images: list | None = None) -> list:
    """Текст и фото пользователя для Responses. Картинка уходит только в запрос."""
    parts: list[dict] = [{"type": "input_text", "text": (text or "").strip() or "Смотри приложенное фото."}]
    for path in images or []:
        url = _data_url(str(path))
        if url:
            parts.append({"type": "input_image", "image_url": url})
    return parts


def _image_item(path: str) -> dict:
    encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Снимок вьюпорта."},
            {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"},
        ],
    }


def _call_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _socket_params(spec: dict, args: dict) -> dict:
    params = {}
    allowed = spec["properties"]
    for key, value in args.items():
        if key not in allowed or value is None or value == "":
            continue
        params[spec["rename"].get(key, key)] = value
    if spec["socket"] == "export_scene" and not params.get("filepath"):
        folder = Path(tempfile.gettempdir()) / "dwgreader_chat"
        folder.mkdir(exist_ok=True)
        ext = str(params.get("format") or "glb")
        params["filepath"] = str(folder / f"export_{int(time.time() * 1000)}.{ext}")
    return params


def _present(result) -> tuple[str, str | None]:
    if isinstance(result, dict) and result.get("image_data"):
        shown = {key: value for key, value in result.items() if key != "image_data"}
        try:
            data = base64.b64decode(str(result["image_data"]))
        except Exception:
            data = b""
        if data:
            folder = Path(tempfile.gettempdir()) / "dwgreader_chat"
            folder.mkdir(exist_ok=True)
            suffix = "png" if str(result.get("format") or "").lower() == "png" else "jpg"
            path = folder / f"preview_{int(time.time() * 1000)}.{suffix}"
            path.write_bytes(data)
            shown["preview"] = "изображение приложено"
            return _as_text(shown), str(path)
        return _as_text(shown), None
    return _as_text(result), None


def _run_tool(name: str, args: dict) -> tuple[str, str | None]:
    spec = _BY_NAME.get(name)
    if spec is None:
        return f"Неизвестный инструмент {name}", None
    try:
        if name == "get_scene_info":
            summary = _as_text(send_command("get_scene_info"))
            try:
                picked = _as_text(send_command("execute_code", {"code": _SELECTION_CODE}))
            except BridgeError as error:
                if "не запущен" in str(error) or "закрыто" in str(error):
                    raise
                picked = f"Выделение не прочитано: {error}"
            return f"{summary}\n{picked}", None
        if name == "look":
            folder = Path(tempfile.gettempdir()) / "dwgreader_chat"
            folder.mkdir(exist_ok=True)
            path = folder / f"viewport_{int(time.time() * 1000)}.png"
            size = max(512, min(2048, int(args.get("max_size") or 1400)))
            send_command("get_viewport_screenshot", {"max_size": size, "filepath": str(path), "format": "png"})
            if not path.is_file():
                return "Снимок не записан", None
            return "Снимок вьюпорта снят.", str(path)
        return _present(send_command(spec["socket"], _socket_params(spec, args)))
    except BridgeError as error:
        message = str(error)
        if "не запущен" in message or "закрыто" in message:
            raise ChatError(message) from error
        return f"Ошибка MCP: {message}", None


def _history_items(items: list) -> list:
    """Журнал сцены становится input Responses. Строка инструмента не превращается в реплику пользователя."""
    converted = []
    index = 0
    while index < len(items):
        item = items[index]
        role = item.get("role")
        if role in {"tool", "changes"}:
            index += 1
            continue
        if role != "assistant":
            pictures = item.get("images") or []
            if isinstance(pictures, str):
                pictures = [pictures]
            converted.append({"role": "user", "content": user_parts(str(item.get("text") or ""), pictures)})
            index += 1
            continue
        tools = []
        follower = index + 1
        while follower < len(items) and items[follower].get("role") == "tool":
            tools.append(items[follower])
            follower += 1
        text = str(item.get("text") or "")
        call_ids = [str(value) for value in item.get("call_ids") or []]
        cached = None if "replay" in _DISABLED else _REPLAY.get(text, call_ids)
        if cached:
            for part in cached:
                replayed = _replay_item(part) if isinstance(part, dict) else None
                if replayed is None:
                    continue
                if "replay_ids" in _DISABLED or "replay" in _DISABLED:
                    replayed.pop("id", None)
                converted.append(replayed)
        else:
            if text:
                converted.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tool in tools:
                if not tool.get("call_id"):
                    continue
                converted.append(_call_item({
                    "call_id": tool.get("call_id"),
                    "name": tool.get("name") or tool.get("text"),
                    "arguments": tool.get("arguments") or "{}",
                }))
        for tool in tools:
            if not tool.get("call_id"):
                continue
            output_text = str(tool.get("output") if tool.get("output") is not None else tool.get("text") or "")
            image = "" if "tool_images" in _DISABLED else str(tool.get("image") or "")
            converted.append({
                "type": "function_call_output",
                "call_id": tool.get("call_id"),
                "output": _tool_output(output_text, image),
            })
        index = follower
    return converted


def _call_item(call: dict) -> dict:
    item = {
        "type": "function_call",
        "call_id": call.get("call_id"),
        "name": call.get("name"),
        "arguments": call.get("arguments") or "{}",
    }
    if call.get("id"):
        item["id"] = call["id"]
    if call.get("namespace"):
        item["namespace"] = call["namespace"]
    return item


def _replay_item(item: dict) -> dict | None:
    """Следующий запрос того же хода получает исходный output, а не сжатую строку ассистента."""
    kind = item.get("type")
    if kind == "function_call":
        return _call_item(item)
    if kind == "reasoning":
        kept = {key: item[key] for key in ("type", "id", "encrypted_content", "summary", "content") if key in item}
        return kept or None
    if kind == "message":
        return {
            "type": "message",
            "role": item.get("role") or "assistant",
            "content": item.get("content") or [],
        }
    return None


def _tool_output(text: str, image_path: str) -> str | list:
    if not image_path or not Path(image_path).is_file():
        return text
    encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    return [
        {"type": "input_text", "text": text or "Снимок вьюпорта."},
        {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"},
    ]


def _image_refused(message: str) -> bool:
    low = message.lower()
    return "image" in low or "input_image" in low


def _detach_tool_images(items: list) -> bool:
    """Если маршрут не принял картинку в результате инструмента, она уходит следующим сообщением пользователя."""
    moved = False
    extra = []
    for item in items:
        if item.get("type") != "function_call_output" or not isinstance(item.get("output"), list):
            continue
        images = [part for part in item["output"] if isinstance(part, dict) and part.get("type") == "input_image"]
        if not images:
            continue
        texts = [
            str(part.get("text") or "")
            for part in item["output"]
            if isinstance(part, dict) and part.get("type") == "input_text"
        ]
        item["output"] = "\n".join(part for part in texts if part) or "Снимок вьюпорта."
        for image in images:
            extra.append({"role": "user", "content": [{"type": "input_text", "text": "Снимок вьюпорта."}, image]})
        moved = True
    items.extend(extra)
    return moved


def _tool_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _remember_step(name: str) -> None:
    _LIVE["tool"] = name
    if name and name not in _LIVE["tools"]:
        _LIVE["tools"].append(name)


def _ask(token: str, model: str, items: list, on_text, on_tool, on_summary) -> dict:
    try:
        return _post(token, model, items, on_text=on_text, on_tool=on_tool, on_summary=on_summary)
    except ChatError as error:
        if ("tool_images" in _DISABLED or _image_refused(str(error))) and _detach_tool_images(items):
            _DISABLED.add("tool_images")
            return _post(token, model, items, on_text=on_text, on_tool=on_tool, on_summary=on_summary)
        raise


def _ssl_context() -> ssl.SSLContext:
    """TLS 1.2: OpenSSL 3.5 в Blender 5.2 на TLS 1.3 рвёт ответ роутера."""
    context = ssl.create_default_context()
    try:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    return context


def _urlopen(request: urllib.request.Request, timeout: float):
    url = str(request.full_url)
    if url.startswith("https://"):
        return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())
    return urllib.request.urlopen(request, timeout=timeout)


def _completion_user_content(text: str, images: list | None):
    caption = (text or "").strip() or "Смотри приложенное фото."
    parts: list[dict] = [{"type": "text", "text": caption}]
    for path in images or []:
        url = _data_url(str(path))
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) == 1:
        return caption
    return parts


def _completion_messages(history: list, user_text: str, user_images: list | None) -> list:
    """Журнал сцены → messages chat/completions. Карточки изменений не уходят."""
    messages: list[dict] = [{"role": "system", "content": INSTRUCTIONS}]
    index = 0
    while index < len(history):
        item = history[index]
        role = item.get("role")
        if role in {"tool", "changes"}:
            index += 1
            continue
        if role != "assistant":
            messages.append({
                "role": "user",
                "content": _completion_user_content(str(item.get("text") or ""), item.get("images")),
            })
            index += 1
            continue
        tools = []
        follower = index + 1
        while follower < len(history) and history[follower].get("role") == "tool":
            tools.append(history[follower])
            follower += 1
        text = str(item.get("text") or "")
        assistant: dict = {"role": "assistant", "content": text or None}
        calls = []
        for tool in tools:
            if not tool.get("call_id"):
                continue
            calls.append({
                "id": str(tool.get("call_id")),
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or tool.get("text") or ""),
                    "arguments": str(tool.get("arguments") or "{}"),
                },
            })
        if calls:
            assistant["tool_calls"] = calls
        if assistant.get("content") or assistant.get("tool_calls"):
            messages.append(assistant)
        for tool in tools:
            if not tool.get("call_id"):
                continue
            output_text = str(tool.get("output") if tool.get("output") is not None else tool.get("text") or "")
            messages.append({
                "role": "tool",
                "tool_call_id": str(tool["call_id"]),
                "content": output_text,
            })
            shot = str(tool.get("image") or "")
            if shot and Path(shot).is_file():
                messages.append({
                    "role": "user",
                    "content": _completion_user_content("Снимок вьюпорта.", [shot]),
                })
        index = follower
    messages.append({
        "role": "user",
        "content": _completion_user_content(user_text, user_images),
    })
    return messages


def _completion_calls(message: dict) -> list:
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        return [item for item in calls if isinstance(item, dict)]
    legacy = message.get("function_call")
    if isinstance(legacy, dict) and legacy.get("name"):
        return [{
            "id": str(message.get("id") or "call_legacy"),
            "type": "function",
            "function": legacy,
        }]
    return []


def _assistant_for_tools(message: dict, calls: list) -> dict:
    packed = {
        "role": "assistant",
        "content": message.get("content") if message.get("content") else None,
        "tool_calls": [],
    }
    for index, call in enumerate(calls):
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        packed["tool_calls"].append({
            "id": str(call.get("id") or f"call_{index}"),
            "type": "function",
            "function": {
                "name": str(fn.get("name") or call.get("name") or ""),
                "arguments": fn.get("arguments") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments") or {}, ensure_ascii=False),
            },
        })
    return packed


def _post_completion(model: str, messages: list, api: dict) -> dict:
    url = str(api.get("base_url") or "").rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "tools": COMPLETION_TOOLS,
        "tool_choice": "auto",
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "DWGREADER/1.0",
    }
    key = str(api.get("api_key") or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    timeout = float(api.get("timeout") or READ_TIMEOUT)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    attempt = 0
    while True:
        if _CANCEL.is_set():
            raise ChatError("Отменено", "cancelled")
        try:
            response = _urlopen(request, timeout)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            code = _error_code(raw)
            retry_http = error.code in {429, 500, 502, 503, 504} and code not in _NO_RETRY_CODES
            if not retry_http or attempt >= len(RETRY_DELAYS):
                raise ChatError(_failure_text(raw, error.code), code)
            _retry_wait(attempt, error.headers)
            attempt += 1
            continue
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            if _CANCEL.is_set():
                raise ChatError("Отменено", "cancelled") from error
            if attempt >= len(RETRY_DELAYS):
                raise ChatError(f"Нет соединения с моделью: {error}") from error
            time.sleep(RETRY_DELAYS[attempt])
            attempt += 1
            continue
        watchdog = _Watchdog(response)
        try:
            with response:
                raw = response.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as error:
            if _CANCEL.is_set():
                raise ChatError("Отменено", "cancelled") from error
            raise ChatError(f"Нет соединения с моделью: {error}") from error
        finally:
            watchdog.stop()
        if _CANCEL.is_set():
            raise ChatError("Отменено", "cancelled")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ChatError("Модель вернула не JSON") from error
        if not isinstance(data, dict) or not data.get("choices"):
            raise ChatError("Модель вернула неожиданный ответ")
        return data


def _complete_turn_api(
    model: str,
    history: list,
    user_text: str,
    user_images: list | None,
    api: dict,
    on_text,
    on_tool,
) -> tuple[str, str]:
    """Тот же цикл инструментов, через OpenAI-совместимый chat/completions."""
    global _TRANSCRIPT
    messages = _completion_messages(history, user_text, user_images)
    image = ""
    text = ""
    recorded: list = []
    try:
        for _round in range(MAX_ROUNDS):
            if _CANCEL.is_set():
                raise ChatError("Отменено", "cancelled")
            _LIVE["text"] = ""
            _LIVE["summary"] = ""
            data = _post_completion(model, messages, api)
            message = data["choices"][0].get("message") if isinstance(data["choices"][0], dict) else {}
            if not isinstance(message, dict):
                message = {}
            text = str(message.get("content") or "").strip() or text
            if text:
                _LIVE["text"] = text
                if on_text is not None:
                    on_text(text)
            calls = _completion_calls(message)
            if not calls:
                entry = {"role": "assistant", "text": text, "call_ids": []}
                if image:
                    entry["image"] = image
                recorded.append(entry)
                return text or "Модель ничего не ответила.", image
            packed = _assistant_for_tools(message, calls)
            messages.append(packed)
            call_ids = [str(call.get("id") or "") for call in packed["tool_calls"]]
            recorded.append({"role": "assistant", "text": text, "call_ids": call_ids})
            for call in packed["tool_calls"]:
                name = _tool_name(str(call["function"]["name"]))
                _remember_step(name)
                if on_tool is not None:
                    on_tool(name)
                output_text, shot = _run_tool(name, _call_args(call["function"]["arguments"]))
                if shot:
                    image = shot
                    _LIVE["image"] = shot
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": output_text,
                })
                if shot:
                    messages.append({
                        "role": "user",
                        "content": _completion_user_content("Снимок вьюпорта.", [shot]),
                    })
                tool_entry = {
                    "role": "tool",
                    "text": name,
                    "name": name,
                    "call_id": call["id"],
                    "arguments": call["function"]["arguments"],
                    "output": output_text,
                }
                if shot:
                    tool_entry["image"] = shot
                recorded.append(tool_entry)
        recorded.append({"role": "assistant", "text": text or "Остановлено после нескольких шагов.", "call_ids": []})
        return text or "Остановлено после нескольких шагов.", image
    finally:
        _TRANSCRIPT = recorded


def _snapshot(output: list) -> list:
    try:
        return json.loads(json.dumps(output))
    except (TypeError, ValueError):
        return [dict(item) for item in output if isinstance(item, dict)]


def complete_turn(
    token: str,
    model: str,
    history: list,
    user_text: str,
    on_text=None,
    on_tool=None,
    user_images: list | None = None,
    api: dict | None = None,
) -> tuple[str, str]:
    """Ответ модели и путь к снимку, если она смотрела вьюпорт."""
    global _TRANSCRIPT
    if api:
        return _complete_turn_api(model, history, user_text, user_images, api, on_text, on_tool)
    items = _history_items(history)
    items.append({"role": "user", "content": user_parts(user_text, user_images)})
    image = ""
    text = ""
    recorded: list = []

    def note_text(value: str) -> None:
        _LIVE["text"] = value
        if on_text is not None:
            on_text(value)

    def note_tool(value: str) -> None:
        _remember_step(value)
        if on_tool is not None:
            on_tool(value)

    def note_summary(value: str) -> None:
        _LIVE["summary"] = value

    try:
        for _round in range(MAX_ROUNDS):
            if _CANCEL.is_set():
                raise ChatError("Отменено", "cancelled")
            _LIVE["text"] = ""
            _LIVE["summary"] = ""
            body = _ask(token, model, items, note_text, note_tool, note_summary)
            output = [item for item in body.get("output") or [] if isinstance(item, dict)]
            text = _message_text(output) or text
            summary = str(body.get("_summary") or _LIVE.get("summary") or "")
            calls = [item for item in output if item.get("type") == "function_call"]
            call_ids = [str(call.get("call_id") or "") for call in calls]
            _REPLAY.put(text, call_ids, _snapshot(output))
            entry = {"role": "assistant", "text": text, "call_ids": call_ids}
            if summary:
                entry["summary"] = summary
            if not calls:
                if image:
                    entry["image"] = image
                recorded.append(entry)
                return text or "Модель ничего не ответила.", image
            recorded.append(entry)
            for item in output:
                if item.get("type") == "function_call":
                    continue
                replayed = _replay_item(item)
                if replayed is not None:
                    items.append(replayed)
            for call in calls:
                items.append(_call_item(call))
                name = _tool_name(str(call.get("name") or ""))
                note_tool(name)
                output_text, shot = _run_tool(name, _call_args(call.get("arguments")))
                if shot:
                    image = shot
                    _LIVE["image"] = shot
                items.append({
                    "type": "function_call_output",
                    "call_id": call.get("call_id"),
                    "output": _tool_output(output_text, shot or ""),
                })
                tool_entry = {
                    "role": "tool",
                    "text": name,
                    "name": name,
                    "call_id": call.get("call_id"),
                    "arguments": call.get("arguments") or "{}",
                    "output": output_text,
                }
                if shot:
                    tool_entry["image"] = shot
                recorded.append(tool_entry)
        recorded.append({"role": "assistant", "text": text or "Остановлено после нескольких шагов.", "call_ids": []})
        return text or "Остановлено после нескольких шагов.", image
    finally:
        _TRANSCRIPT = recorded


def release_turn() -> None:
    """Гасит «Думаю» после отката. Указатель сцены к этому моменту уже мог умереть."""
    _JOB["done"] = False
    _LIVE["text"] = ""
    _LIVE["tool"] = ""
    _LIVE["tools"] = []
    _LIVE["summary"] = ""
    _LIVE["image"] = ""
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return
    try:
        scene.dwgreader_chat_busy = False
    except ReferenceError:
        pass


def load_log(scene) -> list:
    try:
        payload = json.loads(scene.dwgreader_chat_log or "[]")
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def save_log(scene, items: list) -> None:
    scene.dwgreader_chat_log = json.dumps(items[-MAX_MESSAGES:], ensure_ascii=False)


def _wrap(text: str, width: int = 48, limit: int = 80) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        piece = f"{current} {word}".strip()
        if current and len(piece) > width:
            lines.append(current)
            current = word
        else:
            current = piece
        if len(lines) >= limit:
            break
    if current and len(lines) < limit:
        lines.append(current)
    return lines or [""]


def _show_image(layout, path: str) -> None:
    if not path or not Path(path).is_file():
        return
    try:
        image = bpy.data.images.load(path, check_existing=True)
        image.preview_ensure()
        layout.template_icon(icon_value=image.preview.icon_id, scale=6.0)
    except Exception:
        layout.label(text="Снимок сохранён")


def _bubble(layout, title: str, icon: str, text: str, image: str = "", summary: str = "") -> None:
    card = layout.box()
    card.label(text=title, icon=icon)
    body = card.column(align=True)
    body.scale_y = 0.85
    if summary:
        body.label(text="Рассуждение")
        for line in _wrap(summary, limit=8):
            body.label(text=line)
    for line in _wrap(text):
        body.label(text=line)
    if image:
        _show_image(card, image)


def _draft_block():
    block = bpy.data.texts.get(DRAFT_NAME)
    if block is None:
        block = bpy.data.texts.new(DRAFT_NAME)
    block.use_fake_user = True
    return block


def _draft_value(scene) -> str:
    try:
        block = bpy.data.texts.get(DRAFT_NAME)
    except Exception:
        block = None
    if block is not None:
        written = "\n".join(line.body for line in block.lines).strip()
        if written:
            return written
    return str(getattr(scene, "dwgreader_chat_draft", "") or "").strip()


def _clear_draft(scene) -> None:
    scene.dwgreader_chat_draft = ""
    try:
        block = bpy.data.texts.get(DRAFT_NAME)
        if block is not None:
            block.clear()
    except Exception:
        pass


def draw(layout, context) -> None:
    """Переписка в отдельном окне. Здесь только вход и сервер MCP."""
    from . import chat_view

    chat_view.ensure_draw()
    prefs = None
    addon = context.preferences.addons.get(__package__)
    if addon is not None:
        prefs = addon.preferences
    back = layout.row()
    op = back.operator("dwgreader.goto", text="Инструменты", icon="BACK", emboss=False)
    op.screen = "home"
    note = layout.box()
    if chat_view.window_open(context):
        note.label(text="Чат открыт отдельным окном", icon="WINDOW")
        note.label(text="Окно можно двигать, инструменты здесь свободны")
    else:
        note.label(text="Окно чата закрыто", icon="INFO")
    hint = login_hint(prefs)
    if hint:
        warn = note.column(align=True)
        warn.scale_y = 0.8
        warn.label(text=hint, icon="ERROR")
    status = note.row(align=True)
    if port_is_open():
        status.label(text="MCP подключён", icon="CHECKMARK")
    elif mcp_operator_ready():
        status.operator("dwgreader.chat_server", text="Запустить сервер")
    else:
        status.operator("dwgreader.chat_install", text="Установить MCP")
    if not chat_view.window_open(context):
        layout.operator("dwgreader.chat_view", text="Показать окно чата")


def _prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


def _redraw_view() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {"VIEW_3D", "IMAGE_EDITOR"}:
                    area.tag_redraw()
    except Exception:
        pass


def _finish_timer():
    scene = bpy.context.scene if bpy.context else None
    if scene is None:
        return None
    try:
        if not _JOB.get("done"):
            _redraw_view()
            return 0.4
        scene.dwgreader_chat_busy = False
    except ReferenceError:
        return None
    items = load_log(scene)
    entries = [entry for entry in (_JOB.get("entries") or []) if isinstance(entry, dict)]
    if _JOB.get("error"):
        text = str(_JOB["error"])
        scene.dwgreader_status = text[:180]
        entries.append({"role": "assistant", "text": text})
    else:
        scene.dwgreader_status = "Ответ ChatGPT"
        if not entries:
            entry = {"role": "assistant", "text": str(_JOB.get("text") or "")}
            if _JOB.get("image"):
                entry["image"] = _JOB["image"]
            entries.append(entry)
    card = scene_changes.take_result()
    if card:
        entries.append(card)
    items.extend(entries)
    save_log(scene, items)
    _JOB["done"] = False
    _LIVE["text"] = ""
    _LIVE["tool"] = ""
    _LIVE["summary"] = ""
    _redraw_view()
    return None


def _worker(
    token: str,
    model: str,
    history: list,
    user_text: str,
    user_images: list | None = None,
    api: dict | None = None,
) -> None:
    global _TRANSCRIPT
    _LIVE["text"] = ""
    _LIVE["tool"] = ""
    _LIVE["tools"] = []
    _LIVE["summary"] = ""
    _TRANSCRIPT = []
    try:
        text, image = complete_turn(token, model, history, user_text, user_images=user_images, api=api)
    except ChatError as error:
        _JOB["error"] = str(error)
        _JOB["text"] = ""
        _JOB["image"] = ""
        _JOB["tools"] = list(_LIVE.get("tools") or [])
        _JOB["entries"] = list(_TRANSCRIPT)
    except Exception as error:
        _JOB["error"] = str(error)
        _JOB["text"] = ""
        _JOB["image"] = ""
        _JOB["tools"] = list(_LIVE.get("tools") or [])
        _JOB["entries"] = list(_TRANSCRIPT)
    else:
        _JOB["error"] = ""
        _JOB["text"] = text
        _JOB["image"] = image
        _JOB["tools"] = list(_LIVE.get("tools") or [])
        _JOB["entries"] = list(_TRANSCRIPT)
    _JOB["done"] = True


class DWGREADER_OT_chat_send(Operator):
    bl_idname = "dwgreader.chat_send"
    bl_label = "Отправить"
    bl_description = "Отправить сообщение в ChatGPT. Инструменты выполняются в Blender"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        scene = context.scene
        if scene.dwgreader_chat_busy:
            return {"CANCELLED"}
        prefs = _prefs(context)
        reason = login_hint(prefs)
        if reason:
            scene.dwgreader_status = reason
            self.report({"WARNING"}, reason)
            return {"CANCELLED"}
        chatgpt = uses_chatgpt()
        token = ""
        api = None
        if chatgpt:
            token = chatgpt_auth.ensure_access()
            if not token:
                reason = chatgpt_auth.last_notice() or "Сессия ChatGPT не дала токен доступа"
                scene.dwgreader_status = reason[:180]
                self.report({"WARNING"}, reason[:180])
                return {"CANCELLED"}
        else:
            api = api_settings(prefs)
            if api is None:
                scene.dwgreader_status = "Нет адреса и модели API"
                self.report({"WARNING"}, "Нет адреса и модели API")
                return {"CANCELLED"}
        if not port_open():
            scene.dwgreader_status = "Сервер MCP не запущен"
            self.report({"WARNING"}, "Сервер MCP не запущен")
            return {"CANCELLED"}
        text = _draft_value(scene).strip()
        images = [path for path in _take_outgoing() if path]
        if not text and not images:
            return {"CANCELLED"}
        history = load_log(scene)
        history.append({"role": "user", "text": text, "images": images})
        save_log(scene, history)
        _clear_draft(scene)
        scene.dwgreader_status = "Запрос к ChatGPT…" if chatgpt else "Запрос к модели…"
        scene_changes.arm(scene)
        scene.dwgreader_chat_busy = True
        _CANCEL.clear()
        _JOB["done"] = False
        _JOB["tools"] = []
        _JOB["entries"] = []
        _LIVE["text"] = ""
        _LIVE["tool"] = ""
        _LIVE["tools"] = []
        _LIVE["summary"] = ""
        threading.Thread(
            target=_worker,
            args=(token, chosen_model(prefs), history[:-1], text, images, api),
            daemon=True,
        ).start()
        if not bpy.app.timers.is_registered(_finish_timer):
            bpy.app.timers.register(_finish_timer, first_interval=0.4)
        return {"FINISHED"}


class DWGREADER_OT_chat_stop(Operator):
    bl_idname = "dwgreader.chat_stop"
    bl_label = "Остановить"
    bl_description = "Прерывает текущий ответ ChatGPT"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        _CANCEL.set()
        context.scene.dwgreader_status = "Останавливаю ответ…"
        return {"FINISHED"}


class DWGREADER_OT_chat_line(Operator):
    bl_idname = "dwgreader.chat_line"
    bl_label = "Ещё строка"
    bl_description = "Добавляет строку в сообщение"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        block = _draft_block()
        last = block.lines[-1]
        block.cursor_set(len(block.lines) - 1, character=len(last.body))
        block.write("\n")
        return {"FINISHED"}


class DWGREADER_OT_chat_server(Operator):
    bl_idname = "dwgreader.chat_server"
    bl_label = "Запустить сервер"
    bl_description = "Запускает уже установленный MCP for Blender"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not mcp_operator_ready():
            return bpy.ops.dwgreader.chat_install()
        try:
            bpy.ops.blendermcp.start_server()
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        context.scene.dwgreader_status = "Сервер MCP запускается"
        forget_port()
        return {"FINISHED"}


def _start_mcp_server() -> str:
    """Пустая строка — сервер запущен или порт уже слушается."""
    try:
        bpy.ops.blendermcp.start_server()
    except Exception as error:
        if port_open():
            return ""
        return str(error)
    return ""


def _enable_mcp() -> str:
    try:
        bpy.ops.preferences.addon_refresh()
        bpy.ops.preferences.addon_enable(module="blender_mcp")
    except Exception as error:
        return str(error)
    return ""


class DWGREADER_OT_chat_install(Operator):
    bl_idname = "dwgreader.chat_install"
    bl_label = "Установить MCP"
    bl_description = "Ставит MCP for Blender из комплекта аддона и запускает сервер"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        addon = Path(__file__).resolve().parent
        if not deps_ready(addon / "vendor"):
            self.report({"ERROR"}, "Сначала нажмите «Установить библиотеки»")
            return {"CANCELLED"}
        if mcp_operator_ready():
            error = _start_mcp_server()
            if error:
                self.report({"ERROR"}, error)
                return {"CANCELLED"}
            forget_port()
            context.scene.dwgreader_status = "Сервер MCP запускается"
            self.report({"INFO"}, "Сервер MCP запускается")
            return {"FINISHED"}
        scripts = Path(bpy.utils.user_resource("SCRIPTS", path="addons", create=True))
        packaged = scripts / "blender_mcp"
        try:
            if packaged.is_dir() and (packaged / "__init__.py").is_file():
                error = _enable_mcp() or _start_mcp_server()
            else:
                source = addon / "mcp_for_blender" / "addon.py"
                if not source.is_file():
                    self.report({"ERROR"}, "В аддоне нет файла MCP")
                    return {"CANCELLED"}
                target = scripts / "blender_mcp.py"
                target.write_text(
                    compose_mcp_addon(source.read_text(encoding="utf-8"), addon / "vendor"),
                    encoding="utf-8",
                )
                error = _enable_mcp() or _start_mcp_server()
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        context.scene.dwgreader_status = "MCP for Blender установлен"
        forget_port()
        self.report({"INFO"}, "MCP for Blender установлен")
        return {"FINISHED"}


CLASSES = (
    DWGREADER_OT_chat_send,
    DWGREADER_OT_chat_stop,
    DWGREADER_OT_chat_line,
    DWGREADER_OT_chat_server,
    DWGREADER_OT_chat_install,
)


def register() -> None:
    from . import chat_view

    chat_view.register()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dwgreader_chat_log = StringProperty(default="[]", options={"HIDDEN"})
    bpy.types.Scene.dwgreader_chat_draft = StringProperty(name="Сообщение", default="")
    bpy.types.Scene.dwgreader_chat_busy = BoolProperty(default=False, options={"HIDDEN"})


def unregister() -> None:
    from . import chat_view

    chat_view.unregister()
    if bpy.app.timers.is_registered(_finish_timer):
        bpy.app.timers.unregister(_finish_timer)
    for name in ("dwgreader_chat_busy", "dwgreader_chat_draft", "dwgreader_chat_log"):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
