"""Сборка плоской подложки HATCH в Blender.

Каждый слой AutoCAD → отдельная коллекция под SITE_UNDERLAY.
Без выдавливания: треугольники в Z=0.

Запуск:
    blender --background --python build_hatch_underlay.py -- underlay.json out.blend
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


ROOT_NAME = "SITE_UNDERLAY"


def parse_paths() -> tuple[Path, Path | None]:
    argv = sys.argv
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    if not extra:
        raise SystemExit("Нужно: blender --python build_hatch_underlay.py -- <json> [blend]")
    json_path = Path(extra[0])
    blend_path = Path(extra[1]) if len(extra) > 1 else None
    return json_path, blend_path


def safe_name(text: str, fallback: str = "NO_LAYER") -> str:
    value = str(text or "").replace("\n", " ").replace("/", "_").replace("\\", "_").strip()
    value = value.strip(".") or fallback
    return value[:63]


def ensure_collection(name: str, parent: bpy.types.Collection) -> bpy.types.Collection:
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if collection.name not in {child.name for child in parent.children}:
        parent.children.link(collection)
    return collection


def unlink_tree(collection: bpy.types.Collection) -> None:
    for child in list(collection.children):
        unlink_tree(child)
        collection.children.unlink(child)
        bpy.data.collections.remove(child)
    for obj in list(collection.objects):
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, "users", 1) == 0:
            bpy.data.meshes.remove(mesh)


def reset_root(name: str) -> bpy.types.Collection:
    old = bpy.data.collections.get(name)
    if old is not None:
        unlink_tree(old)
        bpy.data.collections.remove(old)
    root = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(root)
    return root


def _shader_node(tree: bpy.types.NodeTree | None, bl_type: str):
    if tree is None:
        return None
    return next((node for node in tree.nodes if node.type == bl_type), None)


def ensure_material(category: str, color: list[float]) -> bpy.types.Material:
    name = f"MAT_{safe_name(category, 'fill')}"
    material = bpy.data.materials.get(name)
    rgba = (float(color[0]), float(color[1]), float(color[2]), 1.0)
    if material is None:
        material = bpy.data.materials.new(name)
    material.diffuse_color = rgba
    principled = _shader_node(material.node_tree, "BSDF_PRINCIPLED")
    if principled is not None:
        principled.inputs["Base Color"].default_value = rgba
        principled.inputs["Roughness"].default_value = 1.0
        if "Specular IOR Level" in principled.inputs:
            principled.inputs["Specular IOR Level"].default_value = 0.0
        if "Emission Color" in principled.inputs:
            principled.inputs["Emission Color"].default_value = rgba
        if "Emission Strength" in principled.inputs:
            principled.inputs["Emission Strength"].default_value = 0.25
    return material


def apply_plan_viewport() -> None:
    """Solid + плоский свет + цвет материала. Сетка и фон темы — как в Blender."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.shading.type = "SOLID"
                space.shading.light = "FLAT"
                space.shading.color_type = "MATERIAL"
                space.shading.show_object_outline = False
                space.shading.background_type = "THEME"
                space.region_3d.view_perspective = "ORTHO"


def build(json_path: Path, blend_path: Path | None) -> None:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    root_name = str(payload.get("root_collection") or ROOT_NAME)
    origin = payload.get("origin") or [0.0, 0.0]
    root = reset_root(root_name)
    root["dxf_source"] = str(payload.get("source") or "")
    root["origin_x"] = float(origin[0])
    root["origin_y"] = float(origin[1])

    cube = bpy.data.objects.get("Cube")
    if cube is not None:
        bpy.data.objects.remove(cube, do_unlink=True)

    used_names: set[str] = set()
    for index, layer in enumerate(payload.get("layers") or []):
        dxf_layer = str(layer.get("dxf_layer") or f"layer_{index}")
        category = str(layer.get("category") or "pavement")
        color = layer.get("color") or [0.55, 0.55, 0.55]
        category_root = ensure_collection(safe_name(category, "type"), root)

        collection_name = safe_name(dxf_layer)
        if collection_name in used_names:
            collection_name = safe_name(f"{dxf_layer}_{index}")
        used_names.add(collection_name)
        collection = ensure_collection(collection_name, category_root)

        vertices = [tuple(point) for point in layer.get("vertices") or []]
        faces = [tuple(face) for face in layer.get("faces") or []]
        if len(vertices) < 3 or not faces:
            continue

        mesh = bpy.data.meshes.new(f"HATCH_{collection_name}")
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        obj = bpy.data.objects.new(collection_name, mesh)
        collection.objects.link(obj)

        material = ensure_material(category, color)
        obj.data.materials.append(material)
        obj.color = (float(color[0]), float(color[1]), float(color[2]), 1.0)
        obj["dxf_layer"] = dxf_layer
        obj["category"] = category
        obj.lock_location = (True, True, True)
        obj.lock_rotation = (True, True, True)
        obj.lock_scale = (True, True, True)

    apply_plan_viewport()

    if blend_path is not None:
        blend_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        print("Сохранено:", blend_path)


if __name__ == "__main__":
    json_file, blend_file = parse_paths()
    build(json_file, blend_file)
