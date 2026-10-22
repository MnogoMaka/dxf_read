import shutil
import tempfile
import zipfile
from pathlib import Path

root = Path(r"c:\Users\user\PycharmProjects\DWGREADER")
addon = root / "blender_addon" / "dwgreader_underlay"
zip_path = root / "blender_addon" / "dwgreader_underlay.zip"

SURFACE = {
    "зелень6.jpg": "grass.jpg",
    "плитка 1.jpg": "pavers.jpg",
    "тротуар1.jpg": "sidewalk.jpg",
    "асфальт.png": "asphalt.png",
    "резиновая крошка.jpg": "rubber.jpg",
}


def pack_python(zf: zipfile.ZipFile, folder: Path, arc_root: str) -> None:
    for path in folder.rglob("*.py"):
        if "__pycache__" in path.parts or "vendor" in path.parts:
            continue
        if path.name == "build_hatch_underlay.py":
            continue
        zf.write(path, f"{arc_root}/{path.relative_to(folder).as_posix()}")


def _copy_zip_prefix(zf: zipfile.ZipFile, source_zip: Path, prefix: str) -> int:
    if not source_zip.is_file():
        return 0
    copied = 0
    with zipfile.ZipFile(source_zip) as src:
        for name in src.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                zf.writestr(name, src.read(name))
                copied += 1
    return copied


def add_building(zf: zipfile.ZipFile, project: Path, existing_zip: Path) -> None:
    zf.writestr("dwgreader_underlay/building/", "")
    source = project / "actual_node"
    blend = source / "Building 2.0.blend"
    if blend.is_file():
        zf.write(blend, "dwgreader_underlay/building/Building 2.0.blend")
        textures = source / "textures"
        if textures.is_dir():
            for path in textures.rglob("*"):
                if not path.is_file() or path.suffix.lower() == ".psd":
                    continue
                zf.write(path, f"dwgreader_underlay/building/{path.relative_to(source).as_posix()}")
        return
    if _copy_zip_prefix(zf, existing_zip, "dwgreader_underlay/building/") == 0:
        raise FileNotFoundError("Нет actual_node/Building 2.0.blend и в текущем zip нет папки building")


def add_surface_textures(zf: zipfile.ZipFile, project: Path, addon_dir: Path) -> None:
    zf.writestr("dwgreader_underlay/textures/", "")
    blago = project / "blago_textures"
    packed: set[str] = set()
    if blago.is_dir():
        for source_name, packed_name in SURFACE.items():
            source = blago / source_name
            if source.is_file():
                zf.write(source, f"dwgreader_underlay/textures/{packed_name}")
                packed.add(packed_name)
    local = addon_dir / "textures"
    for packed_name in SURFACE.values():
        if packed_name in packed:
            continue
        source = local / packed_name
        if source.is_file():
            zf.write(source, f"dwgreader_underlay/textures/{packed_name}")
            packed.add(packed_name)
    missing = [name for name in SURFACE.values() if name not in packed]
    if missing:
        raise FileNotFoundError("Нет текстур поверхностей: " + ", ".join(missing))


def build_zip(project: Path, dest: Path) -> Path:
    addon_dir = project / "blender_addon" / "dwgreader_underlay"
    dest.parent.mkdir(parents=True, exist_ok=True)
    previous = dest if dest.is_file() else None
    snapshot = None
    if previous is not None:
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        handle.close()
        snapshot = Path(handle.name)
        shutil.copy2(previous, snapshot)
    tmp = dest.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("dwgreader_underlay/", "")
            zf.writestr("dwgreader_underlay/dxf_read/", "")
            pack_python(zf, addon_dir, "dwgreader_underlay")
            pack_python(zf, project / "dxf_read", "dwgreader_underlay/dxf_read")
            zf.writestr("dwgreader_underlay/icons/", "")
            for icon in sorted((addon_dir / "icons").glob("*.png")):
                zf.write(icon, f"dwgreader_underlay/icons/{icon.name}")
            zf.writestr("dwgreader_underlay/library/", "")
            library = project / "maf_blender_models"
            for model in sorted(library.iterdir()):
                if model.is_file() and model.suffix.lower() in {".glb", ".json"}:
                    zf.write(model, f"dwgreader_underlay/library/{model.name}")
            add_building(zf, project, snapshot or dest)
            add_surface_textures(zf, project, addon_dir)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
    return dest


if __name__ == "__main__":
    print(build_zip(root, zip_path))
