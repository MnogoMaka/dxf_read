import zipfile
from pathlib import Path

root = Path(r"c:\Users\user\PycharmProjects\DWGREADER")
addon = root / "blender_addon" / "dwgreader_underlay"
zip_path = root / "blender_addon" / "dwgreader_underlay.zip"


def pack(zf: zipfile.ZipFile, folder: Path, arc_root: str) -> None:
    for path in folder.rglob("*.py"):
        if "__pycache__" in path.parts or "vendor" in path.parts:
            continue
        if path.name == "build_hatch_underlay.py":
            continue
        zf.write(path, f"{arc_root}/{path.relative_to(folder).as_posix()}")


with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    # Каталог в корне архива: иначе Blender не видит пакет и не может заменить старую установку.
    zf.writestr("dwgreader_underlay/", "")
    zf.writestr("dwgreader_underlay/dxf_read/", "")
    pack(zf, addon, "dwgreader_underlay")
    pack(zf, root / "dxf_read", "dwgreader_underlay/dxf_read")
    zf.writestr("dwgreader_underlay/icons/", "")
    for icon in sorted((addon / "icons").glob("*.png")):
        zf.write(icon, f"dwgreader_underlay/icons/{icon.name}")
    zf.writestr("dwgreader_underlay/library/", "")
    library = root / "maf_blender_models"
    for model in sorted(library.iterdir()):
        if model.is_file() and model.suffix.lower() in {".glb", ".json"}:
            zf.write(model, f"dwgreader_underlay/library/{model.name}")

print(zip_path)
