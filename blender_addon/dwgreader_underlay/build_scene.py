"""Сборка подложки в текущей сцене Blender. Файл .blend не сохраняет."""

from __future__ import annotations

import json
from pathlib import Path

import bpy
from mathutils import Vector

from .placement_math import split_far_parts


ROOT_NAME = "SITE_UNDERLAY"
# Пятна ближе этого зазора остаются одним объектом. Рамка и соседний чертёж уходят в свой.
LAYER_PART_GAP_M = 10.0


def safe_name(text: str, fallback: str = "NO_LAYER") -> str:
    value = str(text or "").replace("\n", " ").replace("/", "_").replace("\\", "_").strip()
    value = value.strip(".") or fallback
    return value[:63]


def part_object_name(base: str, index: int, count: int) -> str:
    if count <= 1:
        return safe_name(base)
    suffix = f"_{index}"
    stem = safe_name(base)[: 63 - len(suffix)]
    return f"{stem}{suffix}"


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
        for scene in bpy.data.scenes:
            if old.name in scene.collection.children:
                scene.collection.children.unlink(old)
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
    rgb = [float(channel) for channel in (list(color) + [0.55, 0.55, 0.55])[:3]]
    rgba = (rgb[0], rgb[1], rgb[2], 1.0)
    if material is None:
        material = bpy.data.materials.new(name)
    material.use_nodes = True
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


def relax_view_clip(context: bpy.types.Context, span: float = 0.0) -> None:
    """Дальняя плоскость вида по умолчанию 1000 м. Рамка участка в километрах иначе пустая."""
    clip_end = max(1_000_000.0, float(span) * 8.0)
    screen = context.screen
    screens = [screen] if screen is not None else list(bpy.data.screens)
    for item in screens:
        for area in item.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.clip_start = 0.01
                space.clip_end = clip_end


def apply_plan_viewport() -> None:
    """Solid + плоский свет + цвет материала. Сетка и фон темы остаются как в Blender."""
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
                if space.region_3d is not None:
                    space.region_3d.view_perspective = "ORTHO"


def frame_collection(context: bpy.types.Context, root: bpy.types.Collection) -> None:
    """Кадр вида без bpy.ops: вызов view_selected из модальной генерации оставлял скролл панели мёртвым."""
    layer_objects = set(context.view_layer.objects)
    for obj in layer_objects:
        obj.select_set(False)

    mins: Vector | None = None
    maxs: Vector | None = None

    def walk(collection: bpy.types.Collection) -> None:
        nonlocal mins, maxs
        for obj in collection.objects:
            if obj.name in layer_objects:
                obj.select_set(True)
                context.view_layer.objects.active = obj
            if obj.bound_box is None:
                continue
            for corner in obj.bound_box:
                world = obj.matrix_world @ Vector(corner)
                if mins is None or maxs is None:
                    mins = Vector(world)
                    maxs = Vector(world)
                else:
                    mins.x, mins.y, mins.z = min(mins.x, world.x), min(mins.y, world.y), min(mins.z, world.z)
                    maxs.x, maxs.y, maxs.z = max(maxs.x, world.x), max(maxs.y, world.y), max(maxs.z, world.z)
        for child in collection.children:
            walk(child)

    walk(root)
    if mins is None or maxs is None or context.view_layer.objects.active is None:
        return
    size = maxs - mins
    span = max(size.x, size.y, size.z)
    relax_view_clip(context, span)
    screen = context.screen
    screens = [screen] if screen is not None else list(bpy.data.screens)
    center = (mins + maxs) * 0.5
    for item in screens:
        for area in item.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((region for region in area.regions if region.type == "WINDOW"), None)
            space = area.spaces.active
            region_3d = space.region_3d if space.type == "VIEW_3D" else None
            if region is None or region_3d is None or region.height <= 0:
                continue
            aspect = region.width / region.height
            half_height = max(size.y * 0.5, (size.x * 0.5) / aspect, 1.0)
            region_3d.view_location = center
            region_3d.view_distance = half_height * (2.4 if region_3d.view_perspective == "PERSP" else 1.15)
            region_3d.update()
            return


def build(json_path: Path, part_gap_m: float = LAYER_PART_GAP_M) -> int:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    root_name = str(payload.get("root_collection") or ROOT_NAME)
    origin = payload.get("origin") or [0.0, 0.0]
    root = reset_root(root_name)
    root["dxf_source"] = str(payload.get("source") or "")
    root["origin_x"] = float(origin[0])
    root["origin_y"] = float(origin[1])

    used_names: set[str] = set()
    built = 0
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
        faces = [tuple(int(index) for index in face) for face in layer.get("faces") or []]
        if len(vertices) < 3 or not faces:
            continue

        parts = split_far_parts(vertices, faces, float(part_gap_m))
        material = ensure_material(category, color)
        rgb = (list(color) + [0.55, 0.55, 0.55])[:3]
        color_rgba = (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
        for part_index, (part_vertices, part_faces) in enumerate(parts, start=1):
            object_name = part_object_name(collection_name, part_index, len(parts))
            mesh = bpy.data.meshes.new(f"HATCH_{object_name}")
            mesh.from_pydata(part_vertices, [], part_faces)
            mesh.update()
            obj = bpy.data.objects.new(object_name, mesh)
            collection.objects.link(obj)
            obj.data.materials.append(material)
            obj.color = color_rgba
            obj["dxf_layer"] = dxf_layer
            obj["category"] = category
            obj["part"] = part_index
            obj.lock_location = (True, True, True)
            obj.lock_rotation = (True, True, True)
            obj.lock_scale = (True, True, True)
            built += 1

    apply_plan_viewport()
    relax_view_clip(bpy.context)
    try:
        frame_collection(bpy.context, root)
    except Exception:
        pass
    return built
