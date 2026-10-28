from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from pathlib import Path
from typing import BinaryIO


root = Path(__file__).resolve().parent
addon = root / "blender_addon" / "dwgreader_underlay"
zip_path = root / "blender_addon" / "dwgreader_underlay.zip"

SURFACE = {
    "зелень6.jpg": "grass.jpg",
    "плитка 1.jpg": "pavers.jpg",
    "тротуар1.jpg": "sidewalk.jpg",
    "асфальт.png": "asphalt.png",
    "резиновая крошка.jpg": "rubber.jpg",
}

SourceMap = dict[str, Path]


def _put(files: SourceMap, source: Path, arcname: str) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Нет файла сборки: {source}")
    if arcname in files:
        raise RuntimeError(f"Два источника для одного файла архива: {arcname}")
    files[arcname] = source


def pack_python(files: SourceMap, folder: Path, arc_root: str) -> None:
    if not folder.is_dir():
        raise FileNotFoundError(f"Нет папки с кодом: {folder}")
    for path in sorted(folder.rglob("*.py")):
        if "__pycache__" in path.parts or "vendor" in path.parts:
            continue
        if path.name == "build_hatch_underlay.py":
            continue
        _put(files, path, f"{arc_root}/{path.relative_to(folder).as_posix()}")


def add_building(files: SourceMap, fallbacks: set[str], project: Path, existing_zip: Path | None) -> None:
    source = project / "actual_node"
    blend = source / "Building 2.0.blend"
    if blend.is_file():
        _put(files, blend, "dwgreader_underlay/building/Building 2.0.blend")
        textures = source / "textures"
        if textures.is_dir():
            for path in sorted(textures.rglob("*")):
                if not path.is_file() or path.suffix.lower() == ".psd":
                    continue
                _put(files, path, f"dwgreader_underlay/building/{path.relative_to(source).as_posix()}")
        return
    if existing_zip is None:
        raise FileNotFoundError("Нет actual_node/Building 2.0.blend и прежнего архива с ресурсами здания")
    with zipfile.ZipFile(existing_zip) as previous:
        for name in previous.namelist():
            if name.startswith("dwgreader_underlay/building/") and not name.endswith("/"):
                fallbacks.add(name)
    if "dwgreader_underlay/building/Building 2.0.blend" not in fallbacks:
        raise FileNotFoundError("В прежнем архиве нет building/Building 2.0.blend")


def add_surface_textures(files: SourceMap, project: Path, addon_dir: Path) -> None:
    blago = project / "blago_textures"
    packed: set[str] = set()
    if blago.is_dir():
        for source_name, packed_name in SURFACE.items():
            source = blago / source_name
            if source.is_file():
                _put(files, source, f"dwgreader_underlay/textures/{packed_name}")
                packed.add(packed_name)
    local = addon_dir / "textures"
    for packed_name in SURFACE.values():
        if packed_name not in packed:
            _put(files, local / packed_name, f"dwgreader_underlay/textures/{packed_name}")
            packed.add(packed_name)


def addon_version(project: Path) -> tuple[int, ...]:
    init_path = project / "blender_addon" / "dwgreader_underlay" / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(isinstance(target, ast.Name) and target.id == "bl_info" for target in node.targets):
            continue
        info = ast.literal_eval(node.value)
        version = tuple(int(part) for part in info["version"])
        if version:
            return version
    raise RuntimeError("В __init__.py не найдена версия bl_info")


def _collect_sources(project: Path, previous: Path | None) -> tuple[SourceMap, set[str]]:
    addon_dir = project / "blender_addon" / "dwgreader_underlay"
    library = project / "maf_blender_models"
    if not library.is_dir():
        raise FileNotFoundError(f"Нет библиотеки моделей: {library}")

    files: SourceMap = {}
    fallbacks: set[str] = set()
    pack_python(files, addon_dir, "dwgreader_underlay")
    pack_python(files, project / "dxf_read", "dwgreader_underlay/dxf_read")
    for icon in sorted((addon_dir / "icons").glob("*.png")):
        _put(files, icon, f"dwgreader_underlay/icons/{icon.name}")
    for model in sorted(library.iterdir()):
        if model.is_file() and model.name != "catalog.json" and model.suffix.lower() in {".glb", ".json"}:
            _put(files, model, f"dwgreader_underlay/library/{model.name}")
    _put(files, library / "catalog.json", "dwgreader_underlay/library/catalog.json")
    add_building(files, fallbacks, project, previous)
    add_surface_textures(files, project, addon_dir)
    return files, fallbacks


def _digest(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(
    archive_path: Path,
    files: SourceMap,
    fallbacks: set[str],
    previous: Path | None,
) -> None:
    """Не даёт подменить актуальный код старым или потерять ресурсы при сборке."""
    with zipfile.ZipFile(archive_path) as packed:
        damaged = packed.testzip()
        if damaged:
            raise RuntimeError(f"Повреждённый файл в новом архиве: {damaged}")
        names = packed.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("В новом архиве есть повторяющиеся имена файлов")
        expected = set(files) | fallbacks
        missing = sorted(expected - set(names))
        unexpected = sorted(set(names) - expected)
        if missing or unexpected:
            raise RuntimeError(f"Состав архива отличается от источников. Нет: {missing}; лишние: {unexpected}")
        for arcname, source in files.items():
            with packed.open(arcname) as archived, source.open("rb") as original:
                if _digest(archived) != _digest(original):
                    raise RuntimeError(f"Файл в архиве не совпадает с исходником: {arcname}")
        if fallbacks:
            if previous is None:
                raise RuntimeError("Для проверки ресурсов здания нет прежнего архива")
            with zipfile.ZipFile(previous) as old:
                for arcname in sorted(fallbacks):
                    with packed.open(arcname) as archived, old.open(arcname) as original:
                        if _digest(archived) != _digest(original):
                            raise RuntimeError(f"Не удалось сохранить ресурс здания: {arcname}")

        catalog = json.loads(packed.read("dwgreader_underlay/library/catalog.json").decode("utf-8"))
        if not isinstance(catalog, list):
            raise RuntimeError("catalog.json должен содержать список моделей")
        ids = [str(item.get("id") or "") for item in catalog if isinstance(item, dict)]
        if len(ids) != len(set(ids)) or not all(ids):
            raise RuntimeError("В catalog.json есть пустые или повторяющиеся id")
        for item in catalog:
            model = "dwgreader_underlay/library/" + str(item["file"])
            if model not in names:
                raise RuntimeError(f"Модель из catalog.json не попала в архив: {item['file']}")


def build_zip(project: Path, dest: Path) -> Path:
    project = project.resolve()
    dest = dest.resolve()
    addon_version(project)
    dest.parent.mkdir(parents=True, exist_ok=True)
    previous = dest if dest.is_file() else None
    files, fallbacks = _collect_sources(project, previous)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as packed:
            for arcname, source in sorted(files.items()):
                packed.write(source, arcname)
            if fallbacks:
                if previous is None:
                    raise RuntimeError("Нет источника ресурсов здания")
                with zipfile.ZipFile(previous) as old:
                    for arcname in sorted(fallbacks):
                        packed.writestr(arcname, old.read(arcname))
        _verify_archive(tmp, files, fallbacks, previous)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


if __name__ == "__main__":
    built = build_zip(root, zip_path)
    version = ".".join(str(part) for part in addon_version(root))
    print(f"Собран и проверен {built} · версия {version}")
