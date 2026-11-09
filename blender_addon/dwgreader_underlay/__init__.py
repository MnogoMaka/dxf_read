"""Аддон Blender: DWG/DXF → плоская подложка площадки.

Код разбора чертежа лежит в комплекте (пакет dxf_read). Считает его Python,
который идёт вместе с Blender. Папку проекта указывать не нужно.

Установка: zip этой папки → Edit → Preferences → Add-ons → Install from Disk.
После включения один раз нажмите «Установить библиотеки».
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList
from bpy.utils import previews

# Если библиотеки уже стоят, они видны до импорта модулей. Без них аддон всё равно включается.
_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def _reload_stale_modules() -> None:
    """Установка zip выгружает сам пакет, а подмодули старой версии остаются в sys.modules."""
    import importlib

    for name in (
        "placement_math",
        "install_support",
        "build_scene",
        "placement",
        "chatgpt_auth",
        "building_export",
        "mcp_bridge",
        "scene_changes",
        "chat_view",
        "scene_chat",
    ):
        module = sys.modules.get(f"{__name__}.{name}")
        if module is not None:
            importlib.reload(module)


_reload_stale_modules()

from . import building_export, chatgpt_auth, install_support, placement, scene_chat
from .build_scene import (
    SHEET_GAP_M,
    commit_underlay,
    drop_objects,
    frame_objects,
    remove_root,
    sheet_groups,
    sheet_size,
)
from .install_support import (
    ODA_DOWNLOAD_URL,
    ODA_INSTALL_NEED_UI,
    ODA_INSTALL_OK,
    PIP_PACKAGES,
    classify_block_reason,
    deps_ready,
    is_python_binary,
    is_windows_arm,
    oda_present,
    oda_search_roots,
    python_search_folders,
)


bl_info = {
    "name": "Подложка DWG",
    "author": "DWGREADER",
    "version": (1, 5, 18),
    "blender": (4, 0, 0),
    "location": "3D Viewport → Sidebar → Подложка",
    "description": "DXF, жилой дом и расстановка благоустройства в сцене",
    "category": "Import-Export",
}

MARKER = "UNDERLAY_JSON="
EMPTY_MARKER = "UNDERLAY_EMPTY="
LAYERS_MARKER = "LAYERS_JSON="
LAYER_CATEGORIES = (
    ("none", "None", "Тип не задан"),
    ("site_boundary", "Граница", "Граница участка"),
    ("building", "Здания", "Здания и пристройки"),
    ("pavement", "Покрытия", "Проезды, тротуары, площадки"),
    ("green", "Газоны", "Газоны и озеленение"),
    ("vegetation", "Посадки", "Деревья, кусты, цветники и клумбы"),
    ("furniture", "МАФ", "Скамьи, урны, оборудование"),
    ("fence", "Ограждения", "Заборы и ограды"),
)
CATEGORY_IDS = {item[0] for item in LAYER_CATEGORIES}
GEOMETRY_SOURCES = (
    ("fills", "Заливки", "Полигоны из штриховок и заливок"),
    ("curves", "Контуры", "Полигоны из полилиний и других контуров"),
    ("both", "Заливки и контуры", "Оба источника сразу"),
)
GEOMETRY_PRESETS = GEOMETRY_SOURCES + (
    ("per_layer", "По слоям", "У каждого слоя свой способ отрисовки"),
)
LAYER_GEOMETRY = (
    ("fills", "Заливки", "Полигоны из штриховок и заливок"),
    ("curves", "Контуры", "Полигоны из замкнутых контуров"),
    ("both", "Оба", "Заливки и контуры"),
)
ORIGIN_MODES = (
    ("mass", "Центр масс площадок", "Ноль сцены в центре масс площадок участка"),
    ("bbox", "Центр габарита", "Ноль сцены в центре габарита построенной геометрии"),
    ("zero", "Начало чертежа (0,0)", "Координаты как в AutoCAD. Далёкие от нуля координаты Blender показывает с дрожанием"),
)
CONTOUR_ARRANGEMENT_MODES = (
    ("layer", "Отдельно по слоям", "Сначала замыкать линии каждого слоя отдельно. Рекомендуется"),
    ("combined", "Совместно", "Линии разных выбранных слоёв могут вместе образовать полигон"),
)
SPLIT_MODES = (
    ("layer", "Один объект на слой", "Все полигоны слоя AutoCAD в одном объекте"),
    ("polygon", "Каждый полигон отдельно", "Здание_1, Здание_2… Касающиеся полигоны — один объект"),
    ("gap", "Группы по зазору", "Полигоны ближе зазора вместе, дальние — отдельными объектами"),
)
SCREENS = (
    ("home", "Инструменты", ""),
    ("import", "Импорт DXF", ""),
    ("place", "Озеленение и МАФ", ""),
    ("building", "Здание", ""),
    ("chat", "Чат", ""),
)
JUNK_TOKENS = (
    "НОМ._ПЕРА, LABEL_, POINTS_GRID, POINTS_GRADE, NULL_WORKS, "
    "ИНТЕРЬЕР, ГОРИЗОНТАЛ, ВЕРТИКАЛК, DEFPOINTS, ЭКСПЛИКАЦ, "
    "ВЫНОСК, ШТАМП, C-ROAD-, ТПП_РАЗМЕР, ТПП_ТЕКСТ, "
    "РАЗМЕР, РАЗМЕТК, ОСИ, ОСЕВ, OSEV, "
    "ТЕКСТ, PDF , PDF_, "
    "ВИДОВОЙ ЭКРАН, ОДД, ДВИЖЕНИ, "
    "ХАРАКТЕРИСТИКА, ТРАССА, ВЕНТИЛЯТОР, КОЛОД, СЕТИ, "
    "ГРАНИЦА УЛИЦ, ГРАНИЦА ЗАКАЗ, ГРАНИЦА РАСТИТЕЛЬНОСТ, "
    "СИТУАЦИОН, КАРТОГРАМ, КАТОГРАМ, ЛЕГЕНД, "
    "REV_А-, REV_A-, VOLUME, TABLE, GRID"
)


def _addon_dir() -> Path:
    return Path(__file__).resolve().parent


def _vendor_dir() -> Path:
    return _addon_dir() / "vendor"


def _engine_dir() -> Path:
    """Каталог, внутри которого лежит пакет dxf_read."""
    addon_dir = _addon_dir()
    if (addon_dir / "dxf_read" / "__init__.py").is_file():
        return addon_dir
    repo = addon_dir.parents[2]
    if (repo / "dxf_read" / "__init__.py").is_file():
        return repo
    raise FileNotFoundError("В аддоне нет пакета dxf_read")


def _python_folders() -> list[Path]:
    binary = Path(getattr(bpy.app, "binary_path", "") or "")
    return python_search_folders(Path(sys.prefix), binary if binary else None)


def _blender_python() -> Path:
    found: list[Path] = []
    seen: set[Path] = set()
    for folder in _python_folders():
        if not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            resolved = candidate.resolve()
            if resolved in seen or not is_python_binary(candidate):
                continue
            seen.add(resolved)
            found.append(candidate)
    found.sort(key=lambda path: (not path.name.lower().startswith("python3."), path.name.lower()))
    if found:
        return found[0]
    raise FileNotFoundError(
        "Не найден Python рядом с Blender. Библиотеки ставятся его pip."
    )


def _oda_roots() -> list[Path]:
    return oda_search_roots(sys.platform, Path.home(), os.environ.get("LOCALAPPDATA", ""))


def _oda_installed(prefs=None) -> bool:
    """Есть ли ODA File Converter: путь из настроек или стандартные папки системы."""
    if prefs is not None and prefs.oda_converter.strip():
        return Path(bpy.path.abspath(prefs.oda_converter.strip())).is_file()
    return oda_present(_oda_roots(), shutil.which("ODAFileConverter") or "")


def _deps_ready() -> bool:
    return deps_ready(_vendor_dir())


def _run_blender_python(python: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        [str(python), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )


def _ensure_pip(python: Path) -> str:
    """Пустая строка — pip готов. Иначе текст ошибки для панели."""
    probe = _run_blender_python(python, ["-m", "pip", "--version"])
    if probe.returncode == 0:
        return ""
    setup = _run_blender_python(python, ["-m", "ensurepip", "--upgrade"])
    if setup.returncode != 0:
        detail = (setup.stderr or setup.stdout or "ensurepip не сработал").strip()
        return f"В Python Blender нет pip. {detail[:160]}"
    probe = _run_blender_python(python, ["-m", "pip", "--version"])
    if probe.returncode == 0:
        return ""
    return "В Python Blender нет pip. Не удалось вызвать ensurepip."


def _open_oda_download() -> None:
    import webbrowser

    webbrowser.open(ODA_DOWNLOAD_URL)


def _addon_prefs(context):
    package = __package__ or __name__
    addon = context.preferences.addons.get(package)
    return addon.preferences if addon is not None else None


def _tokens(text: str) -> list[str]:
    items: list[str] = []
    for part in text.replace(";", ",").replace("\n", ",").split(","):
        token = part.strip().upper().replace("Ё", "Е")
        if token:
            items.append(token)
    return items


def _documents_converter() -> Path:
    """Папка «Документы/converter», с учётом переноса Documents в OneDrive."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        folder_id = GUID(
            0xFDD39AD0, 0x238F, 0x46AF,
            (wintypes.BYTE * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
        )
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        pointer = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)) == 0:
            allocated = ctypes.cast(pointer, ctypes.c_void_p).value
            text = pointer.value or ""
            if allocated:
                ole32.CoTaskMemFree(allocated)
            if text:
                return Path(text) / "converter"
    return Path.home() / "Documents" / "converter"


_FAULT_LOG = None


def _arm_fault_log() -> None:
    """Следующий нативный сбой оставляет стек Python в профиле, а не только в temp."""
    global _FAULT_LOG
    if _FAULT_LOG is not None:
        return
    import faulthandler

    try:
        folder = chatgpt_auth.storage_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handle = open(folder / "fault.log", "ab", buffering=0)
        faulthandler.enable(file=handle, all_threads=True)
    except Exception:
        return
    _FAULT_LOG = handle


_arm_fault_log()
DEFAULT_OUTPUT = str(_documents_converter())


def _output_dir(prefs, source: Path) -> Path:
    raw = prefs.output_dir.strip()
    if raw:
        path = Path(bpy.path.abspath(raw))
        if not path.is_absolute():
            path = source.parent / path
    else:
        path = Path(DEFAULT_OUTPUT)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings_payload(prefs, source: Path) -> dict:
    oda = ""
    if prefs.oda_converter.strip():
        oda = bpy.path.abspath(prefs.oda_converter.strip())
    return {
        "generic_fill_associate_m": float(prefs.generic_fill_associate_m),
        "min_arrangement_area_m2": float(prefs.min_arrangement_area_m2),
        "flattening_distance_m": float(prefs.flattening_distance_m),
        "contour_snap_tolerance_m": float(prefs.contour_snap_tolerance_m),
        "contour_arrangement_mode": str(prefs.contour_arrangement_mode),
        "contour_from_fills": bool(prefs.contour_from_fills),
        "cut_vegetation": bool(prefs.cut_vegetation),
        "omit_symbols": bool(prefs.omit_symbols),
        "omit_fences": bool(prefs.omit_fences),
        "scene_scale": float(prefs.scene_scale),
        "origin_mode": str(prefs.origin_mode),
        "batch_size": int(prefs.batch_size),
        "llm_timeout": float(prefs.llm_timeout),
        "fill_polyline_tokens": _tokens(prefs.fill_polyline_tokens),
        "junk_layer_tokens": _tokens(prefs.junk_layer_tokens),
        "llm_model": prefs.llm_model.strip(),
        "llm_base_url": prefs.llm_base_url.strip(),
        "oda_converter": oda,
        "output_dir": str(_output_dir(prefs, source)),
    }


def _python_env(prefs=None, force_api: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_vendor_dir()), str(_engine_dir()), env.get("PYTHONPATH", "")) if part
    )
    env.pop("DWGREADER_CHATGPT_ACCESS_TOKEN", None)
    env.pop("DWGREADER_CHATGPT_MODEL", None)
    env.pop("DWGREADER_FORCE_API", None)
    if prefs is None:
        return env
    key = (prefs.llm_api_key or "").strip()
    if key:
        env["LLM_API_KEY"] = key
    else:
        env.pop("LLM_API_KEY", None)
    if force_api:
        env["DWGREADER_FORCE_API"] = "1"
        return env
    env.update(chatgpt_auth.child_credentials(getattr(prefs, "chatgpt_model", "")))
    return env


def _chatgpt_block_reason(prefs, force_api: bool = False) -> str:
    """Пустая строка — разбор можно запускать. Иначе причина для панели."""
    profile = chatgpt_auth.public_profile()
    sharing = bool(profile and profile.get("sharing"))
    slug = (getattr(prefs, "chatgpt_model", "") or chatgpt_auth.saved_model() or "").strip()
    has_access = bool(sharing and not force_api and chatgpt_auth.ensure_access())
    return classify_block_reason(
        sharing=sharing,
        slug=slug,
        has_access=has_access,
        access_notice=chatgpt_auth.last_notice(),
        api_url=str(getattr(prefs, "llm_base_url", "") or ""),
        api_model=str(getattr(prefs, "llm_model", "") or ""),
        force_api=force_api,
    )


def _redraw(context) -> None:
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def _chatgpt_model_items(self, context):
    models = chatgpt_auth.cached_models()
    items = [
        (item["slug"], item.get("display_name") or item["slug"], "")
        for item in models
        if item.get("slug")
    ]
    current = chatgpt_auth.saved_model()
    if current and current != "none" and current not in {item[0] for item in items}:
        items.insert(0, (current, current, ""))
    if not items:
        items = [("none", "Модели появятся после входа", "")]
    return items


def _on_chatgpt_model(self, context):
    chatgpt_auth.remember_model(getattr(self, "chatgpt_model", ""))


def _draw_chatgpt(layout, prefs) -> None:
    waiting = chatgpt_auth.active_login()
    if waiting is not None:
        box = layout.box()
        box.label(text=(waiting.notice or "Завершите вход в браузере"), icon="URL")
        actions = box.row(align=True)
        actions.operator("dwgreader.chatgpt_copy_link", text="Скопировать ссылку")
        actions.operator("dwgreader.chatgpt_cancel", text="Отмена")
        return
    profile = chatgpt_auth.public_profile()
    if profile is None:
        failure = chatgpt_auth.login_failure()
        if failure:
            layout.label(text=failure)
        layout.operator("dwgreader.chatgpt_login", text="Продолжить с ChatGPT")
        return
    box = layout.box()
    box.label(text=profile["email"] or "ChatGPT", icon="USER")
    if profile["sharing"]:
        box.label(text="Используется план ChatGPT")
        if prefs is not None:
            row = box.row(align=True)
            row.prop(prefs, "chatgpt_model", text="Модель")
            row.operator("dwgreader.chatgpt_models", text="", icon="FILE_REFRESH")
        box.operator("dwgreader.chatgpt_usage", text="Расход плана")
    else:
        box.label(text="План ChatGPT не разрешён, классификация пойдёт через API", icon="INFO")
    actions = box.row(align=True)
    actions.operator("dwgreader.chatgpt_login", text="Переподключить")
    other = actions.operator("dwgreader.chatgpt_login", text="Другой аккаунт")
    other.another = True
    actions.operator("dwgreader.chatgpt_logout", text="Выйти")


class DWGREADER_AddonPreferences(AddonPreferences):
    bl_idname = __package__ or __name__

    oda_converter: StringProperty(
        name="ODA File Converter",
        description="Файл ODA File Converter. Пусто — искать в Program Files или /Applications. Нужен только для DWG",
        subtype="FILE_PATH",
        default="",
    )
    output_dir: StringProperty(
        name="Папка результатов",
        description="Куда писать DXF и JSON. По умолчанию — Документы/converter",
        subtype="DIR_PATH",
        default=DEFAULT_OUTPUT,
    )
    llm_model: StringProperty(
        name="Модель",
        description="Имя модели на OpenAI-совместимом сервере",
        default="",
    )
    llm_base_url: StringProperty(
        name="Адрес модели",
        default="",
    )
    llm_api_key: StringProperty(
        name="Ключ API",
        subtype="PASSWORD",
        default="",
    )
    llm_timeout: FloatProperty(
        name="Таймаут запроса, с",
        description="Сколько ждать ответ модели на один пакет слоёв",
        default=300.0,
        min=10.0,
        max=3600.0,
    )
    batch_size: IntProperty(
        name="Слоёв в пакете",
        description="Сколько имён слоёв отправлять модели за один запрос",
        default=600,
        min=1,
        max=2000,
    )
    layer_part_gap_m: FloatProperty(
        name="Зазор частей слоя, м",
        description="Полигоны одного слоя дальше этого зазора становятся отдельными объектами. Ближе остаются вместе. Учитывается при следующей сборке подложки",
        default=10.0,
        min=0.0,
        soft_max=500.0,
        precision=1,
    )
    generic_fill_associate_m: FloatProperty(
        name="Привязка заливки слоя 0, м",
        description="SOLID на слое 0 относится к зданию, если центр внутри контура и не дальше этого допуска",
        default=15.0,
        min=0.0,
        soft_max=100.0,
    )
    min_arrangement_area_m2: FloatProperty(
        name="Минимальная площадь, м²",
        description="Мельче этого заливки не попадают в подложку",
        default=0.05,
        min=0.0,
        soft_max=10.0,
        precision=3,
    )
    flattening_distance_m: FloatProperty(
        name="Спрямление дуг, м",
        description="Допуск ломаной при разборе дуг и сплайнов",
        default=0.02,
        min=0.001,
        soft_max=1.0,
        precision=3,
    )
    contour_snap_tolerance_m: FloatProperty(
        name="Соединение контуров, м",
        description="Концы линий не дальше этого расстояния считаются одной точкой",
        default=0.02,
        min=0.0,
        soft_max=1.0,
        precision=3,
    )
    contour_arrangement_mode: EnumProperty(
        name="Сборка контуров",
        description="Объединять линии отдельно по слоям или совместно",
        items=CONTOUR_ARRANGEMENT_MODES,
        default="layer",
    )
    contour_from_fills: BoolProperty(
        name="Контуры из заливок",
        description="В режиме «Контуры» граница штриховки тоже собирается в полигон, даже если отдельной линии на слое нет",
        default=False,
    )
    omit_symbols: BoolProperty(
        name="Убрать отметки МАФ и посадок",
        description="Контуры кустов, деревьев, цветников и лавочек с чертежа не попадают в сцену. Газоны, покрытия и здания остаются, файл чертежа не меняется",
        default=True,
    )
    omit_fences: BoolProperty(
        name="Убрать ограды",
        description="Слои оград, заборов и ограждений не попадают в подложку и не режут газон и покрытия",
        default=True,
    )
    cut_vegetation: BoolProperty(
        name="Вырезать полигоны под МАФ и посадками",
        description="Выключено: слои типов «МАФ» и «Посадки» лежат поверх целого газона и покрытия. Включено: их контур вырезает землю",
        default=False,
    )
    scene_scale: FloatProperty(
        name="Масштаб в сцену",
        description="Во сколько раз умножить координаты чертежа. 1 — метры чертежа равны метрам сцены",
        default=1.0,
        min=0.000001,
        soft_max=1000.0,
        precision=4,
    )
    origin_mode: EnumProperty(
        name="Начало координат",
        description="Какая точка чертежа станет нулём сцены",
        items=ORIGIN_MODES,
        default="mass",
    )
    split_mode: EnumProperty(
        name="Разбиение на объекты",
        description="Как делить полигоны одного слоя AutoCAD на объекты Blender",
        items=SPLIT_MODES,
        default="gap",
    )
    by_type_collections: BoolProperty(
        name="Раскладывать по коллекциям типов",
        description="Включено: Тип › Слой › объекты. Выключено: коллекции слоёв прямо в корне подложки",
        default=True,
    )
    fill_polyline_tokens: StringProperty(
        name="Полилинии-заливки",
        description="Замкнутая полилиния становится пятном, если в имени слоя есть один из фрагментов. Через запятую",
        default="ЗАЛИВ, ЗАПОЛН, ПЯТН",
    )
    junk_layer_tokens: StringProperty(
        name="Служебные слои",
        description="Слой отбрасывается, если в хвосте имени есть один из фрагментов. Через запятую",
        default=JUNK_TOKENS,
    )
    chatgpt_model: EnumProperty(
        name="Модель ChatGPT",
        description="Модель плана ChatGPT для классификации слоёв",
        items=_chatgpt_model_items,
        update=_on_chatgpt_model,
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.operator("dwgreader.install_deps", icon="IMPORT")
        if chatgpt_auth.public_profile() is None:
            row.operator("dwgreader.chatgpt_login", text="Продолжить с ChatGPT")
        else:
            _draw_chatgpt(layout, self)
        layout.prop(self, "oda_converter")
        if not _oda_installed(self):
            layout.operator("dwgreader.install_oda", icon="IMPORT")
        layout.prop(self, "output_dir")

        services = layout.box()
        services.label(text="Запасной API, если ChatGPT не подключён")
        services.prop(self, "llm_model")
        services.prop(self, "llm_base_url")
        services.prop(self, "llm_api_key")
        services.prop(self, "llm_timeout")
        services.prop(self, "batch_size")

        geometry = layout.box()
        geometry.label(text="Геометрия")
        geometry.prop(self, "scene_scale")
        geometry.prop(self, "origin_mode")
        _draw_geometry(geometry, self)
        _draw_split(geometry, self)

        tokens = layout.box()
        tokens.label(text="Имена слоёв")
        tokens.prop(self, "fill_polyline_tokens")
        tokens.prop(self, "junk_layer_tokens")


def _draw_geometry(layout, prefs) -> None:
    layout.prop(prefs, "generic_fill_associate_m")
    layout.prop(prefs, "min_arrangement_area_m2")
    layout.prop(prefs, "flattening_distance_m")
    layout.prop(prefs, "contour_snap_tolerance_m")
    layout.prop(prefs, "contour_arrangement_mode")


def _draw_split(layout, prefs) -> None:
    layout.prop(prefs, "split_mode")
    if prefs.split_mode == "gap":
        layout.prop(prefs, "layer_part_gap_m", text="Зазор, м")
    layout.prop(prefs, "by_type_collections")


class DWGREADER_OT_install_deps(Operator):
    bl_idname = "dwgreader.install_deps"
    bl_label = "Установить библиотеки"
    bl_description = "Ставит ezdxf, shapely, earcut, matplotlib и requests в папку аддона через Python Blender"

    _timer = None
    _proc = None
    _thread = None
    _state: dict | None = None

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    def invoke(self, context, event):
        try:
            python = _blender_python()
        except FileNotFoundError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        missing = _ensure_pip(python)
        if missing:
            self.report({"ERROR"}, missing)
            return {"CANCELLED"}
        vendor = _vendor_dir()
        vendor.mkdir(parents=True, exist_ok=True)
        command = [
            str(python), "-m", "pip", "install", "--upgrade",
            "--target", str(vendor), *PIP_PACKAGES,
        ]
        return _start_process(self, context, command, cwd=vendor.parent, env=os.environ.copy(), title="Установка библиотек…")

    def modal(self, context, event):
        return _poll_process(
            self, context, event,
            on_ok="Библиотеки установлены",
            on_err="Не удалось установить библиотеки",
        )

    def _stop(self, context, kill: bool) -> None:
        _stop_process(self, context, kill)


class DWGREADER_OT_chatgpt_login(Operator):
    bl_idname = "dwgreader.chatgpt_login"
    bl_label = "Продолжить с ChatGPT"
    bl_description = "Вход через браузер. Классификация слоёв пойдёт через план ChatGPT"

    _timer = None
    _thread = None
    _job: chatgpt_auth.LoginJob | None = None
    another: BoolProperty(name="Другой аккаунт", default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    def invoke(self, context, event):
        if not _deps_ready():
            self.report({"ERROR"}, "Сначала нажмите «Установить библиотеки»")
            return {"CANCELLED"}
        job = chatgpt_auth.LoginJob()
        job.another = bool(self.another)
        self._job = job
        chatgpt_auth.set_active_login(job)
        self._thread = threading.Thread(target=job.run, daemon=True)
        self._thread.start()
        context.window_manager.dwgreader_busy = True
        context.scene.dwgreader_status = "Вход в ChatGPT…"
        self._timer = context.window_manager.event_timer_add(0.4, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            self._finish(context, cancel=True)
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        job = self._job
        if job is not None and job.url and not job.browser_started:
            opened = chatgpt_auth.launch_pending_browser(job)
            if opened is False:
                context.scene.dwgreader_status = "Браузер не открылся. Скопируйте ссылку"
            elif job.notice:
                context.scene.dwgreader_status = job.notice[:180]
        elif job is not None and job.notice and not job.done:
            context.scene.dwgreader_status = job.notice[:180]
        if job is None or not job.done:
            _redraw(context)
            return {"RUNNING_MODAL"}
        self._finish(context, cancel=False)
        if job.email and not job.error:
            self._remember_model(context, job)
            self.report({"INFO"}, f"Вход выполнен: {job.email}")
            context.scene.dwgreader_status = "Используется план ChatGPT" if job.sharing else "План ChatGPT не разрешён"
            return {"FINISHED"}
        if job.email and job.error:
            self._remember_model(context, job)
            self.report({"WARNING"}, job.error)
            context.scene.dwgreader_status = job.error[:180]
            return {"FINISHED"}
        self.report({"ERROR"}, job.error or "Не удалось войти в ChatGPT")
        context.scene.dwgreader_status = (job.error or "Не удалось войти в ChatGPT")[:180]
        return {"CANCELLED"}

    def _remember_model(self, context, job: chatgpt_auth.LoginJob) -> None:
        prefs = _addon_prefs(context)
        if prefs is None or not job.models:
            return
        slugs = [item["slug"] for item in job.models]
        saved = chatgpt_auth.saved_model()
        if saved in slugs:
            prefs.chatgpt_model = saved
        elif prefs.chatgpt_model not in slugs:
            prefs.chatgpt_model = slugs[0]
        chatgpt_auth.remember_model(prefs.chatgpt_model)

    def _finish(self, context, cancel: bool) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if cancel and self._job is not None:
            self._job.cancel()
        if chatgpt_auth.active_login() is self._job or self._job is not None:
            chatgpt_auth.set_active_login(None)
        context.window_manager.dwgreader_busy = False
        _redraw(context)


class DWGREADER_OT_chatgpt_copy_link(Operator):
    bl_idname = "dwgreader.chatgpt_copy_link"
    bl_label = "Скопировать ссылку"
    bl_description = "Копирует ссылку входа, если браузер не открылся"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        url = chatgpt_auth.login_url()
        if not url:
            self.report({"WARNING"}, "Ссылка входа ещё не готова")
            return {"CANCELLED"}
        context.window_manager.clipboard = url
        self.report({"INFO"}, "Ссылка входа скопирована")
        return {"FINISHED"}


class DWGREADER_OT_chatgpt_cancel(Operator):
    bl_idname = "dwgreader.chatgpt_cancel"
    bl_label = "Отмена"
    bl_description = "Прерывает ожидание входа в ChatGPT"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        job = chatgpt_auth.active_login()
        if job is not None:
            job.cancel()
        context.scene.dwgreader_status = "Вход отменён"
        return {"FINISHED"}


class DWGREADER_OT_chatgpt_usage(Operator):
    bl_idname = "dwgreader.chatgpt_usage"
    bl_label = "Расход плана"
    bl_description = "Открывает страницу расхода ChatGPT"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not chatgpt_auth._open_browser(chatgpt_auth.USAGE_URL):
            self.report({"WARNING"}, "Не удалось открыть браузер")
            return {"CANCELLED"}
        return {"FINISHED"}


class DWGREADER_OT_chatgpt_models(Operator):
    bl_idname = "dwgreader.chatgpt_models"
    bl_label = "Обновить модели"
    bl_description = "Заново запрашивает список моделей плана ChatGPT"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        threading.Thread(target=chatgpt_auth.refresh_models, daemon=True).start()
        self.report({"INFO"}, "Список моделей обновляется")
        return {"FINISHED"}


class DWGREADER_OT_chatgpt_logout(Operator):
    bl_idname = "dwgreader.chatgpt_logout"
    bl_label = "Выйти"
    bl_description = "Отзывает сессию ChatGPT на этом компьютере"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    def execute(self, context):
        try:
            chatgpt_auth.sign_out()
        except Exception:
            self.report({"WARNING"}, "Локальная сессия очищена, отзыв на сервере не подтверждён")
        else:
            self.report({"INFO"}, "Выход из ChatGPT выполнен")
        context.scene.dwgreader_status = "Классификация через API"
        _redraw(context)
        return {"FINISHED"}


class DWGREADER_OT_install_oda(Operator):
    bl_idname = "dwgreader.install_oda"
    bl_label = "Установить ODA File Converter"
    bl_description = "Скачивает установщик ODA File Converter. Если сайт блокирует автозагрузку — открывает страницу"

    _timer = None
    _proc = None
    _thread = None
    _state: dict | None = None

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    def invoke(self, context, event):
        if _oda_installed(_addon_prefs(context)):
            self.report({"INFO"}, "ODA File Converter уже установлен")
            return {"CANCELLED"}
        if sys.platform != "win32" or is_windows_arm(platform.machine()):
            _open_oda_download()
            if sys.platform == "darwin":
                self.report(
                    {"INFO"},
                    "Открыта загрузка ODA. Поставьте приложение в /Applications и запустите импорт снова",
                )
            elif sys.platform == "win32":
                self.report(
                    {"INFO"},
                    "Открыта загрузка ODA. На ARM Windows укажите путь к конвертеру в настройках",
                )
            else:
                self.report(
                    {"INFO"},
                    "Открыта загрузка ODA. Укажите путь к конвертеру в настройках",
                )
            return {"FINISHED"}
        try:
            python = _blender_python()
        except FileNotFoundError:
            _open_oda_download()
            self.report(
                {"INFO"},
                "Открыта загрузка ODA. Поставьте и укажите путь в настройках",
            )
            return {"FINISHED"}
        log_dir = Path(DEFAULT_OUTPUT)
        log_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(python), str(_addon_dir() / "install_support.py"),
            "oda-install", str(log_dir),
        ]
        return _start_process(
            self, context, command, cwd=log_dir, env=os.environ.copy(),
            title="Установка ODA File Converter…",
            log_path=log_dir / "dwgreader_oda_install.log",
        )

    def modal(self, context, event):
        if event.type == "ESC":
            self._stop(context, kill=True)
            context.scene.dwgreader_status = "Отменено"
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        proc = self._proc
        if proc is None or proc.poll() is None:
            context.scene.dwgreader_status = str((self._state or {}).get("status") or "")
            _redraw(context)
            return {"RUNNING_MODAL"}
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        code = proc.returncode
        self._stop(context, kill=False)
        if code == ODA_INSTALL_OK or _oda_installed(_addon_prefs(context)):
            context.scene.dwgreader_status = "ODA File Converter установлен"
            self.report({"INFO"}, "ODA File Converter установлен")
            return {"FINISHED"}
        _open_oda_download()
        if code == ODA_INSTALL_NEED_UI:
            context.scene.dwgreader_status = "Подтвердите установщик ODA"
            self.report(
                {"INFO"},
                "Запущен установщик ODA. Подтвердите права администратора. Если окна нет — поставьте с открытой страницы",
            )
        else:
            context.scene.dwgreader_status = "Открыта загрузка ODA"
            self.report(
                {"INFO"},
                "Сайт ODA блокирует автоскачивание. Поставьте с открытой страницы и укажите путь в настройках",
            )
        return {"FINISHED"}

    def _stop(self, context, kill: bool) -> None:
        _stop_process(self, context, kill)


CATEGORY_LABELS = {item[0]: item[1] for item in LAYER_CATEGORIES}
UNDERLAY_ROOT = "SITE_UNDERLAY"
TILE_SCALE = 7.0
_LAYER_SYNC = False
_BUILD_STARTED = 0.0
_PREVIEWS = None
_PING = {"busy": False, "ok": False, "text": "", "stamp": 0.0, "fresh": False}


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    value = abs(int(count))
    if value % 10 == 1 and value % 100 != 11:
        word = forms[0]
    elif 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        word = forms[1]
    else:
        word = forms[2]
    return f"{count} {word}"


def _layers_text(count: int) -> str:
    return _plural(count, ("слой", "слоя", "слоёв"))


def _objects_text(count: int) -> str:
    return _plural(count, ("объект", "объекта", "объектов"))


def _load_previews():
    """Иконки грузятся до отрисовки панели. load() из draw() на части видеокарт гасит Blender."""
    if _PREVIEWS is None:
        return None
    folder = _addon_dir() / "icons"
    if folder.is_dir():
        for path in folder.glob("*.png"):
            if _PREVIEWS.get(path.stem) is not None:
                continue
            try:
                _PREVIEWS.load(path.stem, str(path), "IMAGE")
            except Exception:
                continue
    try:
        _redraw(bpy.context)
    except Exception:
        pass
    return None


def _preview(key: str) -> int:
    if _PREVIEWS is None:
        return 0
    item = _PREVIEWS.get(key)
    if item is None:
        return 0
    return item.icon_id


def _layer_count(item) -> int:
    if item.geometry == "fills":
        return int(item.fills)
    if item.geometry == "curves":
        return int(item.curves)
    return int(item.fills) + int(item.curves)


def _on_layer_geometry(self, context):
    global _LAYER_SYNC
    if _LAYER_SYNC or context is None or context.scene is None:
        return
    scene = context.scene
    if scene.dwgreader_geometry != "per_layer":
        _LAYER_SYNC = True
        try:
            scene.dwgreader_geometry = "per_layer"
        finally:
            _LAYER_SYNC = False


def _on_geometry_preset(self, context):
    global _LAYER_SYNC
    if _LAYER_SYNC:
        return
    scene = self
    if scene.dwgreader_geometry == "per_layer":
        return
    _LAYER_SYNC = True
    try:
        for item in scene.dwgreader_selected:
            item.geometry = scene.dwgreader_geometry
    finally:
        _LAYER_SYNC = False


def _layer_visible(item, scene) -> bool:
    if not item.recognized and not scene.dwgreader_show_unknown:
        return False
    text = scene.dwgreader_filter.strip().lower()
    if not text:
        return True
    label = CATEGORY_LABELS.get(item.category, "").lower()
    return text in item.layer_name.lower() or text in label


def _layer_summary(scene) -> dict:
    items = list(scene.dwgreader_selected)
    recognized = [item for item in items if item.recognized]
    unknown = [item for item in items if not item.recognized]
    chosen = [item for item in items if item.build]
    return {
        "layers": len(items),
        "objects": sum(_layer_count(item) for item in items),
        "recognized": len(recognized),
        "recognized_objects": sum(_layer_count(item) for item in recognized),
        "types": len({item.category for item in recognized if item.category != "none"}),
        "unknown": len(unknown),
        "unknown_objects": sum(_layer_count(item) for item in unknown),
        "chosen": len(chosen),
        "chosen_objects": sum(_layer_count(item) for item in chosen),
        "chosen_types": len({item.category for item in chosen}),
    }


def _layer_memory(scene) -> dict:
    try:
        data = json.loads(scene.dwgreader_layer_memory or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _remember_layers(scene) -> None:
    source = str(scene.dwgreader_classified or "")
    if not source or not len(scene.dwgreader_selected):
        return
    memory = _layer_memory(scene)
    memory[source] = {
        item.layer_name: [bool(item.build), item.category, item.geometry]
        for item in scene.dwgreader_selected
    }
    scene.dwgreader_layer_memory = json.dumps(memory, ensure_ascii=False)


class DWGREADER_OT_pick_file(Operator):
    bl_idname = "dwgreader.pick_file"
    bl_label = "Выбрать чертёж"
    bl_description = "DWG или DXF"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.dwg;*.dxf;*.DWG;*.DXF", options={"HIDDEN"})

    def execute(self, context):
        scene = context.scene
        if scene.dwgreader_source != self.filepath:
            _remember_layers(scene)
            scene.dwgreader_selected.clear()
            scene.dwgreader_ignored.clear()
            scene.dwgreader_classified = ""
        scene.dwgreader_source = self.filepath
        scene.dwgreader_screen = "import"
        scene.dwgreader_step = 1
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class DWGREADER_LayerItem(PropertyGroup):
    layer_name: StringProperty(name="Слой")
    category: EnumProperty(name="Тип", items=LAYER_CATEGORIES, default="none")
    build: BoolProperty(name="Строить", description="Строить этот слой", default=True)
    recognized: BoolProperty(name="Распознан", default=True)
    geometry: EnumProperty(
        name="Отрисовка",
        description="Из чего строить полигоны слоя",
        items=LAYER_GEOMETRY,
        default="fills",
        update=_on_layer_geometry,
    )
    fills: IntProperty(name="Заливок", default=0)
    curves: IntProperty(name="Контуров", default=0)
    entities: IntProperty(name="Объектов в DXF", default=0)


def _layer_columns(layout):
    name = layout.split(factor=0.4, align=True)
    rest = name.split(factor=0.5, align=True)
    tail = rest.split(factor=0.66, align=True)
    return name, rest, tail


class DWGREADER_UL_layers(UIList):
    bl_idname = "DWGREADER_UL_layers"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "build", text="")
        body = row.row(align=True)
        body.active = item.build
        name, rest, tail = _layer_columns(body)
        name.label(text=item.layer_name)
        rest.prop(item, "category", text="")
        tail.prop(item, "geometry", text="")
        tail.label(text=str(item.entities if item.entities else _layer_count(item)))

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        scene = context.scene
        flags = [self.bitflag_filter_item if _layer_visible(item, scene) else 0 for item in items]
        return flags, []


class DWGREADER_UL_place_layers(UIList):
    """Слои, которые есть в собранной подложке."""

    bl_idname = "DWGREADER_UL_place_layers"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.split(factor=0.65)
        row.label(text=item.layer_name, icon="MESH_PLANE")
        row.label(text=CATEGORY_LABELS.get(item.category, ""))

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        present = placement.scene_zone_names()
        flags = [
            self.bitflag_filter_item
            if item.layer_name in present or item.layer_name[:63] in present
            else 0
            for item in items
        ]
        return flags, []


def _on_source_path(self, context):
    """Смена файла сбрасывает разобранные слои. Поле пути само открывает выбор файла."""
    if self.dwgreader_classified and self.dwgreader_classified != self.dwgreader_source:
        self.dwgreader_selected.clear()
        self.dwgreader_ignored.clear()
        self.dwgreader_classified = ""
        self.dwgreader_step = 1


def _drawing_path(context) -> Path | None:
    raw = context.scene.dwgreader_source.strip()
    if not raw:
        return None
    path = Path(bpy.path.abspath(raw))
    if path.suffix.lower() not in {".dwg", ".dxf"} or not path.is_file():
        return None
    return path


def _launch_pipeline(operator, context, extra: list[str], title: str, mode: str, force_api: bool = False):
    prefs = _addon_prefs(context)
    if prefs is None:
        operator.report({"ERROR"}, "Настройки аддона недоступны")
        return {"CANCELLED"}
    if not _deps_ready():
        operator.report({"ERROR"}, "Сначала нажмите «Установить библиотеки»")
        return {"CANCELLED"}
    source = _drawing_path(context)
    if source is None:
        operator.report({"ERROR"}, "Выберите существующий файл .dxf или .dwg")
        return {"CANCELLED"}
    if source.suffix.lower() == ".dwg" and not _oda_installed(prefs):
        operator.report({"ERROR"}, "Для DWG нужен ODA File Converter. Нажмите «Установить ODA File Converter»")
        return {"CANCELLED"}
    try:
        python = _blender_python()
        engine = _engine_dir()
    except FileNotFoundError as exc:
        operator.report({"ERROR"}, str(exc))
        return {"CANCELLED"}
    out = _output_dir(prefs, source)
    settings_path = out / "_pipeline_settings.json"
    settings_path.write_text(
        json.dumps(_settings_payload(prefs, source), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    command = [
        str(python), "-m", "dxf_read", str(source),
        "--no-blender", "--settings", str(settings_path),
        *extra,
    ]
    operator._mode = mode
    if mode == "classify":
        reason = _chatgpt_block_reason(prefs, force_api=force_api)
        if reason:
            context.scene.dwgreader_chatgpt_block = reason[:400]
            context.scene.dwgreader_status = reason[:180]
            operator.report({"WARNING"}, reason[:180])
            _redraw(context)
            return {"CANCELLED"}
    context.scene.dwgreader_chatgpt_block = ""
    env = _python_env(prefs, force_api=force_api)
    notice = chatgpt_auth.last_notice()
    if notice and not force_api:
        operator.report({"WARNING"}, notice)
    return _start_process(
        operator, context, command, cwd=engine, env=env,
        title=title,
        log_path=out / "dwgreader_import.log",
    )


def _fill_review(context, layers_path: Path) -> tuple[int, int]:
    global _LAYER_SYNC
    data = json.loads(layers_path.read_text(encoding="utf-8"))
    scene = context.scene
    _remember_layers(scene)
    memory = _layer_memory(scene).get(str(scene.dwgreader_source), {})
    counts = data.get("counts") or {}
    scene.dwgreader_selected.clear()
    scene.dwgreader_ignored.clear()
    seen: set[str] = set()
    pending: list[dict] = []

    def add(name: str, category: str, recognized: bool) -> None:
        text = str(name).strip()
        if not text or text in seen:
            return
        row = counts.get(text) or {}
        entities = int(row.get("entities") or 0)
        if "entities" in row and entities <= 0:
            return
        seen.add(text)
        pending.append({
            "name": text,
            "category": category,
            "recognized": recognized,
            "fills": int(row.get("fills") or 0),
            "curves": int(row.get("curves") or 0),
            "entities": entities,
        })

    _LAYER_SYNC = True
    try:
        order = [item[0] for item in LAYER_CATEGORIES]
        grouped = data.get("by_category") or {}
        for category in order:
            for name in grouped.get(category) or []:
                add(name, category, True)
        for category, names in grouped.items():
            if category in CATEGORY_IDS:
                continue
            for name in names or []:
                add(name, "none", True)
        for name in data.get("rejected") or []:
            add(name, "none", False)
        pending.sort(key=lambda row: (-row["entities"], row["name"].lower()))
        for row in pending:
            item = scene.dwgreader_selected.add()
            item.layer_name = row["name"]
            item.category = row["category"]
            item.recognized = row["recognized"]
            item.build = row["recognized"]
            item.geometry = "fills"
            item.fills = row["fills"]
            item.curves = row["curves"]
            item.entities = row["entities"]
            saved = memory.get(row["name"])
            if isinstance(saved, list) and len(saved) == 3:
                item.build = bool(saved[0])
                if saved[1] in CATEGORY_IDS:
                    item.category = saved[1]
            if row["fills"] <= 0 and row["curves"] > 0:
                item.geometry = "curves"
        scene.dwgreader_geometry = "fills"
    finally:
        _LAYER_SYNC = False
    scene.dwgreader_selected_index = 0
    scene.dwgreader_classified = scene.dwgreader_source
    recognized = sum(1 for item in scene.dwgreader_selected if item.recognized)
    return recognized, len(scene.dwgreader_selected) - recognized


class DWGREADER_OT_classify(Operator):
    bl_idname = "dwgreader.classify_layers"
    bl_label = "Анализировать чертёж"
    bl_description = "Прочитать чертёж, распознать типы слоёв и посчитать объекты. Сцену не меняет"
    bl_options = {"REGISTER"}

    _timer = None
    _proc = None
    _thread = None
    _state: dict | None = None
    _mode = "classify"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    force_api: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        title = "Классификация через API…" if self.force_api else "Разбор чертежа и классификация слоёв…"
        return _launch_pipeline(
            self, context, ["--classify-only"],
            title,
            "classify",
            force_api=bool(self.force_api),
        )

    def modal(self, context, event):
        return _poll_process(
            self, context, event,
            on_ok="",
            on_err="Разбор слоёв не удался",
            mode="classify",
        )

    def _stop(self, context, kill: bool) -> None:
        _stop_process(self, context, kill)


class DWGREADER_OT_mark_layers(Operator):
    bl_idname = "dwgreader.mark_layers"
    bl_label = "Отметить слои"
    bl_description = "Отметить слои для построения"
    bl_options = {"INTERNAL", "UNDO"}

    mode: EnumProperty(
        items=(
            ("all", "Выбрать все", "Поставить галочку у каждого слоя"),
            ("none", "Снять все", "Снять галочку у каждого слоя"),
            ("working", "Выбрать рабочие", "Оставить галочки только у слоёв, которым назначен тип"),
        ),
        default="all",
    )

    def execute(self, context):
        scene = context.scene
        for item in scene.dwgreader_selected:
            if self.mode == "all":
                item.build = True
            elif self.mode == "none":
                item.build = False
            else:
                item.build = item.category not in {"", "none"}
        return {"FINISHED"}


class DWGREADER_OT_plan_zoom(Operator):
    bl_idname = "dwgreader.plan_zoom"
    bl_label = "Масштаб плана"
    bl_description = "Приблизить или отдалить план активного слоя"
    bl_options = {"INTERNAL"}

    delta: IntProperty(name="Шаг", default=1)

    def execute(self, context):
        scene = context.scene
        current = int(getattr(scene, "dwgreader_plan_zoom", 1) or 1)
        scene.dwgreader_plan_zoom = min(4, max(1, current + int(self.delta)))
        return {"FINISHED"}


class DWGREADER_OT_goto(Operator):
    bl_idname = "dwgreader.goto"
    bl_label = "Перейти"
    bl_description = "Открыть экран"
    bl_options = {"INTERNAL"}

    screen: StringProperty(default="home")
    step: IntProperty(default=0)

    def execute(self, context):
        scene = context.scene
        if self.screen in {item[0] for item in SCREENS}:
            scene.dwgreader_screen = self.screen
        if 1 <= self.step <= 3:
            scene.dwgreader_step = self.step
        return {"FINISHED"}


class DWGREADER_OT_sheet(Operator):
    bl_idname = "dwgreader.sheet"
    bl_label = "Чертёж на листе"
    bl_description = "Показать чертёж, оставить только его или убрать"
    bl_options = {"UNDO"}

    index: IntProperty(default=0, min=0)
    action: EnumProperty(
        items=(
            ("frame", "Показать", "Кадрировать вид на этот чертёж"),
            ("keep", "Оставить", "Убрать остальные чертежи листа"),
            ("drop", "Убрать", "Удалить объекты этого чертежа"),
        ),
    )

    def execute(self, context):
        root = bpy.data.collections.get(UNDERLAY_ROOT)
        if root is None:
            self.report({"ERROR"}, "Подложки в сцене нет")
            return {"CANCELLED"}
        groups = sheet_groups(root, context.scene.dwgreader_sheet_gap)
        if self.index >= len(groups):
            self.report({"ERROR"}, "Список чертежей изменился")
            return {"CANCELLED"}
        chosen = groups[self.index]
        if self.action == "frame":
            chosen_names = {obj.name for obj in chosen}
            others = [obj for group in groups for obj in group if obj.name not in chosen_names]
            frame_objects(context, chosen, others)
            context.scene.dwgreader_sheet_shown = self.index
            self.report({"INFO"}, f"Чертёж {self.index + 1}")
            return {"FINISHED"}
        context.scene.dwgreader_sheet_shown = -1
        layer = context.view_layer.objects
        keep_names = {obj.name for obj in chosen}
        if self.action == "keep":
            for obj in chosen:
                if obj.name in layer:
                    obj.hide_set(False)
            others = [obj for group in groups for obj in group if obj.name not in keep_names]
            count = drop_objects(others, root)
            self.report({"INFO"}, f"Оставлен чертёж {self.index + 1}, убрано {count}")
            return {"FINISHED"}
        count = drop_objects(list(chosen), root)
        for group in groups:
            for obj in group:
                try:
                    name = obj.name
                except ReferenceError:
                    continue
                if name in keep_names or name not in layer:
                    continue
                obj.hide_set(False)
        self.report({"INFO"}, f"Убран чертёж {self.index + 1}: {count}")
        return {"FINISHED"}


class DWGREADER_OT_open_chat(Operator):
    bl_idname = "dwgreader.open_chat"
    bl_label = "Открыть чат"
    bl_description = "Чат ChatGPT: сцена, выделение и вьюпорт через MCP"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        from . import chat_view

        context.scene.dwgreader_screen = "chat"
        if not chat_view.request_chat_window(context):
            self.report({"WARNING"}, "Не удалось открыть окно чата")
            return {"CANCELLED"}
        return {"FINISHED"}


class DWGREADER_OT_cancel_import(Operator):
    bl_idname = "dwgreader.cancel_import"
    bl_label = "Отменить импорт"
    bl_description = "Удалить построенную подложку из сцены"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy and bpy.data.collections.get(UNDERLAY_ROOT) is not None

    def execute(self, context):
        remove_root(UNDERLAY_ROOT)
        placement.clear_placement()
        context.scene.dwgreader_step = 2 if len(context.scene.dwgreader_selected) else 1
        context.scene.dwgreader_status = "Подложка удалена"
        return {"FINISHED"}


class DWGREADER_OT_ping(Operator):
    bl_idname = "dwgreader.ping_server"
    bl_label = "Проверить сервер"
    bl_description = "Проверить, отвечает ли сервер модели классификации"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        _start_ping()
        return {"FINISHED"}


def _ping_worker(host: str, port: int) -> None:
    import socket
    import time as clock

    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            raise OSError("нет адреса")
        family, kind, proto, _name, address = infos[0]
        started = clock.perf_counter()
        with socket.socket(family, kind, proto) as sock:
            sock.settimeout(5.0)
            sock.connect(address)
        elapsed = int((clock.perf_counter() - started) * 1000)
        _PING.update(ok=True, text=f"Сервер отвечает · {elapsed} мс")
    except Exception:
        _PING.update(ok=False, text="Сервер не отвечает")
    finally:
        _PING["busy"] = False
        _PING["fresh"] = True


def _start_ping() -> None:
    import time as clock
    from urllib.parse import urlparse

    if _PING["busy"]:
        return
    prefs = _addon_prefs(bpy.context)
    url = str(getattr(prefs, "llm_base_url", "") or "").strip()
    parsed = urlparse(url)
    _PING["stamp"] = clock.time()
    if not parsed.hostname:
        _PING.update(ok=False, text="Адрес модели не задан", fresh=True)
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _PING["busy"] = True
    threading.Thread(target=_ping_worker, args=(parsed.hostname, port), daemon=True).start()


def _restore_chatgpt_models():
    """Сессия на диске переживает перезапуск. Список моделей подтягивается заново, если его ещё нет."""
    state = _restore_chatgpt_models
    state.tries = getattr(state, "tries", 0) + 1
    profile = chatgpt_auth.public_profile()
    if profile is None or not profile.get("sharing") or state.tries > 8:
        return None
    if state.tries == 1:
        threading.Thread(target=chatgpt_auth.refresh_models, daemon=True).start()
    models = chatgpt_auth.cached_models()
    slugs = [item["slug"] for item in models if item.get("slug")]
    if not slugs:
        return 1.0
    prefs = _addon_prefs(bpy.context)
    saved = chatgpt_auth.saved_model()
    if prefs is not None and prefs.chatgpt_model not in slugs:
        prefs.chatgpt_model = saved if saved in slugs else slugs[0]
    _redraw(bpy.context)
    return None


def _ping_timer():
    import time as clock

    try:
        if _PING["fresh"]:
            _PING["fresh"] = False
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
        if not _PING["busy"] and clock.time() - float(_PING["stamp"]) >= 30.0:
            _start_ping()
    except Exception:
        pass
    return 1.0


class DWGREADER_OT_generate(Operator):
    bl_idname = "dwgreader.generate_underlay"
    bl_label = "Построить"
    bl_description = "Собрать подложку в сцене из отмеченных слоёв. Прежняя подложка заменяется. Ctrl+Z отменяет весь импорт"
    bl_options = set()

    _timer = None
    _proc = None
    _thread = None
    _state: dict | None = None
    _mode = "build"

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return (
            not context.window_manager.dwgreader_busy
            and any(item.build for item in scene.dwgreader_selected)
            and scene.dwgreader_classified == scene.dwgreader_source
        )

    def invoke(self, context, event):
        global _BUILD_STARTED
        import time as clock

        scene = context.scene
        source = _drawing_path(context)
        prefs = _addon_prefs(context)
        if source is None or prefs is None:
            self.report({"ERROR"}, "Выберите чертёж и разберите слои")
            return {"CANCELLED"}
        groups: dict[str, list[str]] = {}
        per_layer: dict[str, str] = {}
        mixed = scene.dwgreader_geometry == "per_layer"
        for item in scene.dwgreader_selected:
            name = item.layer_name.strip()
            if name and item.build:
                groups.setdefault(item.category, []).append(name)
                if mixed:
                    per_layer[name] = item.geometry
        if not groups:
            self.report({"ERROR"}, "Не отмечено ни одного слоя")
            return {"CANCELLED"}
        _remember_layers(scene)
        out = _output_dir(prefs, source)
        groups_path = out / "_confirmed_layers.json"
        payload: dict = {"by_category": groups}
        if mixed:
            payload["geometry_by_layer"] = per_layer
        groups_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        preset = scene.dwgreader_geometry if scene.dwgreader_geometry in {"fills", "curves", "both"} else "fills"
        _BUILD_STARTED = clock.time()
        return _launch_pipeline(
            self, context, ["--groups", str(groups_path), "--geometry", preset],
            "Сборка благоустройства…",
            "build",
        )

    def modal(self, context, event):
        return _poll_process(
            self, context, event,
            on_ok="",
            on_err="Генерация не удалась",
            mode="build",
        )

    def _stop(self, context, kill: bool) -> None:
        _stop_process(self, context, kill)


def _draw_mcp_setup(layout) -> None:
    """Установка MCP на том же экране, что библиотеки и ODA."""
    try:
        ready = scene_chat.mcp_operator_ready()
        opened = scene_chat.port_is_open()
    except Exception:
        ready = False
        opened = False
    if opened:
        return
    box = layout.box()
    if ready:
        box.label(text="Сервер MCP не запущен", icon="INFO")
        box.operator("dwgreader.chat_server", text="Запустить сервер", icon="PLAY")
    else:
        box.label(text="MCP for Blender ещё не стоит", icon="INFO")
        box.operator("dwgreader.chat_install", text="Установить MCP", icon="IMPORT")


def _draw_setup(layout, prefs) -> None:
    if not _deps_ready():
        box = layout.box()
        box.label(text="Библиотеки разбора ещё не стоят", icon="ERROR")
        box.operator("dwgreader.install_deps", icon="IMPORT")
    if not _oda_installed(prefs):
        box = layout.box()
        box.label(text="Для DWG не найден ODA File Converter", icon="INFO")
        box.operator("dwgreader.install_oda", icon="IMPORT")
    _draw_mcp_setup(layout)


def _tile(layout, key: str, title: str, lines: tuple[str, ...]):
    box = layout.box()
    icon = _preview(key)
    if icon:
        row = box.row()
        row.alignment = "CENTER"
        row.template_icon(icon_value=icon, scale=TILE_SCALE)
    box.label(text=title)
    text = box.column(align=True)
    text.scale_y = 0.75
    for line in lines:
        text.label(text=line)
    return box


def _draw_home(layout, context) -> None:
    tile = _tile(layout, "tile_chat", "Генерация по описанию", (
        "Объекта ещё нет — опишите словами,",
        "и модель соберёт объёмы под сцену.",
    ))
    tile.operator("dwgreader.open_chat", text="Открыть чат")

    tile = _tile(layout, "tile_dxf", "Импорт DXF · благоустройство", (
        "Объект уже начерчен — разберёт слои",
        "и построит только подтверждённые типы.",
    ))
    op = tile.operator("dwgreader.goto", text="Выбрать чертёж")
    op.screen = "import"
    op.step = 1

    tile = _tile(layout, "tile_maf", "Озеленение и МАФ", (
        "Подложка уже есть — расставит деревья,",
        "кусты, лавки и детские площадки.",
    ))
    op = tile.operator("dwgreader.goto", text="Открыть")
    op.screen = "place"

    tile = _tile(layout, "tile_building", "Здание · геометрические ноды", (
        "Жилой дом собран нодами — поставит",
        "его в сцену и выгрузит фасад в GLB.",
    ))
    op = tile.operator("dwgreader.goto", text="Открыть")
    op.screen = "building"

    box = layout.box()
    row = box.row()
    row.enabled = False
    icon = _preview("tile_parking")
    if icon:
        row.template_icon(icon_value=icon, scale=2.0)
    row.label(text="Раскладка парковок")
    row.label(text="В работе")


def _step_ready(scene, step: int) -> bool:
    if step == 1:
        return True
    if step == 2:
        return bool(len(scene.dwgreader_selected)) and scene.dwgreader_classified == scene.dwgreader_source != ""
    return bpy.data.collections.get(UNDERLAY_ROOT) is not None


def _draw_back(layout, label: str) -> None:
    row = layout.row()
    op = row.operator("dwgreader.goto", text=label, icon="BACK", emboss=False)
    op.screen = "home"


def _draw_import(layout, context, prefs) -> None:
    scene = context.scene
    _draw_back(layout, "Инструменты")
    crumbs = layout.row(align=True)
    for number, label in ((1, "Файл"), (2, "Объекты"), (3, "Геометрия")):
        cell = crumbs.row(align=True)
        cell.enabled = _step_ready(scene, number)
        op = cell.operator("dwgreader.goto", text=f"{number} {label}", depress=scene.dwgreader_step == number)
        op.screen = "import"
        op.step = number
    step = scene.dwgreader_step
    while step > 1 and not _step_ready(scene, step):
        step -= 1
    if step == 1:
        _draw_step_file(layout, context, prefs)
    elif step == 2:
        _draw_step_objects(layout, context, prefs)
    else:
        _draw_step_result(layout, context)


def _wrap_status(text: str, width: int = 42) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        piece = f"{current} {word}".strip()
        if current and len(piece) > width:
            lines.append(current)
            current = word
        else:
            current = piece
    if current:
        lines.append(current)
    return lines or [text]


def _draw_step_file(layout, context, prefs) -> None:
    scene = context.scene
    box = layout.box()
    box.label(text="Импорт DXF · благоустройство", icon="IMPORT")
    box.prop(scene, "dwgreader_source", text="")
    if prefs is not None:
        col = box.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(prefs, "batch_size")
    block = (scene.dwgreader_chatgpt_block or "").strip()
    if block:
        failed = box.box()
        failed.label(text="ChatGPT не ответил", icon="ERROR")
        for line in _wrap_status(block):
            failed.label(text=line)
        choice = failed.row(align=True)
        choice.scale_y = 1.3
        choice.operator("dwgreader.classify_layers", text="Повторить через ChatGPT").force_api = False
        choice.operator("dwgreader.classify_layers", text="Получить ответ по API").force_api = True
    run = box.row()
    run.scale_y = 1.4
    run.operator("dwgreader.classify_layers", text="Анализировать чертёж", icon="VIEWZOOM")
    hint = layout.column(align=True)
    hint.scale_y = 0.75
    hint.label(text="Анализ не меняет ни чертёж, ни сцену.", icon="INFO")
    hint.label(text="Геометрия появится после шага 2.")


_PLAN_FILE = {"path": "", "mtime": 0, "layers": {}}
_LAYER_PLAN = {"key": None, "icon": 0, "message": "", "preview_key": ""}


def _plan_segments(cache: Path) -> dict[str, list[tuple[float, float, float, float]]]:
    mtime = cache.stat().st_mtime_ns
    if _PLAN_FILE["path"] == str(cache) and _PLAN_FILE["mtime"] == mtime:
        return _PLAN_FILE["layers"]
    import gzip

    with gzip.open(cache, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    layers: dict[str, list[tuple[float, float, float, float]]] = {}

    def add(name: str, segment: tuple[float, float, float, float]) -> None:
        if not name:
            return
        bucket = layers.setdefault(name, [])
        if len(bucket) < 8000:
            bucket.append(segment)

    for fill in payload.get("fills") or []:
        if not isinstance(fill, list) or len(fill) < 6:
            continue
        name = str(fill[1] or "")
        rings = [fill[5]]
        if isinstance(fill[6], list):
            rings.extend(fill[6])
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 2:
                continue
            points = [
                (float(point[0]), float(point[1]))
                for point in ring
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if len(points) < 2:
                continue
            closed = points + points[:1]
            for start, end in zip(closed, closed[1:]):
                add(name, (start[0], start[1], end[0], end[1]))
    for curve in payload.get("curves") or []:
        if not isinstance(curve, list) or len(curve) < 6:
            continue
        add(str(curve[0] or ""), (float(curve[2]), float(curve[3]), float(curve[4]), float(curve[5])))
    _PLAN_FILE["path"] = str(cache)
    _PLAN_FILE["mtime"] = mtime
    _PLAN_FILE["layers"] = layers
    return layers


def _layer_plan_icon(context, prefs, layer_name: str) -> tuple[int, str]:
    from .placement_math import plan_image

    scene = context.scene
    source_raw = str(getattr(scene, "dwgreader_source", "") or "").strip()
    if prefs is None or not source_raw or _PREVIEWS is None:
        return 0, "Повторите анализ"
    source = Path(bpy.path.abspath(source_raw))
    cache = _output_dir(prefs, source) / f"{source.stem}_geometry_cache.json.gz"
    if not cache.is_file():
        return 0, "Повторите анализ"
    zoom = min(4, max(1, int(getattr(scene, "dwgreader_plan_zoom", 1) or 1)))
    key = (str(cache), cache.stat().st_mtime_ns, layer_name, zoom)
    if _LAYER_PLAN["key"] == key:
        return int(_LAYER_PLAN["icon"]), str(_LAYER_PLAN["message"])
    try:
        segments = _plan_segments(cache).get(layer_name) or []
        image = plan_image(segments, 160 * zoom)
    except (OSError, ValueError, EOFError):
        return 0, "Повторите анализ"
    previous = str(_LAYER_PLAN.get("preview_key") or "")
    if previous and previous in _PREVIEWS:
        try:
            del _PREVIEWS[previous]
        except Exception:
            pass
    if not image:
        _LAYER_PLAN.update(key=key, icon=0, message="В слое нет контуров", preview_key="")
        return 0, "В слое нет контуров"
    folder = (Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else Path.home()) / "DWGREADER" / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    preview_key = f"layer-{key[1]}-{zoom}-{hashlib.sha256(layer_name.encode('utf-8')).hexdigest()[:10]}"
    dest = folder / f"{preview_key}.png"
    dest.write_bytes(image)
    _PREVIEWS.load(preview_key, str(dest), "IMAGE")
    icon = _PREVIEWS[preview_key].icon_id
    _LAYER_PLAN.update(key=key, icon=icon, message="", preview_key=preview_key)
    return icon, ""


def _draw_layer_plan(layout, context, prefs) -> None:
    scene = context.scene
    items = scene.dwgreader_selected
    if not items:
        return
    index = min(max(int(scene.dwgreader_selected_index), 0), len(items) - 1)
    name = items[index].layer_name
    try:
        icon, message = _layer_plan_icon(context, prefs, name)
    except Exception:
        icon, message = 0, "Повторите анализ"
    if icon:
        zoom = min(4, max(1, int(getattr(scene, "dwgreader_plan_zoom", 1) or 1)))
        controls = layout.row(align=True)
        controls.operator("dwgreader.plan_zoom", text="−").delta = -1
        controls.label(text=f"План {zoom}×")
        controls.operator("dwgreader.plan_zoom", text="+").delta = 1
        row = layout.row()
        row.alignment = "CENTER"
        row.template_icon(icon_value=icon, scale=4.5 * zoom)
        layout.label(text=name)
        return
    if message:
        layout.label(text=message, icon="INFO")


def _fold(layout, scene, prop: str, title: str) -> bool:
    opened = bool(getattr(scene, prop, False))
    row = layout.row(align=True)
    row.prop(scene, prop, text=title, icon="TRIA_DOWN" if opened else "TRIA_RIGHT", emboss=False)
    return opened


def _draw_step_objects(layout, context, prefs) -> None:
    scene = context.scene
    summary = _layer_summary(scene)
    box = layout.box()
    box.label(text="Что нашлось в чертеже")
    text = box.column(align=True)
    text.scale_y = 0.8
    text.label(text=f"{_layers_text(summary['layers'])}, {_objects_text(summary['objects'])}.")
    text.label(
        text=f"Распознано типов: {summary['types']} — "
        f"{_objects_text(summary['recognized_objects'])} в {_layers_text(summary['recognized'])}."
    )
    box.prop(scene, "dwgreader_geometry", text="Отрисовка")
    if prefs is not None:
        fills_only = scene.dwgreader_geometry == "fills"
        contour_option = box.row()
        contour_option.enabled = not fills_only
        contour_option.prop(prefs, "contour_from_fills")

    if _fold(layout, scene, "dwgreader_show_layers", "Слои"):
        layers = layout.box()
        layers.prop(scene, "dwgreader_filter", text="", icon="VIEWZOOM")
        marks = layers.row(align=True)
        marks.operator("dwgreader.mark_layers", text="Снять все").mode = "none"
        marks.operator("dwgreader.mark_layers", text="Выбрать все").mode = "all"
        working = layers.row(align=True)
        working.operator("dwgreader.mark_layers", text="Выбрать рабочие").mode = "working"
        head = layers.row(align=True)
        head.label(text="", icon="BLANK1")
        name, rest, tail = _layer_columns(head)
        name.label(text="Слой")
        rest.label(text="Тип")
        tail.label(text="Отрисовка")
        tail.label(text="DXF")
        _draw_layer_plan(layers, context, prefs)
        layers.template_list(
            "DWGREADER_UL_layers", "layers",
            scene, "dwgreader_selected",
            scene, "dwgreader_selected_index",
            rows=10,
        )
        if summary["unknown"]:
            warn = layers.row(align=True)
            warn.label(
                text=f"{_layers_text(summary['unknown'])}, {_objects_text(summary['unknown_objects'])} — тип не распознан",
                icon="ERROR",
            )
            warn.prop(
                scene, "dwgreader_show_unknown",
                text="Скрыть" if scene.dwgreader_show_unknown else "Показать",
                toggle=True,
            )

    if prefs is not None and _fold(layout, scene, "dwgreader_show_params", "Параметры построения"):
        params = layout.box()
        col = params.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(prefs, "scene_scale")
        col.prop(prefs, "origin_mode")
        col.prop(prefs, "omit_symbols")
        col.prop(prefs, "omit_fences")
        cut = col.row()
        cut.enabled = not prefs.omit_symbols
        cut.prop(prefs, "cut_vegetation")
        _draw_geometry(col, prefs)
        _draw_split(col, prefs)

    flags = layout.column(align=True)
    flags.prop(scene, "dwgreader_auto_trees", text="Расстановка деревьев")
    flags.prop(scene, "dwgreader_auto_maf", text="МАФ на площадках")
    maf = flags.column(align=True)
    maf.enabled = bool(scene.dwgreader_auto_maf)
    maf.prop(scene, "dwgreader_auto_play_kind", text="Тип площадок")
    maf.prop(scene, "dwgreader_safety_scale", text="Зона безопасности", slider=True)
    flags.prop(scene, "dwgreader_auto_buildings", text="Расстановка зданий")
    flags.prop(scene, "dwgreader_auto_prune_sheets", text="Убрать мелкие чертежи")
    small = flags.column(align=True)
    small.enabled = bool(scene.dwgreader_auto_buildings)
    small.prop(scene, "dwgreader_skip_small_buildings", text="Не ставить на мелкие пятна")
    area = small.row()
    area.enabled = bool(scene.dwgreader_auto_buildings) and bool(scene.dwgreader_skip_small_buildings)
    area.prop(scene, "dwgreader_min_building_area_m2", text="Минимальная площадь здания, м²")

    nav = layout.row(align=True)
    nav.scale_y = 1.3
    back = nav.operator("dwgreader.goto", text="Назад")
    back.screen = "import"
    back.step = 1
    nav.operator(
        "dwgreader.generate_underlay",
        text=f"Построить · {_layers_text(summary['chosen'])}, {_objects_text(summary['chosen_objects'])}",
        icon="PLAY",
    )
    note = layout.column(align=True)
    note.scale_y = 0.75
    note.label(text="Галочки хранятся в сцене: повторный анализ")
    note.label(text="того же чертежа откроется с ними.")


def _sheets_title(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        word = "чертежей"
    elif tail % 10 == 1:
        word = "чертёж"
    elif tail % 10 in (2, 3, 4):
        word = "чертежа"
    else:
        word = "чертежей"
    return f"На листе {count} {word}"


def _draw_sheets(layout, context, root) -> None:
    scene = context.scene
    groups = sheet_groups(root, scene.dwgreader_sheet_gap)
    box = layout.box()
    box.label(text=_sheets_title(len(groups)), icon="MESH_GRID")
    gap = box.column()
    gap.use_property_split = True
    gap.use_property_decorate = False
    gap.prop(scene, "dwgreader_sheet_gap")
    if len(groups) < 2:
        note = box.column(align=True)
        note.scale_y = 0.75
        note.label(text="Всё попало в один чертёж. Если на листе их")
        note.label(text="несколько, уменьшите зазор — части появятся списком.")
        return
    note = box.column(align=True)
    note.scale_y = 0.75
    note.label(text="Показать приближает вид. Оставить убирает остальные.")
    for index, group in enumerate(groups[:12]):
        width, height = sheet_size(group)
        box.label(text=f"{index + 1} · {width:.0f} × {height:.0f} м · {len(group)}")
        row = box.row(align=True)
        show = row.operator(
            "dwgreader.sheet",
            text="Показан" if scene.dwgreader_sheet_shown == index else "Показать",
            depress=scene.dwgreader_sheet_shown == index,
        )
        show.index = index
        show.action = "frame"
        keep = row.operator("dwgreader.sheet", text="Оставить")
        keep.index = index
        keep.action = "keep"
        drop = row.operator("dwgreader.sheet", text="Убрать")
        drop.index = index
        drop.action = "drop"
    if len(groups) > 12:
        box.label(text=f"Ещё {len(groups) - 12}. Увеличьте зазор или уберите ближние.")


def _draw_step_result(layout, context) -> None:
    scene = context.scene
    root = bpy.data.collections.get(UNDERLAY_ROOT)
    if root is None:
        layout.label(text="Подложки в сцене нет")
        return
    children = list(root.children_recursive)
    box = layout.box()
    head = box.row()
    head.label(text="Геометрия построена", icon="CHECKMARK")
    if scene.dwgreader_build_seconds > 0:
        side = head.row()
        side.alignment = "RIGHT"
        side.label(text=f"{scene.dwgreader_build_seconds:.0f} с")
    text = box.column(align=True)
    text.scale_y = 0.8
    text.label(text=f"{_objects_text(len(root.all_objects))} в {len(children)} коллекциях")
    source = Path(str(root.get("dxf_source") or scene.dwgreader_source or "")).name
    if source:
        text.label(text=source)
    _draw_sheets(layout, context, root)
    plants = layout.box()
    plants.label(text="Деревья на газонах", icon="OUTLINER_OB_CURVES")
    placement.draw_plant_controls(plants, context, all_lawns=True)
    hint = plants.column(align=True)
    hint.scale_y = 0.75
    hint.label(text="Ползунки заново ставят деревья и кусты")
    hint.label(text="на все газоны этой подложки.")
    grounds = layout.box()
    grounds.label(text="Площадки", icon="MESH_GRID")
    names = placement.playground_layer_names()
    if names:
        for name in names[:8]:
            grounds.label(text=name)
        if len(names) > 8:
            grounds.label(text=f"и ещё {len(names) - 8}")
    else:
        empty = grounds.column(align=True)
        empty.scale_y = 0.75
        empty.label(text="Пока площадок нет: имя «площад», «игров», «спорт»")
        empty.label(text="или «резин». ТБО не берём.")

    tree = layout.box()
    top = tree.row()
    top.label(text=root.name, icon="OUTLINER_COLLECTION")
    top.label(text=str(len(root.all_objects)))
    shown = 0
    for child in root.children:
        row = tree.row()
        row.separator(factor=2.0)
        row.label(text=child.name, icon="OUTLINER_COLLECTION")
        row.label(text=str(len(child.all_objects)))
        shown += 1
        if shown >= 60:
            break
    note = layout.column(align=True)
    note.scale_y = 0.75
    note.label(text="Имя слоя и тип записаны в свойствах")
    note.label(text="каждого объекта (dxf_layer, category).")

    nav = layout.row(align=True)
    nav.scale_y = 1.3
    revise = nav.operator("dwgreader.goto", text="Изменить слои")
    revise.screen = "import"
    revise.step = 2
    nav.operator("dwgreader.cancel_import", text="Отменить импорт")
    again = nav.operator("dwgreader.goto", text="Ещё чертёж")
    again.screen = "import"
    again.step = 1
    tail = layout.column(align=True)
    tail.scale_y = 0.75
    tail.label(text="«Изменить слои» возвращает к выбору. Построить можно заново")
    tail.label(text="Весь импорт — один шаг отмены: Ctrl+Z")


def _draw_place(layout, context) -> None:
    _draw_back(layout, "Инструменты")
    placement.draw_panel(layout, context)


def _draw_building(layout, context) -> None:
    _draw_back(layout, "Инструменты")
    box = layout.box()
    box.label(text="Здание · геометрические ноды", icon="HOME")
    text = box.column(align=True)
    text.scale_y = 0.8
    text.label(text="Выделенный полигон задаёт контур дома.")
    text.label(text="Тип дома — в списке под кнопкой, не только номер.")
    run = box.row()
    run.scale_y = 1.3
    run.operator("dwgreader.place_building", text="Поставить здание в сцену", icon="HOME")
    building_export.draw_controls(layout, context)
    layout.operator("dwgreader.export_building_glb", text="Экспорт выбранного в GLB", icon="EXPORT")
    hint = layout.column(align=True)
    hint.scale_y = 0.75
    hint.label(text="Старый ряд заготовок можно удалить: это были")
    hint.label(text="разные сохранённые варианты одного и того же дома.")


class DWGREADER_PT_panel(Panel):
    bl_label = "Инструменты"
    bl_idname = "DWGREADER_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Подложка"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        prefs = _addon_prefs(context)
        scene = context.scene
        _draw_setup(layout, prefs)
        _draw_chatgpt(layout, prefs)
        screen = scene.dwgreader_screen
        if screen == "import":
            _draw_import(layout, context, prefs)
        elif screen == "place":
            _draw_place(layout, context)
        elif screen == "building":
            _draw_building(layout, context)
        elif screen == "chat":
            scene_chat.draw(layout, context)
        else:
            _draw_home(layout, context)
        if scene.dwgreader_status:
            status = layout.box()
            status.label(text=scene.dwgreader_status)
        if context.window_manager.dwgreader_busy:
            layout.label(text="Esc — остановить", icon="CANCEL")


class DWGREADER_PT_status(Panel):
    bl_label = "Состояние"
    bl_idname = "DWGREADER_PT_status"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Подложка"
    bl_options = {"HIDE_HEADER"}
    bl_order = 10

    def draw(self, context):
        row = self.layout.row()
        if _PING["busy"] and not _PING["text"]:
            text, icon = "Проверка сервера…", "TIME"
        elif _PING["text"]:
            text, icon = _PING["text"], "CHECKMARK" if _PING["ok"] else "ERROR"
        else:
            text, icon = "Сервер не проверен", "QUESTION"
        row.operator("dwgreader.ping_server", text=text, icon=icon, emboss=False)
        right = row.row()
        right.alignment = "RIGHT"
        version = ".".join(str(part) for part in bl_info["version"])
        right.label(text=f"v{version}")


def _start_process(operator, context, command, cwd: Path, env: dict, title: str, log_path: Path | None = None):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except OSError as exc:
        operator.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    state = {
        "status": title,
        "tail": [],
        "json": "",
        "layers": "",
        "empty": "",
        "chatgpt_need": "",
        "log": log_path or (cwd / "dwgreader_import.log"),
        "status_locked": False,
    }
    operator._state = state
    operator._proc = proc
    context.window_manager.dwgreader_busy = True
    context.scene.dwgreader_status = title

    def reader() -> None:
        log_file = Path(state["log"]).open("w", encoding="utf-8")
        try:
            stream = proc.stdout
            if stream is None:
                return
            for raw in stream:
                line = raw.rstrip()
                log_file.write(line + "\n")
                log_file.flush()
                tail = state["tail"]
                tail.append(line)
                del tail[:-40]
                if line.startswith(MARKER):
                    state["json"] = line[len(MARKER):].strip()
                elif line.startswith(LAYERS_MARKER):
                    state["layers"] = line[len(LAYERS_MARKER):].strip()
                elif line.startswith(EMPTY_MARKER):
                    state["empty"] = line[len(EMPTY_MARKER):].strip()
                elif line.startswith("CHATGPT_NEED="):
                    reason = line[len("CHATGPT_NEED="):].strip()
                    state["chatgpt_need"] = reason
                    state["status"] = reason[:180]
                    state["status_locked"] = True
                elif line.startswith("STATUS="):
                    state["status"] = line[len("STATUS="):].strip()[:180]
                    state["status_locked"] = True
                elif line and not state["status_locked"]:
                    state["status"] = line[:180]
        finally:
            log_file.close()

    operator._thread = threading.Thread(target=reader, daemon=True)
    operator._thread.start()
    operator._timer = context.window_manager.event_timer_add(0.4, window=context.window)
    context.window_manager.modal_handler_add(operator)
    return {"RUNNING_MODAL"}


def _stop_process(operator, context, kill: bool) -> None:
    if operator._timer is not None:
        context.window_manager.event_timer_remove(operator._timer)
        operator._timer = None
    proc = operator._proc
    if kill and proc is not None and proc.poll() is None:
        proc.kill()
    operator._proc = None
    context.window_manager.dwgreader_busy = False
    for window in context.window_manager.windows:
        try:
            window.cursor_modal_restore()
        except Exception:
            pass


def _poll_process(operator, context, event, on_ok: str, on_err: str, build: bool = False, mode: str = ""):
    if event.type == "ESC":
        operator._stop(context, kill=True)
        context.scene.dwgreader_status = "Отменено"
        return {"CANCELLED"}
    if event.type != "TIMER":
        return {"PASS_THROUGH"}

    proc = operator._proc
    if proc is None or proc.poll() is None:
        context.scene.dwgreader_status = str((operator._state or {}).get("status") or "")
        _redraw(context)
        return {"RUNNING_MODAL"}

    thread = operator._thread
    if thread is not None:
        thread.join(timeout=2)
    state = operator._state or {}
    operator._stop(context, kill=False)

    if mode == "classify":
        need = str(state.get("chatgpt_need") or "")
        if need:
            context.scene.dwgreader_chatgpt_block = need[:400]
            context.scene.dwgreader_status = need[:180]
            operator.report({"WARNING"}, need[:180])
            _redraw(context)
            return {"FINISHED"}
        layers_raw = str(state.get("layers") or "")
        layers_path = Path(layers_raw) if layers_raw else None
        if proc.returncode != 0 or layers_path is None or not layers_path.is_file():
            tail = state.get("tail") or []
            detail = tail[-1] if tail else f"код {proc.returncode}"
            context.scene.dwgreader_status = f"Ошибка: {detail}"
            operator.report({"ERROR"}, f"{on_err}. Журнал: {state.get('log')}")
            return {"CANCELLED"}
        selected, ignored = _fill_review(context, layers_path)
        context.scene.dwgreader_step = 2
        context.scene.dwgreader_status = f"Слои разобраны: {selected} распознано, {ignored} без типа"
        operator.report({"INFO"}, context.scene.dwgreader_status)
        _redraw(context)
        return {"FINISHED"}

    if (build or mode == "build") and state.get("empty"):
        message = str(state["empty"])
        context.scene.dwgreader_status = message[:180]
        _schedule_underlay_replace(context.scene.name, None, message)
        operator.report({"WARNING"}, message)
        _redraw(context)
        return {"FINISHED"}

    if build or mode == "build":
        if proc.returncode != 0 or not state.get("json"):
            tail = state.get("tail") or []
            detail = tail[-1] if tail else f"код {proc.returncode}"
            context.scene.dwgreader_status = f"Ошибка: {detail}"
            operator.report({"ERROR"}, f"{on_err}. Журнал: {state.get('log')}")
            return {"CANCELLED"}
        json_path = Path(str(state["json"]))
        if not json_path.is_file():
            context.scene.dwgreader_status = "JSON подложки не найден"
            operator.report({"ERROR"}, str(json_path))
            return {"CANCELLED"}
        context.scene.dwgreader_status = "Замена подложки в сцене…"
        _schedule_underlay_replace(context.scene.name, str(json_path))
        operator.report({"INFO"}, "Подложка заменяется в сцене")
        _redraw(context)
        return {"FINISHED"}

    if proc.returncode != 0:
        tail = state.get("tail") or []
        detail = tail[-1] if tail else f"код {proc.returncode}"
        context.scene.dwgreader_status = f"Ошибка: {detail}"
        operator.report({"ERROR"}, f"{on_err}. Журнал: {state.get('log')}")
        return {"CANCELLED"}
    context.scene.dwgreader_status = on_ok
    operator.report({"INFO"}, on_ok)
    return {"FINISHED"}


class _SceneHolder:
    """Таймер сборки читает галочки той сцены, которую только что построил."""

    def __init__(self, base, scene):
        self._base = base
        self.scene = scene

    def __getattr__(self, name):
        return getattr(self._base, name)


def _fit_status(text: str, limit: int = 180) -> str:
    """Оставляет хвост про здания, если строка статуса длиннее лимита."""
    if len(text) <= limit:
        return text
    marker = "здания "
    index = text.rfind(marker)
    if index < 0:
        return text[:limit]
    tail = text[index:]
    if len(tail) >= limit:
        return tail[:limit]
    head = text[:index].rstrip(". ")
    room = limit - len(tail) - 1
    if room < 8:
        return tail
    return head[:room].rstrip() + " " + tail


def _schedule_underlay_replace(scene_name: str, json_path: str | None, empty_message: str | None = None) -> None:
    """Сборка после завершения оператора. Иначе Blender откатывает удаление старой подложки."""

    def run() -> None:
        import time as clock

        scene = bpy.data.scenes.get(scene_name)
        if scene is None:
            return None
        try:
            placement.clear_placement()
            building_export.clear_auto_buildings()
            if not json_path:
                remove_root(UNDERLAY_ROOT)
                if empty_message:
                    scene.dwgreader_status = empty_message[:180]
                return None
            count = build_underlay(Path(json_path), scene)
            pruned = _prune_small_sheets(scene)
            if pruned[1]:
                count = max(count - pruned[1], 0)
            placed = ""
            base = bpy.context
            if base is not None:
                holder = base if getattr(base, "scene", None) is scene else _SceneHolder(base, scene)
                try:
                    placed = placement.furnish_underlay(holder)
                except Exception as exc:
                    placed = f"Расстановка не завершилась: {exc}"
        except Exception as exc:
            scene.dwgreader_status = f"Ошибка сборки: {exc}"
            return None
        scene.dwgreader_build_seconds = max(clock.time() - _BUILD_STARTED, 0.0) if _BUILD_STARTED else 0.0
        scene.dwgreader_step = 3
        status = f"Готово: объектов в сцене {count}"
        if pruned[0] or pruned[2] > 1:
            status = f"{status}. Убрано мелких чертежей {pruned[0]}, крупных осталось {pruned[2]}"
        if placed:
            status = f"{status}. {placed}"
        scene.dwgreader_status = _fit_status(status)
        try:
            bpy.ops.ed.undo_push(message="Подложка DWG")
        except Exception:
            pass
        return None

    bpy.app.timers.register(run, first_interval=0.05)


def _prune_small_sheets(scene) -> tuple[int, int, int]:
    """Мелкие чертежи до расстановки. Возвращает (групп убрано, объектов убрано, групп осталось)."""
    if not bool(getattr(scene, "dwgreader_auto_prune_sheets", False)):
        return (0, 0, 0)
    root = bpy.data.collections.get(UNDERLAY_ROOT)
    if root is None:
        return (0, 0, 0)
    groups = sheet_groups(root, float(getattr(scene, "dwgreader_sheet_gap", SHEET_GAP_M)))
    if len(groups) < 2:
        return (0, 0, len(groups))
    from .placement_math import small_sheet_indexes

    drop_at = small_sheet_indexes([len(group) for group in groups])
    kept = len(groups) - len(drop_at)
    if not drop_at:
        return (0, 0, kept)
    removed = drop_objects([obj for index in drop_at for obj in groups[index]], root)
    return (len(drop_at), removed, kept)


def build_underlay(json_path: Path, scene: bpy.types.Scene | None = None) -> int:
    target = scene or bpy.context.scene
    prefs = _addon_prefs(bpy.context)
    if prefs is None:
        return commit_underlay(json_path, scene=target)
    gap = float(getattr(prefs, "layer_part_gap_m", 10.0))
    return commit_underlay(
        json_path,
        gap,
        split_mode=str(getattr(prefs, "split_mode", "gap") or "gap"),
        by_type=bool(getattr(prefs, "by_type_collections", True)),
        scene=target,
    )


CLASSES = (
    DWGREADER_AddonPreferences,
    DWGREADER_LayerItem,
    DWGREADER_UL_layers,
    DWGREADER_UL_place_layers,
    DWGREADER_OT_install_deps,
    DWGREADER_OT_chatgpt_login,
    DWGREADER_OT_chatgpt_copy_link,
    DWGREADER_OT_chatgpt_cancel,
    DWGREADER_OT_chatgpt_usage,
    DWGREADER_OT_chatgpt_models,
    DWGREADER_OT_chatgpt_logout,
    DWGREADER_OT_install_oda,
    DWGREADER_OT_pick_file,
    DWGREADER_OT_classify,
    DWGREADER_OT_mark_layers,
    DWGREADER_OT_plan_zoom,
    DWGREADER_OT_goto,
    DWGREADER_OT_sheet,
    DWGREADER_OT_open_chat,
    DWGREADER_OT_cancel_import,
    DWGREADER_OT_ping,
    DWGREADER_OT_generate,
    DWGREADER_PT_panel,
    DWGREADER_PT_status,
)


_LAYER_PENDING = False


def _activate_listed_layer(self, context):
    global _LAYER_PENDING
    if _LAYER_PENDING or context is None:
        return
    _LAYER_PENDING = True

    def _later():
        global _LAYER_PENDING
        _LAYER_PENDING = False
        current = bpy.context
        if current is None or getattr(current, "mode", "OBJECT") != "OBJECT":
            return None
        try:
            meshes = placement._listed_layer_meshes(current)
        except Exception:
            return None
        if not meshes:
            return None
        obj = meshes[0]
        for item in list(getattr(current, "selected_objects", []) or []):
            item.select_set(False)
        obj.hide_set(False)
        obj.select_set(True)
        current.view_layer.objects.active = obj
        return None

    bpy.app.timers.register(_later, first_interval=0.05)


_RETIRED_LLM = frozenset({
    "b6cb148688ac0b3ca2bcdc0c67748ddbce5cf233ee588802e27b6e15618959ea",
    "d23f99f8ee65669a557f1ad671080fa0a68e36edad8afc31ca2f2c3b9d2f8e29",
    "bae096a7fc9204865ea5fc4511fecec4b9f904996c411108474f882e67eb09ef",
})


def _clear_retired_llm(prefs) -> None:
    """Стирает сохранённые адрес и имя модели, которые больше не входят в сборку."""
    for name in ("llm_base_url", "llm_model"):
        value = str(getattr(prefs, name, "") or "").strip().rstrip("/")
        if not value:
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if digest in _RETIRED_LLM:
            setattr(prefs, name, "")


def register() -> None:
    chatgpt_auth.ensure_vendor_path()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    placement.register()
    building_export.register()
    scene_chat.register()
    bpy.types.Scene.dwgreader_source = StringProperty(
        name="Чертёж",
        description="Файл DWG или DXF",
        subtype="FILE_PATH",
        default="",
        update=_on_source_path,
    )
    bpy.types.Scene.dwgreader_status = StringProperty(name="Статус", default="")
    bpy.types.Scene.dwgreader_classified = StringProperty(name="Разобранный чертёж", default="")
    bpy.types.Scene.dwgreader_geometry = EnumProperty(
        name="Отрисовка",
        description="Способ отрисовки для всех слоёв. «По слоям» — у каждого слоя свой",
        items=GEOMETRY_PRESETS,
        default="fills",
        update=_on_geometry_preset,
    )
    bpy.types.Scene.dwgreader_selected = CollectionProperty(type=DWGREADER_LayerItem)
    bpy.types.Scene.dwgreader_ignored = CollectionProperty(type=DWGREADER_LayerItem)
    bpy.types.Scene.dwgreader_selected_index = IntProperty(default=0, update=_activate_listed_layer)
    bpy.types.Scene.dwgreader_ignored_index = IntProperty(default=0)
    bpy.types.Scene.dwgreader_screen = EnumProperty(name="Экран", items=SCREENS, default="home")
    bpy.types.Scene.dwgreader_step = IntProperty(name="Шаг", default=1, min=1, max=3)
    bpy.types.Scene.dwgreader_filter = StringProperty(
        name="Фильтр",
        description="Фильтр по имени слоя или типу",
        default="",
        options={"TEXTEDIT_UPDATE"},
    )
    bpy.types.Scene.dwgreader_show_unknown = BoolProperty(
        name="Показать нераспознанные",
        description="Показать в списке слои, которым модель не нашла тип",
        default=False,
    )
    bpy.types.Scene.dwgreader_show_params = BoolProperty(
        name="Параметры построения",
        description="Показать масштаб, начало координат и вырезание отметок",
        default=False,
    )
    bpy.types.Scene.dwgreader_show_layers = BoolProperty(
        name="Слои",
        description="Показать список слоёв и план активного слоя",
        default=False,
    )
    bpy.types.Scene.dwgreader_plan_zoom = IntProperty(
        name="Масштаб плана",
        description="Во сколько раз увеличить план активного слоя",
        default=1,
        min=1,
        max=4,
    )
    bpy.types.Scene.dwgreader_auto_trees = BoolProperty(
        name="Расстановка деревьев",
        description="После построения расставить деревья и кусты на всех газонах",
        default=True,
    )
    bpy.types.Scene.dwgreader_auto_maf = BoolProperty(
        name="МАФ на площадках",
        description="После построения заполнить каждую площадку несколькими моделями, зона безопасности остаётся внутри полигона",
        default=True,
    )
    bpy.types.Scene.dwgreader_auto_buildings = BoolProperty(
        name="Расстановка зданий",
        description="После построения поставить дом на каждый полигон зданий",
        default=True,
    )
    bpy.types.Scene.dwgreader_auto_prune_sheets = BoolProperty(
        name="Убрать мелкие чертежи",
        description="До расстановки удалить чертежи, в которых объектов меньше 75% от самого большого. Близкие по числу объектов остаются на выбор",
        default=True,
    )
    bpy.types.Scene.dwgreader_skip_small_buildings = BoolProperty(
        name="Не ставить на мелкие пятна",
        description="Не ставить геометрические ноды дома на полигоны зданий меньше заданной площади. Подложка, деревья и МАФ не затрагиваются",
        default=True,
    )
    bpy.types.Scene.dwgreader_min_building_area_m2 = FloatProperty(
        name="Минимальная площадь здания, м²",
        description="Полигон здания меньше этой площади не получает ноду дома. Действует только при включённой галочке",
        default=50.0,
        min=0.0,
        soft_max=500.0,
        precision=1,
    )
    bpy.types.Scene.dwgreader_layer_memory = StringProperty(default="{}", options={"HIDDEN"})
    bpy.types.Scene.dwgreader_build_seconds = FloatProperty(default=0.0, options={"HIDDEN"})
    bpy.types.Scene.dwgreader_chatgpt_block = StringProperty(default="", options={"HIDDEN"})
    bpy.types.Scene.dwgreader_sheet_shown = IntProperty(default=-1, options={"HIDDEN"})
    bpy.types.Scene.dwgreader_sheet_gap = FloatProperty(
        name="Зазор чертежей, м",
        description="Объекты подложки дальше этого зазора считаются разными чертежами на одном листе",
        default=SHEET_GAP_M,
        min=1.0,
        soft_max=5000.0,
        precision=0,
    )
    bpy.types.WindowManager.dwgreader_busy = BoolProperty(name="Идёт импорт", default=False)
    global _PREVIEWS
    _PREVIEWS = previews.new()
    if not bpy.app.timers.is_registered(_load_previews):
        bpy.app.timers.register(_load_previews, first_interval=0.01)
    if not bpy.app.timers.is_registered(_ping_timer):
        bpy.app.timers.register(_ping_timer, first_interval=2.0, persistent=True)
    if not bpy.app.timers.is_registered(_restore_chatgpt_models):
        bpy.app.timers.register(_restore_chatgpt_models, first_interval=0.4)
    try:
        addon = bpy.context.preferences.addons.get(__package__ or __name__)
    except Exception:
        addon = None
    if addon is not None:
        _clear_retired_llm(addon.preferences)


def unregister() -> None:
    global _PREVIEWS
    if bpy.app.timers.is_registered(_load_previews):
        bpy.app.timers.unregister(_load_previews)
    if bpy.app.timers.is_registered(_ping_timer):
        bpy.app.timers.unregister(_ping_timer)
    if bpy.app.timers.is_registered(_restore_chatgpt_models):
        bpy.app.timers.unregister(_restore_chatgpt_models)
    if _PREVIEWS is not None:
        previews.remove(_PREVIEWS)
        _PREVIEWS = None
    scene_chat.unregister()
    placement.unregister()
    building_export.unregister()
    for name in (
        "dwgreader_sheet_shown",
        "dwgreader_chatgpt_block",
        "dwgreader_sheet_gap",
        "dwgreader_build_seconds",
        "dwgreader_layer_memory",
        "dwgreader_show_params",
        "dwgreader_show_layers",
        "dwgreader_plan_zoom",
        "dwgreader_min_building_area_m2",
        "dwgreader_skip_small_buildings",
        "dwgreader_auto_buildings",
        "dwgreader_auto_prune_sheets",
        "dwgreader_auto_maf",
        "dwgreader_auto_trees",
        "dwgreader_show_unknown",
        "dwgreader_filter",
        "dwgreader_step",
        "dwgreader_screen",
    ):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    del bpy.types.WindowManager.dwgreader_busy
    del bpy.types.Scene.dwgreader_ignored_index
    del bpy.types.Scene.dwgreader_selected_index
    del bpy.types.Scene.dwgreader_ignored
    del bpy.types.Scene.dwgreader_selected
    del bpy.types.Scene.dwgreader_classified
    del bpy.types.Scene.dwgreader_geometry
    del bpy.types.Scene.dwgreader_status
    del bpy.types.Scene.dwgreader_source
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
