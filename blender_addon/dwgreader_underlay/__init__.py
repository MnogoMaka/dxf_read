"""Аддон Blender: DWG/DXF → плоская подложка площадки.

Код разбора чертежа лежит в комплекте (пакет dxf_read). Считает его Python,
который идёт вместе с Blender. Папку проекта указывать не нужно.

Установка: zip этой папки → Edit → Preferences → Add-ons → Install from Disk.
После включения один раз нажмите «Установить библиотеки».
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList
from bpy.utils import previews

def _reload_stale_modules() -> None:
    """Установка zip выгружает сам пакет, а подмодули старой версии остаются в sys.modules."""
    import importlib

    for name in ("placement_math", "build_scene", "placement"):
        module = sys.modules.get(f"{__name__}.{name}")
        if module is not None:
            importlib.reload(module)


_reload_stale_modules()

from . import placement
from .build_scene import build, remove_root


bl_info = {
    "name": "Подложка DWG",
    "author": "DWGREADER",
    "version": (1, 4, 2),
    "blender": (4, 0, 0),
    "location": "3D Viewport → Sidebar → Подложка",
    "description": "DXF → проверка слоёв → благоустройство в сцене",
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
    ("green", "Газоны", "Газоны и цветники"),
    ("vegetation", "Посадки", "Деревья и кустарники"),
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
SPLIT_MODES = (
    ("layer", "Один объект на слой", "Все полигоны слоя AutoCAD в одном объекте"),
    ("polygon", "Каждый полигон отдельно", "Здание_1, Здание_2… Касающиеся полигоны — один объект"),
    ("gap", "Группы по зазору", "Полигоны ближе зазора вместе, дальние — отдельными объектами"),
)
SCREENS = (
    ("home", "Инструменты", ""),
    ("import", "Импорт DXF", ""),
    ("place", "Озеленение и МАФ", ""),
)
PIP_PACKAGES = ("ezdxf", "shapely", "numpy", "mapbox-earcut")
WINGET_ODA_ID = "ODA.ODAFileConverter"
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
    folders = [Path(sys.prefix) / "bin", Path(sys.prefix)]
    binary = Path(getattr(bpy.app, "binary_path", "") or "")
    if not binary:
        return folders
    # macOS: Blender.app/Contents/MacOS/Blender → Contents/Resources/4.x/python/bin
    resources = binary.parent.parent / "Resources"
    if resources.is_dir():
        folders.extend(resources.glob("*/python/bin"))
    folders.extend(binary.parent.glob("*/python/bin"))
    return folders


def _is_python_binary(path: Path) -> bool:
    name = path.name.lower()
    if not path.is_file() or "config" in name:
        return False
    return name.startswith("python") and not name.endswith((".dll", ".dylib", ".so"))


def _blender_python() -> Path:
    found: list[Path] = []
    seen: set[Path] = set()
    for folder in _python_folders():
        if not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            resolved = candidate.resolve()
            if resolved in seen or not _is_python_binary(candidate):
                continue
            seen.add(resolved)
            found.append(candidate)
    found.sort(key=lambda path: (not path.name.lower().startswith("python3."), path.name.lower()))
    if found:
        return found[0]
    raise FileNotFoundError(
        "Не найден Python рядом с Blender. Библиотеки ставятся его pip."
    )


def _oda_roots() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        return (Path("/Applications"), Path.home() / "Applications")
    if sys.platform.startswith("linux"):
        return (Path("/usr/bin"), Path("/usr/local/bin"), Path.home())
    return (
        Path(r"C:\Program Files\ODA"),
        Path(r"C:\Program Files (x86)\ODA"),
        Path(r"C:\Program Files"),
    )


def _oda_installed(prefs=None) -> bool:
    """Есть ли ODA File Converter: путь из настроек или стандартные папки системы."""
    if prefs is not None and prefs.oda_converter.strip():
        return Path(bpy.path.abspath(prefs.oda_converter.strip())).is_file()
    for root in _oda_roots():
        if not root.is_dir():
            continue
        patterns = (
            "ODAFileConverter*/ODAFileConverter.exe",
            "ODAFileConverter.exe",
            "ODAFileConverter*.app/Contents/MacOS/ODAFileConverter",
            "ODAFileConverter",
        )
        if any(path.is_file() for pattern in patterns for path in root.glob(pattern)):
            return True
    return False


def _deps_ready() -> bool:
    vendor = _vendor_dir()
    return all((vendor / name).exists() for name in ("ezdxf", "shapely", "mapbox_earcut", "numpy"))


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
        pointer = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)) == 0:
            documents = Path(pointer.value)
            ole32.CoTaskMemFree(pointer)
            return documents / "converter"
    return Path.home() / "Documents" / "converter"


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
        "site_cluster_radius_m": float(prefs.site_cluster_radius_m),
        "generic_fill_associate_m": float(prefs.generic_fill_associate_m),
        "min_arrangement_area_m2": float(prefs.min_arrangement_area_m2),
        "flattening_distance_m": float(prefs.flattening_distance_m),
        "cut_vegetation": bool(prefs.cut_vegetation),
        "scene_scale": float(prefs.scene_scale),
        "origin_mode": str(prefs.origin_mode),
        "batch_size": int(prefs.batch_size),
        "llm_timeout": float(prefs.llm_timeout),
        "fill_polyline_tokens": _tokens(prefs.fill_polyline_tokens),
        "junk_layer_tokens": _tokens(prefs.junk_layer_tokens),
        "llm_model": prefs.llm_model.strip(),
        "llm_base_url": prefs.llm_base_url.strip(),
        "llm_api_key": prefs.llm_api_key,
        "oda_converter": oda,
        "output_dir": str(_output_dir(prefs, source)),
    }


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_vendor_dir()), str(_engine_dir()), env.get("PYTHONPATH", "")) if part
    )
    return env


def _redraw(context) -> None:
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


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
        default="qwen/qwen3-vl-30b-a3b-instruct",
    )
    llm_base_url: StringProperty(
        name="Адрес модели",
        default="https://llm-router.cxm.dev/v1/",
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
    site_cluster_radius_m: FloatProperty(
        name="Радиус кластера, м",
        description="В одном пятне остаются заливки не дальше этого радиуса друг от друга",
        default=400000.0,
        min=1.0,
        soft_max=400000.0,
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
    cut_vegetation: BoolProperty(
        name="Вырезать деревья и кусты",
        description="Контуры деревьев и кустов прорезают газоны и покрытия. Выключено — площадки остаются целыми, зелень лежит своим слоем",
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
    library_dir: StringProperty(
        name="Библиотека моделей",
        description="Папка с catalog.json и отдельным GLB на каждую модель",
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.operator("dwgreader.install_deps", icon="IMPORT")
        layout.prop(self, "oda_converter")
        if not _oda_installed(self):
            layout.operator("dwgreader.install_oda", icon="IMPORT")
        layout.prop(self, "output_dir")
        layout.prop(self, "library_dir")

        services = layout.box()
        services.label(text="Классификация слоёв")
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
    layout.prop(prefs, "site_cluster_radius_m")
    layout.prop(prefs, "generic_fill_associate_m")
    layout.prop(prefs, "min_arrangement_area_m2")
    layout.prop(prefs, "flattening_distance_m")
    layout.prop(prefs, "cut_vegetation")


def _draw_split(layout, prefs) -> None:
    layout.prop(prefs, "split_mode")
    if prefs.split_mode == "gap":
        layout.prop(prefs, "layer_part_gap_m", text="Зазор, м")
    layout.prop(prefs, "by_type_collections")


class DWGREADER_OT_install_deps(Operator):
    bl_idname = "dwgreader.install_deps"
    bl_label = "Установить библиотеки"
    bl_description = "Ставит ezdxf, shapely и earcut в папку аддона через Python Blender"

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


class DWGREADER_OT_install_oda(Operator):
    bl_idname = "dwgreader.install_oda"
    bl_label = "Установить ODA File Converter"
    bl_description = "Ставит ODA File Converter, если его ещё нет: winget в Windows, страница загрузки на Mac"

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
        if sys.platform == "darwin":
            import webbrowser
            webbrowser.open("https://www.opendesign.com/guestfiles/oda_file_converter")
            self.report(
                {"INFO"},
                "Открыта загрузка ODA. Поставьте приложение в /Applications и запустите импорт снова",
            )
            return {"FINISHED"}
        if os.name != "nt":
            self.report({"ERROR"}, "Скачайте ODA File Converter с сайта Open Design Alliance и укажите путь в настройках")
            return {"CANCELLED"}
        winget = shutil.which("winget")
        if not winget:
            self.report({"ERROR"}, "winget не найден. Нужны Windows 10 или 11 с App Installer")
            return {"CANCELLED"}
        command = [
            winget, "install", "--id", WINGET_ODA_ID, "-e",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ]
        log_dir = Path(DEFAULT_OUTPUT)
        log_dir.mkdir(parents=True, exist_ok=True)
        return _start_process(
            self, context, command, cwd=log_dir, env=os.environ.copy(),
            title="Установка ODA File Converter…",
            log_path=log_dir / "dwgreader_oda_install.log",
        )

    def modal(self, context, event):
        return _poll_process(
            self, context, event,
            on_ok="ODA File Converter установлен",
            on_err="Не удалось установить ODA File Converter",
        )

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


def _preview(key: str) -> int:
    if _PREVIEWS is None:
        return 0
    item = _PREVIEWS.get(key)
    if item is None:
        path = _addon_dir() / "icons" / f"{key}.png"
        if not path.is_file():
            return 0
        item = _PREVIEWS.load(key, str(path), "IMAGE")
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


def _launch_pipeline(operator, context, extra: list[str], title: str, mode: str):
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
    return _start_process(
        operator, context, command, cwd=engine, env=_python_env(),
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
    preset = scene.dwgreader_geometry
    default_geometry = preset if preset in {"fills", "curves", "both"} else "fills"
    scene.dwgreader_selected.clear()
    scene.dwgreader_ignored.clear()
    seen: set[str] = set()
    used_geometry: set[str] = set()
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
            item.geometry = default_geometry
            item.fills = row["fills"]
            item.curves = row["curves"]
            item.entities = row["entities"]
            saved = memory.get(row["name"])
            if isinstance(saved, list) and len(saved) == 3:
                item.build = bool(saved[0])
                if saved[1] in CATEGORY_IDS:
                    item.category = saved[1]
                if saved[2] in {"fills", "curves", "both"}:
                    item.geometry = saved[2]
            used_geometry.add(item.geometry)
        if len(used_geometry) > 1:
            scene.dwgreader_geometry = "per_layer"
        elif used_geometry:
            scene.dwgreader_geometry = next(iter(used_geometry))
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

    def invoke(self, context, event):
        return _launch_pipeline(
            self, context, ["--classify-only"],
            "Разбор чертежа и классификация слоёв…",
            "classify",
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
    bl_description = "Поставить или снять галочку у всех слоёв, видимых в списке"
    bl_options = {"INTERNAL", "UNDO"}

    state: BoolProperty(default=True)

    def execute(self, context):
        scene = context.scene
        for item in scene.dwgreader_selected:
            if _layer_visible(item, scene):
                item.build = self.state
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


class DWGREADER_OT_open_chat(Operator):
    bl_idname = "dwgreader.open_chat"
    bl_label = "Открыть чат"
    bl_description = "Генерация объектов по описанию. Скоро"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        self.report({"INFO"}, "Генерация по описанию скоро появится")
        context.scene.dwgreader_status = "Генерация по описанию скоро появится"
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
    bl_description = "Собрать подложку в сцене из отмеченных слоёв. Ctrl+Z отменяет весь импорт"
    bl_options = {"UNDO"}

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
        for item in scene.dwgreader_selected:
            name = item.layer_name.strip()
            if name and item.build:
                groups.setdefault(item.category, []).append(name)
                per_layer[name] = item.geometry
        if not groups:
            self.report({"ERROR"}, "Не отмечено ни одного слоя")
            return {"CANCELLED"}
        _remember_layers(scene)
        out = _output_dir(prefs, source)
        groups_path = out / "_confirmed_layers.json"
        groups_path.write_text(
            json.dumps({"by_category": groups, "geometry_by_layer": per_layer}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
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


def _draw_setup(layout, prefs) -> None:
    if not _deps_ready():
        box = layout.box()
        box.label(text="Библиотеки разбора ещё не стоят", icon="ERROR")
        box.operator("dwgreader.install_deps", icon="IMPORT")
    if not _oda_installed(prefs):
        box = layout.box()
        box.label(text="Для DWG не найден ODA File Converter", icon="INFO")
        box.operator("dwgreader.install_oda", icon="IMPORT")


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


def _draw_step_file(layout, context, prefs) -> None:
    scene = context.scene
    box = layout.box()
    box.label(text="Импорт DXF · благоустройство", icon="IMPORT")
    box.prop(scene, "dwgreader_source", text="")
    if prefs is not None:
        col = box.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(prefs, "scene_scale")
        col.prop(prefs, "origin_mode")
        params = box.box()
        params.prop(
            scene, "dwgreader_show_params",
            text="Параметры разбора",
            icon="TRIA_DOWN" if scene.dwgreader_show_params else "TRIA_RIGHT",
            emboss=False,
        )
        if scene.dwgreader_show_params:
            col = params.column()
            col.use_property_split = True
            col.use_property_decorate = False
            _draw_geometry(col, prefs)
            col.prop(prefs, "batch_size")
    run = box.row()
    run.scale_y = 1.4
    run.operator("dwgreader.classify_layers", text="Анализировать чертёж", icon="VIEWZOOM")
    hint = layout.column(align=True)
    hint.scale_y = 0.75
    hint.label(text="Анализ не меняет ни чертёж, ни сцену.", icon="INFO")
    hint.label(text="Геометрия появится после шага 2.")


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
    row = box.row(align=True)
    row.prop(scene, "dwgreader_filter", text="", icon="VIEWZOOM")
    row.operator("dwgreader.mark_layers", text="Все").state = True
    row.operator("dwgreader.mark_layers", text="Ничего").state = False

    head = box.row(align=True)
    head.label(text="", icon="CHECKBOX_HLT")
    name, rest, tail = _layer_columns(head)
    name.label(text="Слой")
    rest.label(text="Тип")
    tail.label(text="Отрисовка")
    tail.label(text="DXF")
    box.template_list(
        "DWGREADER_UL_layers", "layers",
        scene, "dwgreader_selected",
        scene, "dwgreader_selected_index",
        rows=10,
    )
    if summary["unknown"]:
        warn = box.row(align=True)
        warn.label(
            text=f"{_layers_text(summary['unknown'])}, {_objects_text(summary['unknown_objects'])} — тип не распознан",
            icon="ERROR",
        )
        warn.prop(
            scene, "dwgreader_show_unknown",
            text="Скрыть" if scene.dwgreader_show_unknown else "Показать",
            toggle=True,
        )

    if prefs is not None:
        params = layout.box()
        params.label(text="Параметры построения")
        col = params.column()
        col.use_property_split = True
        col.use_property_decorate = False
        _draw_split(col, prefs)

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
        screen = scene.dwgreader_screen
        if screen == "import":
            _draw_import(layout, context, prefs)
        elif screen == "place":
            _draw_place(layout, context)
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
        operator.report({"WARNING"}, message)
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
        try:
            count = build_underlay(json_path)
        except Exception as exc:
            context.scene.dwgreader_status = f"Ошибка сборки: {exc}"
            operator.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        import time as clock

        context.scene.dwgreader_build_seconds = max(clock.time() - _BUILD_STARTED, 0.0) if _BUILD_STARTED else 0.0
        context.scene.dwgreader_step = 3
        context.scene.dwgreader_status = f"Готово: объектов в сцене {count}"
        operator.report({"INFO"}, f"Благоустройство собрано, объектов: {count}")
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


def build_underlay(json_path: Path) -> int:
    prefs = _addon_prefs(bpy.context)
    if prefs is None:
        return build(json_path)
    gap = float(getattr(prefs, "layer_part_gap_m", 10.0))
    return build(
        json_path,
        gap,
        split_mode=str(getattr(prefs, "split_mode", "gap") or "gap"),
        by_type=bool(getattr(prefs, "by_type_collections", True)),
    )


CLASSES = (
    DWGREADER_AddonPreferences,
    DWGREADER_LayerItem,
    DWGREADER_UL_layers,
    DWGREADER_UL_place_layers,
    DWGREADER_OT_install_deps,
    DWGREADER_OT_install_oda,
    DWGREADER_OT_pick_file,
    DWGREADER_OT_classify,
    DWGREADER_OT_mark_layers,
    DWGREADER_OT_goto,
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


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    placement.register()
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
    bpy.types.Scene.dwgreader_show_params = BoolProperty(name="Параметры разбора", default=False)
    bpy.types.Scene.dwgreader_layer_memory = StringProperty(default="{}", options={"HIDDEN"})
    bpy.types.Scene.dwgreader_build_seconds = FloatProperty(default=0.0, options={"HIDDEN"})
    bpy.types.WindowManager.dwgreader_busy = BoolProperty(name="Идёт импорт", default=False)
    global _PREVIEWS
    _PREVIEWS = previews.new()
    if not bpy.app.timers.is_registered(_ping_timer):
        bpy.app.timers.register(_ping_timer, first_interval=2.0, persistent=True)
    try:
        addon = bpy.context.preferences.addons.get(__package__ or __name__)
    except Exception:
        addon = None
    if addon is not None:
        prefs = addon.preferences
        if prefs.llm_model in {"", "google/gemma-4-e4b"}:
            prefs.llm_model = "qwen/qwen3-vl-30b-a3b-instruct"
        if prefs.llm_base_url in {"", "http://127.0.0.1:1234/v1"}:
            prefs.llm_base_url = "https://llm-router.cxm.dev/v1/"
        if not str(prefs.library_dir or "").strip():
            candidate = Path(r"c:\Users\user\PycharmProjects\DWGREADER\maf_blender_models")
            if (candidate / "catalog.json").is_file():
                prefs.library_dir = str(candidate)


def unregister() -> None:
    global _PREVIEWS
    if bpy.app.timers.is_registered(_ping_timer):
        bpy.app.timers.unregister(_ping_timer)
    if _PREVIEWS is not None:
        previews.remove(_PREVIEWS)
        _PREVIEWS = None
    placement.unregister()
    for name in (
        "dwgreader_build_seconds",
        "dwgreader_layer_memory",
        "dwgreader_show_params",
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
