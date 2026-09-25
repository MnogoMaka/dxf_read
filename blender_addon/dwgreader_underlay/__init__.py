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
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from .build_scene import build


bl_info = {
    "name": "Подложка DWG",
    "author": "DWGREADER",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "3D Viewport → Sidebar → Подложка",
    "description": "Импорт DWG/DXF в плоскую цветную подложку площадки",
    "category": "Import-Export",
}

MARKER = "UNDERLAY_JSON="
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


def _blender_python() -> Path:
    prefix = Path(sys.prefix)
    for folder in (prefix / "bin", prefix):
        for name in ("python.exe", "python3.exe", "python3", "python"):
            candidate = folder / name
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        "Не найден Python рядом с Blender. Библиотеки ставятся его pip."
    )


def _oda_installed(prefs=None) -> bool:
    """Есть ли уже ODAFileConverter.exe: путь из настроек или стандартные папки."""
    if prefs is not None and prefs.oda_converter.strip():
        return Path(bpy.path.abspath(prefs.oda_converter.strip())).is_file()
    roots = (
        Path(r"C:\Program Files\ODA"),
        Path(r"C:\Program Files (x86)\ODA"),
        Path(r"C:\Program Files"),
    )
    for root in roots:
        if not root.is_dir():
            continue
        if any(root.glob("ODAFileConverter*/ODAFileConverter.exe")):
            return True
        if any(root.glob("ODAFileConverter.exe")):
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
        description="ODAFileConverter.exe. Пусто — искать в Program Files. Нужен только для DWG",
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
        default="google/gemma-4-e4b",
    )
    llm_base_url: StringProperty(
        name="Адрес модели",
        default="http://127.0.0.1:1234/v1",
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

    def draw(self, context):
        layout = self.layout
        layout.operator("dwgreader.install_deps", icon="IMPORT")
        layout.prop(self, "oda_converter")
        if not _oda_installed(self):
            layout.operator("dwgreader.install_oda", icon="IMPORT")
        layout.prop(self, "output_dir")

        services = layout.box()
        services.label(text="Классификация слоёв")
        services.prop(self, "llm_model")
        services.prop(self, "llm_base_url")
        services.prop(self, "llm_api_key")
        services.prop(self, "llm_timeout")
        services.prop(self, "batch_size")

        geometry = layout.box()
        geometry.label(text="Геометрия")
        _draw_geometry(geometry, self)

        tokens = layout.box()
        tokens.label(text="Имена слоёв")
        tokens.prop(self, "fill_polyline_tokens")
        tokens.prop(self, "junk_layer_tokens")


def _draw_geometry(layout, prefs) -> None:
    layout.prop(prefs, "site_cluster_radius_m")
    layout.prop(prefs, "generic_fill_associate_m")
    layout.prop(prefs, "min_arrangement_area_m2")
    layout.prop(prefs, "flattening_distance_m")


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
    bl_description = "Ставит ODA File Converter через winget, если его ещё нет в системе"

    _timer = None
    _proc = None
    _thread = None
    _state: dict | None = None

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    def invoke(self, context, event):
        if os.name != "nt":
            self.report({"ERROR"}, "Установка через winget доступна только в Windows")
            return {"CANCELLED"}
        winget = shutil.which("winget")
        if not winget:
            self.report({"ERROR"}, "winget не найден. Нужны Windows 10 или 11 с App Installer")
            return {"CANCELLED"}
        if _oda_installed(_addon_prefs(context)):
            self.report({"INFO"}, "ODA File Converter уже установлен")
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


class DWGREADER_OT_pick_file(Operator):
    bl_idname = "dwgreader.pick_file"
    bl_label = "Выбрать чертёж"
    bl_description = "DWG или DXF"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.dwg;*.dxf;*.DWG;*.DXF", options={"HIDDEN"})

    def execute(self, context):
        context.scene.dwgreader_source = self.filepath
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class DWGREADER_OT_import(Operator):
    bl_idname = "dwgreader.import_underlay"
    bl_label = "Собрать подложку"
    bl_description = "Конвертировать чертёж и добавить подложку в сцену"
    bl_options = {"REGISTER", "UNDO"}

    _timer = None
    _proc = None
    _thread = None
    _state: dict | None = None

    @classmethod
    def poll(cls, context):
        return not context.window_manager.dwgreader_busy

    def invoke(self, context, event):
        prefs = _addon_prefs(context)
        if prefs is None:
            self.report({"ERROR"}, "Настройки аддона недоступны")
            return {"CANCELLED"}
        if not _deps_ready():
            self.report({"ERROR"}, "Сначала нажмите «Установить библиотеки»")
            return {"CANCELLED"}
        source = Path(bpy.path.abspath(context.scene.dwgreader_source.strip()))
        if source.suffix.lower() not in {".dwg", ".dxf"} or not source.is_file():
            self.report({"ERROR"}, "Выберите существующий файл .dwg или .dxf")
            return {"CANCELLED"}
        if source.suffix.lower() == ".dwg" and not _oda_installed(prefs):
            self.report({"ERROR"}, "Для DWG нужен ODA File Converter. Нажмите «Установить ODA File Converter»")
            return {"CANCELLED"}
        try:
            python = _blender_python()
            engine = _engine_dir()
        except FileNotFoundError as exc:
            self.report({"ERROR"}, str(exc))
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
        ]
        return _start_process(
            self, context, command, cwd=engine, env=_python_env(),
            title="Конвертация и классификация слоёв…",
            log_path=out / "dwgreader_import.log",
        )

    def modal(self, context, event):
        return _poll_process(
            self, context, event,
            on_ok="",
            on_err="Импорт не удался",
            build=True,
        )

    def _stop(self, context, kill: bool) -> None:
        _stop_process(self, context, kill)


class DWGREADER_PT_panel(Panel):
    bl_label = "Подложка DWG"
    bl_idname = "DWGREADER_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Подложка"

    def draw(self, context):
        layout = self.layout
        prefs = _addon_prefs(context)
        scene = context.scene
        if not _deps_ready():
            layout.label(text="Библиотеки разбора ещё не стоят")
            layout.operator("dwgreader.install_deps", icon="IMPORT")
            layout.separator()
        if not _oda_installed(prefs):
            layout.label(text="Для DWG не найден ODA File Converter")
            layout.operator("dwgreader.install_oda", icon="IMPORT")
            layout.separator()
        layout.prop(scene, "dwgreader_source", text="")
        layout.operator("dwgreader.pick_file", icon="FILEBROWSER")
        layout.operator("dwgreader.import_underlay", icon="IMPORT")
        if scene.dwgreader_status:
            layout.label(text=scene.dwgreader_status)
        if prefs is None:
            return
        layout.separator()
        box = layout.box()
        box.label(text="Геометрия")
        _draw_geometry(box, prefs)
        box.prop(prefs, "batch_size")
        layout.label(text="Модель, ODA и токены слоёв — в настройках аддона")


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

    state = {"status": title, "tail": [], "json": "", "log": log_path or (cwd / "dwgreader_import.log")}
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
                elif line:
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


def _poll_process(operator, context, event, on_ok: str, on_err: str, build: bool = False):
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

    if build:
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
        context.scene.dwgreader_status = f"Готово: {count} слоёв"
        operator.report({"INFO"}, f"Подложка собрана, слоёв: {count}")
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
    return build(json_path)


CLASSES = (
    DWGREADER_AddonPreferences,
    DWGREADER_OT_install_deps,
    DWGREADER_OT_install_oda,
    DWGREADER_OT_pick_file,
    DWGREADER_OT_import,
    DWGREADER_PT_panel,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dwgreader_source = StringProperty(
        name="Чертёж",
        description="Файл DWG или DXF",
        subtype="FILE_PATH",
        default="",
    )
    bpy.types.Scene.dwgreader_status = StringProperty(name="Статус", default="")
    bpy.types.WindowManager.dwgreader_busy = BoolProperty(name="Идёт импорт", default=False)


def unregister() -> None:
    del bpy.types.WindowManager.dwgreader_busy
    del bpy.types.Scene.dwgreader_status
    del bpy.types.Scene.dwgreader_source
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
