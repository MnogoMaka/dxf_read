"""Сборка подложки в текущей сцене Blender. Файл .blend не сохраняет."""

from __future__ import annotations

import json
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

from .placement_math import cluster_boxes, split_far_parts, split_polygons


ROOT_NAME = "SITE_UNDERLAY"
# Пятна ближе этого зазора остаются одним объектом. Рамка и соседний чертёж уходят в свой.
LAYER_PART_GAP_M = 10.0
# Имя файла в комплекте и размер одной клетки текстуры в метрах плана.
SURFACE_TEXTURES = {
    "grass": ("grass.jpg", 16.0),
    "tile": ("pavers.jpg", 8.0),
    "sidewalk": ("sidewalk.jpg", 8.0),
    "asphalt": ("asphalt.png", 8.0),
    "rubber": ("rubber.jpg", 8.0),
}


def surface_texture_key(assigned: str | None) -> str | None:
    """Ключ, который уже выбрала модель. Имя слоя здесь не разбирается."""
    key = str(assigned or "").strip().lower()
    if key in SURFACE_TEXTURES:
        return key
    return None


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
    for child in parent.children:
        if child.name == name:
            return child
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def _unlink_from_parents(collection: bpy.types.Collection) -> None:
    for scene in bpy.data.scenes:
        if collection.name in scene.collection.children:
            scene.collection.children.unlink(collection)
    for parent in list(bpy.data.collections):
        if parent == collection:
            continue
        if collection.name in parent.children:
            parent.children.unlink(collection)


def _remove_collection(collection: bpy.types.Collection) -> None:
    _unlink_from_parents(collection)
    try:
        bpy.data.collections.remove(collection, do_unlink=True)
    except TypeError:
        bpy.data.collections.remove(collection)


def unlink_tree(collection: bpy.types.Collection) -> None:
    for child in list(collection.children):
        unlink_tree(child)
        if child.name in collection.children:
            collection.children.unlink(child)
        _remove_collection(child)
    for obj in list(collection.objects):
        _delete_object(obj)


def _delete_object(obj: bpy.types.Object) -> None:
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and getattr(mesh, "users", 1) == 0 and isinstance(mesh, bpy.types.Mesh):
        bpy.data.meshes.remove(mesh)


def _is_underlay_collection(collection: bpy.types.Collection, name: str) -> bool:
    return (
        collection.name == name
        or collection.name.startswith(f"{name}.")
        or collection.get("dxf_source") is not None
    )


def remove_root(name: str) -> None:
    """Удаляет прежнюю подложку, в том числе копии SITE_UNDERLAY.001 и объекты вне коллекции."""
    clear_sheet_cache()
    for obj in list(bpy.data.objects):
        if "dxf_layer" in obj.keys():
            _delete_object(obj)
    for collection in list(bpy.data.collections):
        try:
            matched = _is_underlay_collection(collection, name)
        except ReferenceError:
            continue
        if not matched:
            continue
        try:
            unlink_tree(collection)
            _remove_collection(collection)
        except ReferenceError:
            continue
    layer = getattr(bpy.context, "view_layer", None)
    if layer is not None:
        layer.update()


def reset_root(name: str, scene: bpy.types.Scene | None = None) -> bpy.types.Collection:
    remove_root(name)
    root = bpy.data.collections.new(name)
    target = scene or bpy.context.scene
    target.collection.children.link(root)
    return root


def _shader_node(tree: bpy.types.NodeTree | None, bl_type: str):
    if tree is None:
        return None
    return next((node for node in tree.nodes if node.type == bl_type), None)


def _rgba(color: list[float]) -> tuple[float, float, float, float]:
    rgb = [float(channel) for channel in (list(color) + [0.55, 0.55, 0.55])[:3]]
    return rgb[0], rgb[1], rgb[2], 1.0


def _surface_image(filename: str):
    path = Path(__file__).resolve().parent / "textures" / filename
    if not path.is_file():
        return None
    try:
        image = bpy.data.images.load(str(path), check_existing=True)
    except Exception:
        return None
    try:
        image.colorspace_settings.name = "sRGB"
    except Exception:
        pass
    try:
        if image.packed_file is None:
            image.pack()
    except Exception:
        pass
    return image


def _flat_material(name: str, rgba: tuple[float, float, float, float]) -> bpy.types.Material:
    material = bpy.data.materials.get(name)
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


def _textured_material(name: str, rgba: tuple[float, float, float, float], image) -> bpy.types.Material:
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.diffuse_color = rgba
    tree = material.node_tree
    principled = _shader_node(tree, "BSDF_PRINCIPLED")
    image_node = _shader_node(tree, "TEX_IMAGE")
    if image_node is None and tree is not None:
        image_node = tree.nodes.new("ShaderNodeTexImage")
    if image_node is not None:
        image_node.image = image
        image_node.interpolation = "Linear"
        image_node.extension = "REPEAT"
    if principled is not None and image_node is not None and tree is not None:
        socket = principled.inputs["Base Color"]
        for link in list(socket.links):
            tree.links.remove(link)
        tree.links.new(image_node.outputs["Color"], socket)
        principled.inputs["Roughness"].default_value = 1.0
        if "Specular IOR Level" in principled.inputs:
            principled.inputs["Specular IOR Level"].default_value = 0.0
        if "Emission Strength" in principled.inputs:
            principled.inputs["Emission Strength"].default_value = 0.0
    return material


def ensure_material(category: str, color: list[float], texture_key: str | None = None) -> bpy.types.Material:
    rgba = _rgba(color)
    spec = SURFACE_TEXTURES.get(texture_key or "")
    image = _surface_image(spec[0]) if spec else None
    if image is None:
        return _flat_material(f"MAT_{safe_name(category, 'fill')}", rgba)
    return _textured_material(f"MAT_{safe_name(texture_key, 'fill')}", rgba, image)


def assign_plan_uv(mesh, tile_m: float) -> None:
    """Клетка текстуры ложится на tile_m метров плана и повторяется."""
    layer = mesh.uv_layers.new(name="Plan") if not mesh.uv_layers else mesh.uv_layers[0]
    mesh.uv_layers.active = layer
    for polygon in mesh.polygons:
        for loop_index in polygon.loop_indices:
            vertex = mesh.vertices[mesh.loops[loop_index].vertex_index]
            layer.data[loop_index].uv = (vertex.co.x / tile_m, vertex.co.y / tile_m)


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
    """Solid + плоский свет. Текстура видна на газоне и покрытиях, остальные группы остаются цветом материала."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.shading.type = "SOLID"
                space.shading.light = "FLAT"
                space.shading.color_type = "TEXTURE"
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


SHEET_GAP_M = 200.0
_SHEET_CACHE: dict[str, object] = {"key": None, "groups": []}
_PARK_KEY = "dwgreader_parked_name"
_BUILD_ROOT_KEY = "dwgreader_build_root"
_MATERIAL_INPUTS = ("Base Color", "Roughness", "Specular IOR Level", "Emission Color", "Emission Strength")


def clear_sheet_cache() -> None:
    _SHEET_CACHE["key"] = None
    _SHEET_CACHE["groups"] = []


def world_box(obj: bpy.types.Object) -> tuple[float, float, float, float]:
    corners = []
    if obj.bound_box is not None:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if corners:
        xs = [corner.x for corner in corners]
        ys = [corner.y for corner in corners]
        if max(xs) - min(xs) > 1e-6 or max(ys) - min(ys) > 1e-6:
            return (min(xs), min(ys), max(xs), max(ys))
    point = obj.matrix_world.translation
    return (point.x, point.y, point.x, point.y)


def _sheet_identity(objects: list[bpy.types.Object]) -> tuple[tuple[str, int], ...] | None:
    try:
        return tuple(sorted((obj.name, int(obj.as_pointer())) for obj in objects))
    except ReferenceError:
        return None


def _same_sheet_objects(cached: object, objects: list[bpy.types.Object]) -> bool:
    if not isinstance(cached, list):
        return False
    try:
        cached_ids = sorted(int(obj.as_pointer()) for group in cached for obj in group)
        live_ids = sorted(int(obj.as_pointer()) for obj in objects)
    except ReferenceError:
        return False
    return cached_ids == live_ids


def sheet_groups(root: bpy.types.Collection, gap: float) -> list[list[bpy.types.Object]]:
    """Отдельные чертежи на одном листе: объекты дальше gap друг от друга."""
    objects = [obj for obj in root.all_objects if obj.type != "EMPTY"]
    key = (_sheet_identity(objects), round(float(gap), 3))
    cached = _SHEET_CACHE["groups"]
    if key[0] is not None and _SHEET_CACHE["key"] == key and _same_sheet_objects(cached, objects):
        return cached
    boxes = [world_box(obj) for obj in objects]
    groups = [[objects[index] for index in members] for members in cluster_boxes(boxes, gap)]
    _SHEET_CACHE["key"] = key
    _SHEET_CACHE["groups"] = groups
    return groups


def sheet_size(objects: list[bpy.types.Object]) -> tuple[float, float]:
    boxes = [world_box(obj) for obj in objects]
    if not boxes:
        return (0.0, 0.0)
    return (
        max(box[2] for box in boxes) - min(box[0] for box in boxes),
        max(box[3] for box in boxes) - min(box[1] for box in boxes),
    )


def frame_objects(
    context: bpy.types.Context,
    objects: list[bpy.types.Object],
    others: list[bpy.types.Object] | None = None,
) -> None:
    """Кадр сверху на выбранный чертёж. Остальные чертежи листа скрываются в виде, не удаляются."""
    layer = context.view_layer.objects
    for obj in list(layer):
        obj.select_set(False)
    for obj in others or []:
        if obj.name in layer:
            obj.hide_set(True)
            obj.select_set(False)
    mins: Vector | None = None
    maxs: Vector | None = None
    for obj in objects:
        if obj.name in layer:
            obj.hide_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
        if obj.bound_box is None:
            point = obj.matrix_world.translation
            corners = [point]
        else:
            corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        for world in corners:
            if mins is None or maxs is None:
                mins = Vector(world)
                maxs = Vector(world)
            else:
                mins.x, mins.y, mins.z = min(mins.x, world.x), min(mins.y, world.y), min(mins.z, world.z)
                maxs.x, maxs.y, maxs.z = max(maxs.x, world.x), max(maxs.y, world.y), max(maxs.z, world.z)
    if mins is None or maxs is None:
        return
    size = maxs - mins
    span = max(size.x, size.y, size.z, 1.0)
    reach = max(span, abs(mins.x), abs(mins.y), abs(maxs.x), abs(maxs.y))
    relax_view_clip(context, reach)
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
            region_3d.view_perspective = "ORTHO"
            region_3d.view_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
            region_3d.view_location = center
            region_3d.view_distance = half_height * 1.35
            region_3d.update()
            return


def drop_objects(objects: list[bpy.types.Object], root: bpy.types.Collection | None = None) -> int:
    removed = 0
    for obj in objects:
        try:
            _delete_object(obj)
            removed += 1
        except ReferenceError:
            continue
    if root is not None:
        _prune_empty(root)
    clear_sheet_cache()
    return removed


def _prune_empty(collection: bpy.types.Collection) -> None:
    for child in list(collection.children):
        _prune_empty(child)
        try:
            if not child.objects and not child.children:
                bpy.data.collections.remove(child, do_unlink=True)
        except ReferenceError:
            continue


SPLIT_MODES = ("layer", "polygon", "gap")


def layer_parts(layer: dict, vertices, faces, split_mode: str, part_gap_m: float):
    if split_mode == "layer":
        return [(vertices, faces)]
    if split_mode == "polygon":
        return split_polygons(vertices, faces, layer.get("polygon_faces"))
    return split_far_parts(vertices, faces, float(part_gap_m))


def build(
    json_path: Path,
    part_gap_m: float = LAYER_PART_GAP_M,
    split_mode: str = "gap",
    by_type: bool = True,
    scene: bpy.types.Scene | None = None,
    root_name: str | None = None,
    clear_existing: bool = True,
) -> int:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    root_name = str(root_name or payload.get("root_collection") or ROOT_NAME)
    origin = payload.get("origin") or [0.0, 0.0]
    root = reset_root(root_name, scene) if clear_existing else _link_new_root(root_name, scene)
    root["dxf_source"] = str(payload.get("source") or "")
    root["origin_x"] = float(origin[0])
    root["origin_y"] = float(origin[1])
    root["scale"] = float(payload.get("scale") or 1.0)
    root["split_mode"] = split_mode
    if split_mode not in SPLIT_MODES:
        split_mode = "gap"

    used_names: set[str] = set()
    built = 0
    for index, layer in enumerate(payload.get("layers") or []):
        dxf_layer = str(layer.get("dxf_layer") or f"layer_{index}")
        category = str(layer.get("category") or "pavement")
        color = layer.get("color") or [0.55, 0.55, 0.55]
        parent = ensure_collection(safe_name(category, "type"), root) if by_type else root

        collection_name = safe_name(dxf_layer)
        if collection_name in used_names or bpy.data.collections.get(collection_name) is not None:
            collection_name = safe_name(f"{dxf_layer}_{index}")
        used_names.add(collection_name)
        collection = ensure_collection(collection_name, parent)

        vertices = [tuple(point) for point in layer.get("vertices") or []]
        faces = [tuple(int(index) for index in face) for face in layer.get("faces") or []]
        if len(vertices) < 3 or not faces:
            continue

        parts = layer_parts(layer, vertices, faces, split_mode, part_gap_m)
        texture_key = surface_texture_key(layer.get("texture"))
        material = ensure_material(category, color, texture_key)
        textured = texture_key is not None and material.name == f"MAT_{safe_name(texture_key, 'fill')}"
        tile_m = SURFACE_TEXTURES[texture_key][1] if textured else None
        rgb = (list(color) + [0.55, 0.55, 0.55])[:3]
        color_rgba = (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
        for part_index, (part_vertices, part_faces) in enumerate(parts, start=1):
            object_name = part_object_name(collection_name, part_index, len(parts))
            mesh = bpy.data.meshes.new(f"HATCH_{object_name}")
            mesh.from_pydata(part_vertices, [], part_faces)
            mesh.update()
            if tile_m:
                assign_plan_uv(mesh, tile_m)
            obj = bpy.data.objects.new(object_name, mesh)
            collection.objects.link(obj)
            obj.data.materials.append(material)
            obj.color = color_rgba
            obj["dxf_layer"] = dxf_layer
            obj["category"] = category
            if texture_key:
                obj["texture"] = texture_key
            obj["part"] = part_index
            obj["geometry"] = str(layer.get("geometry") or "")
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


def _update_view() -> None:
    layer = getattr(bpy.context, "view_layer", None)
    if layer is None:
        return
    try:
        layer.update()
    except Exception:
        return


def _link_new_root(name: str, scene: bpy.types.Scene | None) -> bpy.types.Collection:
    root = bpy.data.collections.new(name)
    root[_BUILD_ROOT_KEY] = name
    target = scene or bpy.context.scene
    target.collection.children.link(root)
    return root


def _collection_tree(root: bpy.types.Collection):
    stack = [root]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        try:
            pointer = int(current.as_pointer())
        except ReferenceError:
            continue
        if pointer in seen:
            continue
        seen.add(pointer)
        yield current
        try:
            stack.extend(current.children)
        except ReferenceError:
            continue


def _underlay_roots(name: str) -> list[bpy.types.Collection]:
    roots = []
    for collection in list(bpy.data.collections):
        try:
            matched = _is_underlay_collection(collection, name)
        except ReferenceError:
            continue
        if matched:
            roots.append(collection)
    return roots


def _rename_parked(block, library, taken: set[str]) -> str:
    original = block.name
    block[_PARK_KEY] = original
    for index in range(10000):
        suffix = "" if index == 0 else f"~{index}"
        requested = f"~p~{original}"
        requested = f"{requested[: 63 - len(suffix)]}{suffix}"
        if requested in taken:
            continue
        try:
            owner = library.get(requested)
        except ReferenceError:
            owner = None
        if owner not in (None, block):
            taken.add(requested)
            continue
        block.name = requested
        taken.add(block.name)
        return block.name
    raise RuntimeError(f"Не удалось освободить имя {original}")


def _park_underlay(name: str, parked: list[tuple[str, str]]) -> None:
    """Прячет текущую подложку под другими именами, чтобы новая собралась, не стирая её."""
    taken: set[str] = set()
    seen: set[int] = set()
    objects = []
    for root in _underlay_roots(name):
        try:
            found = list(root.all_objects)
        except ReferenceError:
            continue
        for obj in found:
            try:
                pointer = int(obj.as_pointer())
            except ReferenceError:
                continue
            if pointer in seen:
                continue
            seen.add(pointer)
            objects.append(obj)
    for obj in list(bpy.data.objects):
        try:
            marked = "dxf_layer" in obj.keys()
            pointer = int(obj.as_pointer())
        except ReferenceError:
            continue
        if not marked or pointer in seen:
            continue
        seen.add(pointer)
        objects.append(obj)
    for obj in objects:
        parked.append(("object", _rename_parked(obj, bpy.data.objects, taken)))
        mesh = obj.data if isinstance(getattr(obj, "data", None), bpy.types.Mesh) else None
        if mesh is not None and _PARK_KEY not in mesh.keys():
            parked.append(("mesh", _rename_parked(mesh, bpy.data.meshes, taken)))
    seen_collections: set[int] = set()
    for root in _underlay_roots(name):
        for collection in _collection_tree(root):
            try:
                pointer = int(collection.as_pointer())
            except ReferenceError:
                continue
            if pointer in seen_collections:
                continue
            seen_collections.add(pointer)
            parked.append(("collection", _rename_parked(collection, bpy.data.collections, taken)))


def _library(kind: str):
    if kind == "object":
        return bpy.data.objects
    if kind == "mesh":
        return bpy.data.meshes
    return bpy.data.collections


def _unpark(parked: list[tuple[str, str]]) -> None:
    for kind, name in reversed(parked):
        try:
            block = _library(kind).get(name)
        except ReferenceError:
            continue
        if block is None:
            continue
        original = block.get(_PARK_KEY)
        if not original:
            continue
        block.name = str(original)
        try:
            del block[_PARK_KEY]
        except KeyError:
            pass


def _discard_parked(parked: list[tuple[str, str]]) -> None:
    for kind, name in parked:
        if kind != "object":
            continue
        try:
            obj = bpy.data.objects.get(name)
        except ReferenceError:
            continue
        if obj is None:
            continue
        try:
            _delete_object(obj)
        except (ReferenceError, RuntimeError):
            continue
    for kind, name in parked:
        if kind != "mesh":
            continue
        try:
            mesh = bpy.data.meshes.get(name)
            if mesh is not None and getattr(mesh, "users", 1) == 0:
                bpy.data.meshes.remove(mesh)
        except (ReferenceError, RuntimeError):
            continue
    for kind, name in parked:
        if kind != "collection":
            continue
        try:
            collection = bpy.data.collections.get(name)
            if collection is None:
                continue
            unlink_tree(collection)
            _remove_collection(collection)
        except (ReferenceError, RuntimeError):
            continue


def _discard_fresh_underlay(root_name: str) -> None:
    for collection in list(bpy.data.collections):
        try:
            fresh = collection.get(_BUILD_ROOT_KEY) == root_name and _PARK_KEY not in collection.keys()
        except ReferenceError:
            continue
        if not fresh:
            continue
        try:
            unlink_tree(collection)
            _remove_collection(collection)
        except ReferenceError:
            continue


def _socket_value(value):
    if isinstance(value, (int, float, bool)):
        return value
    try:
        return tuple(value)
    except TypeError:
        return value


def _snapshot_materials() -> dict[str, tuple]:
    saved: dict[str, tuple] = {}
    for material in list(bpy.data.materials):
        try:
            principled = _shader_node(material.node_tree, "BSDF_PRINCIPLED")
            inputs = {}
            if principled is not None:
                for key in _MATERIAL_INPUTS:
                    if key not in principled.inputs:
                        continue
                    inputs[key] = _socket_value(principled.inputs[key].default_value)
            saved[material.name] = (_socket_value(material.diffuse_color), inputs)
        except ReferenceError:
            continue
    return saved


def _restore_materials(saved: dict[str, tuple]) -> None:
    for name, (diffuse, inputs) in saved.items():
        material = bpy.data.materials.get(name)
        if material is None:
            continue
        material.diffuse_color = diffuse
        principled = _shader_node(getattr(material, "node_tree", None), "BSDF_PRINCIPLED")
        if principled is None:
            continue
        for key, value in inputs.items():
            if key in principled.inputs:
                principled.inputs[key].default_value = value


def _remove_unused_materials_except(kept: set[str]) -> None:
    for material in list(bpy.data.materials):
        if material.name in kept or getattr(material, "users", 1) != 0:
            continue
        bpy.data.materials.remove(material)


def _claim_root_name(root_name: str) -> None:
    for collection in list(bpy.data.collections):
        try:
            if collection.get(_BUILD_ROOT_KEY) != root_name or _PARK_KEY in collection.keys():
                continue
        except ReferenceError:
            continue
        if collection.name != root_name:
            collection.name = root_name
        try:
            del collection[_BUILD_ROOT_KEY]
        except KeyError:
            pass
        return


def _rollback_underlay(root_name: str, parked: list[tuple[str, str]], materials: dict[str, tuple]) -> None:
    _discard_fresh_underlay(root_name)
    _update_view()
    _remove_unused_materials_except(set(materials))
    _restore_materials(materials)
    _unpark(parked)
    clear_sheet_cache()
    _update_view()


def commit_underlay(
    json_path: Path,
    part_gap_m: float = LAYER_PART_GAP_M,
    split_mode: str = "gap",
    by_type: bool = True,
    scene: bpy.types.Scene | None = None,
) -> int:
    """Собирает новую подложку рядом со старой и подменяет её только после успеха.

    Если сборка падает, прежние объекты остаются на своих именах.
    """
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    root_name = str(payload.get("root_collection") or ROOT_NAME)
    materials = _snapshot_materials()
    parked: list[tuple[str, str]] = []
    try:
        clear_sheet_cache()
        _park_underlay(root_name, parked)
        count = build(
            Path(json_path),
            part_gap_m,
            split_mode=split_mode,
            by_type=by_type,
            scene=scene,
            root_name=root_name,
            clear_existing=False,
        )
    except Exception:
        _rollback_underlay(root_name, parked, materials)
        raise
    _discard_parked(parked)
    _claim_root_name(root_name)
    clear_sheet_cache()
    _update_view()
    return count
