"""Расстановка деревьев, лавок и площадок по подложке.

Модели лежат в папке library рядом с аддоном: catalog.json и отдельный GLB на объект.
"""

from __future__ import annotations

import json
import logging
import os
import random
from math import hypot, pi, tau
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from bpy.types import Operator
from bpy.utils import previews
from bpy_extras import view3d_utils
from mathutils import Vector

from .placement_math import (
    bench_path,
    DEFAULT_PLANT_DENSITY,
    DEFAULT_TREE_SHARE,
    face_groups,
    bench_island_keys,
    blocks_playground,
    cluster_nearby_islands,
    compose_playground,
    is_playground_surface,
    minimum_rectangle,
    playground_clearance,
    auto_playground_kinds,
    play_group,
    mirrored_keep_from,
    orientation_angle,
    mesh_plan_side,
    OccupiedFootprints,
    host_rings,
    plant_footprint,
    plant_slot_count,
    PlantObstacles,
    slot_attempt_families,
    polygon_area_xy,
    polygon_centroid_xy,
    safety_inside,
    scatter_points,
    choose_plant_names,
)

ROOT_NAME = "SITE_PLACEMENT"
UNDERLAY_NAME = "SITE_UNDERLAY"
LIBRARY_NAME = "SITE_LIBRARY"
PLANT_GROUP = "DWGREADER_Plantings"
GROUP_VERSION = 3
MIN_ZONE_AREA_M2 = 1.0
MIN_BENCH_STEP_M = 0.5
MAX_PLANTS = 2500
PLANT_ATTEMPTS = 24

_CATALOG: list[dict] | None = None
_CATALOG_STAMP: tuple[str, int] | None = None
_DRAW = None


class PlacementError(RuntimeError):
    pass


def _prefs(context):
    package = (__package__ or "dwgreader_underlay").split(".")[0]
    addon = context.preferences.addons.get(package)
    return addon.preferences if addon is not None else None


def bundled_library() -> Path:
    return Path(__file__).resolve().parent / "library"


def library_path(context) -> Path | None:
    folder = bundled_library()
    if (folder / "catalog.json").is_file():
        return folder
    return None


def load_catalog(context) -> list[dict]:
    global _CATALOG, _CATALOG_STAMP
    folder = library_path(context)
    if folder is None:
        raise PlacementError("В аддоне нет библиотеки моделей")
    catalog_path = folder / "catalog.json"
    if not catalog_path.is_file():
        raise PlacementError("В папке библиотеки нет catalog.json")
    stamp = (str(catalog_path), catalog_path.stat().st_mtime_ns)
    if _CATALOG is not None and _CATALOG_STAMP == stamp:
        return _CATALOG
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlacementError(f"catalog.json не читается: {exc}") from exc
    if not isinstance(payload, list):
        raise PlacementError("catalog.json должен быть списком моделей")
    assets = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id") or not item.get("file"):
            continue
        footprint = item.get("footprint") or [1.0, 1.0, 1.0]
        asset = {
            "id": str(item["id"]),
            "family": str(item.get("family") or ""),
            "placement": str(item.get("placement") or ""),
            "playground_type": str(item.get("playground_type") or "children"),
            "file": str(item["file"]),
            "name": str(item.get("name") or item["id"]),
            "footprint": [float(footprint[0]), float(footprint[1]), float(footprint[2])],
        }
        clearance = item.get("clearance")
        if isinstance(clearance, list) and len(clearance) >= 4:
            asset["clearance"] = [float(clearance[0]), float(clearance[1]), float(clearance[2]), float(clearance[3])]
        motion = str(item.get("motion") or "")
        if motion:
            asset["motion"] = motion
        if item.get("fall_height") is not None:
            asset["fall_height"] = float(item["fall_height"])
        if item.get("role"):
            asset["role"] = str(item["role"])
        if item.get("runout") is not None:
            asset["runout"] = float(item["runout"])
        if item.get("runout_side"):
            asset["runout_side"] = str(item["runout_side"])
        assets.append(asset)
    _CATALOG = assets
    _CATALOG_STAMP = stamp
    return assets


def _assets(context, family: str) -> list[dict]:
    return [asset for asset in load_catalog(context) if asset["family"] == family]


def _play_assets(context, kind: str | None = None) -> list[dict]:
    playground_type = kind or str(getattr(context.scene, "dwgreader_playground_type", "children") or "children")
    return [asset for asset in _assets(context, "playground") if asset.get("playground_type") == playground_type]


def _on_playground_type(_scene, context) -> None:
    if context is None:
        return
    assets = _play_assets(context)
    selected = str(getattr(context.scene, "dwgreader_play_asset", "") or "")
    if assets and selected not in {asset["id"] for asset in assets}:
        context.scene.dwgreader_play_asset = assets[0]["id"]


def _asset(context, asset_id: str) -> dict:
    for asset in load_catalog(context):
        if asset["id"] == asset_id:
            return asset
    raise PlacementError(f"В каталоге нет модели {asset_id}")


def _detach_library(collection) -> None:
    """Шаблоны остаются в файле для ноды и не входят в сцену, поэтому не попадают в GLB."""
    if collection is None:
        return
    scene = bpy.context.scene
    master = scene.collection if scene is not None else None
    if master is not None and collection.name in master.children:
        master.children.unlink(collection)
    for parent in list(bpy.data.collections):
        if parent == collection:
            continue
        name = str(getattr(parent, "name", "") or "")
        if name == LIBRARY_NAME or name.startswith(f"{LIBRARY_NAME}."):
            continue
        try:
            linked = collection.name in parent.children
        except ReferenceError:
            continue
        if linked:
            parent.children.unlink(collection)
    try:
        collection.use_fake_user = True
    except AttributeError:
        pass


def _collection(name: str, parent: bpy.types.Collection | None) -> bpy.types.Collection:
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if name == LIBRARY_NAME or str(name).startswith(f"{LIBRARY_NAME}."):
        _detach_library(collection)
        if parent is not None and collection.name not in {child.name for child in parent.children}:
            parent.children.link(collection)
        return collection
    if parent is None:
        if collection.name not in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.link(collection)
    elif collection.name not in {child.name for child in parent.children}:
        parent.children.link(collection)
    return collection


def _placement_collection(kind: str) -> bpy.types.Collection:
    root = _collection(ROOT_NAME, None)
    return _collection({"plantings": "Plantings", "benches": "Benches", "playgrounds": "Playgrounds"}[kind], root)


def clear_placement() -> None:
    """Удаляет деревья, лавки и модели площадок прошлой расстановки. Библиотеку не трогает."""
    from .build_scene import _remove_collection, unlink_tree

    for collection in list(bpy.data.collections):
        try:
            name = collection.name
        except ReferenceError:
            continue
        if name != ROOT_NAME and not name.startswith(f"{ROOT_NAME}."):
            continue
        try:
            unlink_tree(collection)
            _remove_collection(collection)
        except ReferenceError:
            continue


def _under_collection(obj: bpy.types.Object, root_name: str) -> bool:
    root = bpy.data.collections.get(root_name)
    if root is None or obj is None:
        return False

    def contains(collection: bpy.types.Collection) -> bool:
        if obj.name in collection.objects:
            return True
        return any(contains(child) for child in collection.children)

    return contains(root)


def _busy(context) -> bool:
    window = context.window_manager
    return bool(getattr(window, "dwgreader_busy", False))


def _status(context, text: str) -> None:
    if hasattr(context.scene, "dwgreader_status"):
        context.scene.dwgreader_status = text


def _ground_center(obj: bpy.types.Object) -> None:
    vertices = obj.data.vertices
    if not vertices:
        return
    xs = [vertex.co.x for vertex in vertices]
    ys = [vertex.co.y for vertex in vertices]
    zs = [vertex.co.z for vertex in vertices]
    shift = Vector(((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5, min(zs)))
    for vertex in vertices:
        vertex.co -= shift
    obj.data.update()


def _drop_mirrored_half(obj: bpy.types.Object) -> None:
    """Лежак в каталоге — две копии по разные стороны оси X. Оставляем одну и ставим её на линию."""
    mesh = obj.data
    if mesh is None or obj.type != "MESH":
        return
    cut = mirrored_keep_from([vertex.co.x for vertex in mesh.vertices])
    if cut is None:
        return
    import bmesh

    edited = bmesh.new()
    edited.from_mesh(mesh)
    doomed = [vertex for vertex in edited.verts if vertex.co.x < cut]
    bmesh.ops.delete(edited, geom=doomed, context="VERTS")
    edited.to_mesh(mesh)
    edited.free()
    mesh.update()
    _ground_center(obj)


def _import_mesh(context, asset: dict) -> bpy.types.Object:
    name = f"LIB_{asset['id']}"
    existing = bpy.data.objects.get(name)
    if existing is not None and existing.type == "MESH":
        existing.rotation_mode = "XYZ"
        if asset["id"] == "lounger_01":
            _drop_mirrored_half(existing)
        _park_library_object(existing)
        return existing
    folder = library_path(context)
    if folder is None:
        raise PlacementError("В аддоне нет библиотеки моделей")
    filepath = folder / asset["file"]
    if not filepath.is_file():
        raise PlacementError(f"Нет файла модели: {asset['file']}")
    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(filepath))
    imported = [obj for obj in bpy.data.objects if obj not in before]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if not meshes:
        for obj in imported:
            bpy.data.objects.remove(obj, do_unlink=True)
        raise PlacementError(f"В {asset['file']} нет сетки")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
        obj.hide_set(False)
        obj.hide_viewport = False
        obj.select_set(True)
    context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    if len(meshes) > 1:
        bpy.ops.object.join()
    mesh_object = context.view_layer.objects.active
    _ground_center(mesh_object)
    if asset["id"] == "lounger_01":
        _drop_mirrored_half(mesh_object)
    mesh_object.name = name
    mesh_object.data.name = name
    mesh_object.rotation_mode = "XYZ"
    mesh_object["asset_id"] = asset["id"]
    library = _collection(LIBRARY_NAME, None)
    for collection in list(mesh_object.users_collection):
        collection.objects.unlink(mesh_object)
    library.objects.link(mesh_object)
    _park_library_object(mesh_object)
    for obj in imported:
        if obj != mesh_object and obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
    return mesh_object


def _park_library_object(obj: bpy.types.Object) -> None:
    """Прячем исходник глазом, не монитором: монитор выкидывает его из геонод, и на слое пусто."""
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_select = True
    obj.hide_set(True)


def _family_collection(context, family: str) -> bpy.types.Collection:
    library = _collection(LIBRARY_NAME, None)
    collection = _collection(f"DWG_LIB_{family}", library)
    for asset in _assets(context, family):
        mesh_object = _import_mesh(context, asset)
        if mesh_object.name not in collection.objects:
            collection.objects.link(mesh_object)
    return collection


def _socket(node_group: bpy.types.NodeTree, name: str, in_out: str, socket_type: str):
    socket = node_group.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    return socket


def _plant_group() -> bpy.types.NodeTree:
    current = bpy.data.node_groups.get(PLANT_GROUP)
    if current is not None and current.get("dwg_version") == GROUP_VERSION:
        return current
    if current is not None:
        bpy.data.node_groups.remove(current)
    group = bpy.data.node_groups.new(PLANT_GROUP, "GeometryNodeTree")
    group["dwg_version"] = GROUP_VERSION
    _socket(group, "Geometry", "INPUT", "NodeSocketGeometry")
    _socket(group, "Geometry", "OUTPUT", "NodeSocketGeometry")

    nodes = group.nodes
    links = group.links
    group_input = nodes.new("NodeGroupInput")
    group_output = nodes.new("NodeGroupOutput")
    distribute = nodes.new("GeometryNodeDistributePointsOnFaces")
    distribute.distribute_method = "RANDOM"
    distribute.label = "distribute"
    index = nodes.new("GeometryNodeInputIndex")
    pick = nodes.new("FunctionNodeRandomValue")
    pick.label = "pick"
    choice = nodes.new("FunctionNodeRandomValue")
    choice.data_type = "INT"
    choice.label = "choice"
    rotate_value = nodes.new("FunctionNodeRandomValue")
    rotate_value.label = "rotate"
    compare = nodes.new("FunctionNodeCompare")
    compare.data_type = "FLOAT"
    compare.operation = "LESS_THAN"
    compare.label = "share"
    invert = nodes.new("FunctionNodeBooleanMath")
    invert.operation = "NOT"
    trees = nodes.new("GeometryNodeCollectionInfo")
    bushes = nodes.new("GeometryNodeCollectionInfo")
    trees.label = "trees"
    bushes.label = "bushes"
    for info in (trees, bushes):
        info.inputs["Separate Children"].default_value = True
        info.inputs["Reset Children"].default_value = True
    tree_instances = nodes.new("GeometryNodeInstanceOnPoints")
    bush_instances = nodes.new("GeometryNodeInstanceOnPoints")
    for instance in (tree_instances, bush_instances):
        instance.inputs["Pick Instance"].default_value = True
    combine = nodes.new("ShaderNodeCombineXYZ")
    combine.inputs["X"].default_value = 0.0
    combine.inputs["Y"].default_value = 0.0
    euler = nodes.new("FunctionNodeEulerToRotation")
    join = nodes.new("GeometryNodeJoinGeometry")
    realize = nodes.new("GeometryNodeRealizeInstances")

    links.new(group_input.outputs["Geometry"], distribute.inputs["Mesh"])
    distribute.inputs["Density"].default_value = 0.06
    distribute.inputs["Seed"].default_value = 1
    links.new(index.outputs["Index"], pick.inputs["ID"])
    pick.inputs["Min"].default_value = 0.0
    pick.inputs["Max"].default_value = 1.0
    pick.inputs["Seed"].default_value = 1
    links.new(pick.outputs["Value"], compare.inputs["A"])
    compare.inputs["B"].default_value = 0.3
    links.new(compare.outputs["Result"], invert.inputs[0])
    links.new(distribute.outputs["Points"], tree_instances.inputs["Points"])
    links.new(distribute.outputs["Points"], bush_instances.inputs["Points"])
    links.new(compare.outputs["Result"], tree_instances.inputs["Selection"])
    links.new(invert.outputs[0], bush_instances.inputs["Selection"])
    links.new(trees.outputs["Instances"], tree_instances.inputs["Instance"])
    links.new(bushes.outputs["Instances"], bush_instances.inputs["Instance"])
    links.new(index.outputs["Index"], choice.inputs["ID"])
    choice.inputs["Min"].default_value = 0
    choice.inputs["Max"].default_value = 100
    choice.inputs["Seed"].default_value = 1
    links.new(choice.outputs["Value"], tree_instances.inputs["Instance Index"])
    links.new(choice.outputs["Value"], bush_instances.inputs["Instance Index"])
    links.new(index.outputs["Index"], rotate_value.inputs["ID"])
    rotate_value.inputs["Seed"].default_value = 18
    rotate_value.inputs["Min"].default_value = 0.0
    rotate_value.inputs["Max"].default_value = tau
    links.new(rotate_value.outputs["Value"], combine.inputs["Z"])
    links.new(combine.outputs["Vector"], euler.inputs["Euler"])
    links.new(euler.outputs["Rotation"], tree_instances.inputs["Rotation"])
    links.new(euler.outputs["Rotation"], bush_instances.inputs["Rotation"])
    links.new(tree_instances.outputs["Instances"], join.inputs["Geometry"])
    links.new(bush_instances.outputs["Instances"], join.inputs["Geometry"])
    links.new(join.outputs["Geometry"], realize.inputs["Geometry"])
    links.new(realize.outputs["Geometry"], group_output.inputs["Geometry"])
    return group


def _configure_plant(group: bpy.types.NodeTree, density: float, share: float, seed: int, trees, bushes) -> None:
    """Свои числа на копии группы: в Blender 5.2 вход модификатора не пишется как свойство."""
    for node in group.nodes:
        if node.label == "distribute":
            node.inputs["Density"].default_value = float(density)
            node.inputs["Seed"].default_value = int(seed)
        elif node.label == "share":
            node.inputs["B"].default_value = float(share)
        elif node.label == "pick":
            node.inputs["Seed"].default_value = int(seed)
        elif node.label == "choice":
            node.inputs["Seed"].default_value = int(seed)
        elif node.label == "rotate":
            node.inputs["Seed"].default_value = int(seed) + 17
        elif node.label == "trees":
            node.inputs["Collection"].default_value = trees
        elif node.label == "bushes":
            node.inputs["Collection"].default_value = bushes


def _zone_from_object(obj: bpy.types.Object, edit: bool) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]], str]:
    if edit:
        import bmesh

        mesh = bmesh.from_edit_mesh(obj.data)
        selected = [face for face in mesh.faces if face.select]
        if not selected:
            raise PlacementError("Выберите полигоны зоны")
        world = obj.matrix_world
        vertices: list[tuple[float, float, float]] = []
        index_of: dict[int, int] = {}
        faces: list[tuple[int, ...]] = []
        for face in selected:
            polygon = []
            for vertex in face.verts:
                if vertex.index not in index_of:
                    index_of[vertex.index] = len(vertices)
                    co = world @ vertex.co
                    vertices.append((co.x, co.y, co.z))
                polygon.append(index_of[vertex.index])
            faces.append(tuple(polygon))
    else:
        world = obj.matrix_world
        vertices = [tuple(world @ vertex.co) for vertex in obj.data.vertices]
        faces = [tuple(int(index) for index in polygon.vertices) for polygon in obj.data.polygons]
    if len(vertices) < 3 or not faces:
        raise PlacementError("У зоны нет полигона")
    area = polygon_area_xy(vertices, faces)
    if area < MIN_ZONE_AREA_M2:
        raise PlacementError("Полигон меньше 1 м²")
    sample = tuple(sorted((round(vertex[0], 2), round(vertex[1], 2)) for vertex in vertices)[:8])
    return vertices, faces, f"{obj.name}:{area:.1f}:{sample}"


def _placement_source(obj: bpy.types.Object) -> bool:
    """Библиотека и уже расставленные копии не являются зоной."""
    return _under_collection(obj, LIBRARY_NAME) or _under_collection(obj, ROOT_NAME)


def scene_zone_names() -> set[str]:
    """Имена полигонов, по которым можно расставлять: объект, коллекция или dxf_layer."""
    names: set[str] = set()

    def walk(collection: bpy.types.Collection) -> None:
        if collection.name in {LIBRARY_NAME, ROOT_NAME}:
            return
        names.add(collection.name)
        for obj in collection.objects:
            if obj.type != "MESH" or _placement_source(obj):
                continue
            names.add(obj.name)
            stored = str(obj.get("dxf_layer") or "")
            if stored:
                names.add(stored)
        for child in collection.children:
            walk(child)

    scene = bpy.context.scene if bpy.context else None
    if scene is not None:
        walk(scene.collection)
    return names


def _meshes_named(layer_name: str) -> list[bpy.types.Object]:
    """Сетки сцены с именем слоя: подложка или любая другая модель."""
    found: list[bpy.types.Object] = []
    seen: set[str] = set()
    target = layer_name[:63]

    def take(obj: bpy.types.Object) -> None:
        if obj.type != "MESH" or obj.name in seen or _placement_source(obj):
            return
        stored = str(obj.get("dxf_layer") or "")
        if stored == layer_name or obj.name == target:
            seen.add(obj.name)
            found.append(obj)

    def walk(collection: bpy.types.Collection) -> None:
        if collection.name in {LIBRARY_NAME, ROOT_NAME}:
            return
        named = collection.name == layer_name or collection.name == target
        for obj in collection.objects:
            if named:
                if obj.type == "MESH" and obj.name not in seen and not _placement_source(obj):
                    seen.add(obj.name)
                    found.append(obj)
            else:
                take(obj)
        for child in collection.children:
            walk(child)

    scene = bpy.context.scene if bpy.context else None
    if scene is not None:
        walk(scene.collection)
    return found


def _listed_layer_meshes(context) -> list[bpy.types.Object]:
    """Слой, подсвеченный в списке плагина: клик по строке не выбирает объект в 3D-виде."""
    scene = getattr(context, "scene", None)
    items = getattr(scene, "dwgreader_selected", None) if scene is not None else None
    if not items:
        return []
    index = int(getattr(scene, "dwgreader_selected_index", 0) or 0)
    if index < 0 or index >= len(items):
        return []
    layer_name = str(items[index].layer_name or "").strip()
    if not layer_name:
        return []
    return _meshes_named(layer_name)


_LAST_ZONE = ""
_LAST_BENCH_NAME = ""
_PLANT_OWNER = object()


def _context_active_object(context):
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None and view_layer.objects.active is not None:
        return view_layer.objects.active
    return getattr(context, "active_object", None)


def _remember_zone(obj: bpy.types.Object | None) -> None:
    global _LAST_ZONE
    if obj is not None and obj.type == "MESH" and not _placement_source(obj):
        _LAST_ZONE = obj.name


def _track_clicked_zone() -> None:
    context = bpy.context
    if context is None:
        return
    _sync_bench_roundness(context)


def _active_zone_mesh(context) -> bpy.types.Object | None:
    """Полигон, который пользователь кликнул. Соседние выделенные объекты не берём."""
    active = _context_active_object(context)
    if active is not None and active.type == "MESH" and not _placement_source(active):
        _remember_zone(active)
        return active
    return None


def _slider_zone_mesh(context) -> bpy.types.Object | None:
    """Ползунок меняет только полигон последнего клика, не весь слой из списка."""
    picked = _active_zone_mesh(context)
    if picked is not None:
        return picked
    remembered = bpy.data.objects.get(_LAST_ZONE) if _LAST_ZONE else None
    if remembered is not None and remembered.type == "MESH" and not _placement_source(remembered):
        return remembered
    return None


def _single_underlay_mesh(context) -> bpy.types.Object | None:
    return _active_zone_mesh(context)


def _underlay_selection(context) -> list:
    """В правке — активный объект. В объекте — все выделенные сетки подложки."""
    if getattr(context, "mode", "OBJECT") == "EDIT_MESH":
        active = _context_active_object(context)
        if active is not None and active.type == "MESH" and not _placement_source(active):
            return [active]
        return []
    found = []
    for obj in list(getattr(context, "selected_objects", []) or []):
        if obj is None or obj.type != "MESH" or _placement_source(obj):
            continue
        found.append(obj)
    return found


def _selected_zones(
    context,
    only: bpy.types.Object | None = None,
    allow_layer_fallback: bool = True,
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]], str, float]]:
    edit = context.mode == "EDIT_MESH"
    active = _context_active_object(context)
    if only is not None:
        objects = [only]
    elif edit:
        objects = [active]
    else:
        objects = _underlay_selection(context)
        if not objects:
            picked = _active_zone_mesh(context)
            if picked is not None:
                objects = [picked]
            elif not allow_layer_fallback:
                raise PlacementError("Выберите полигон в 3D-виде")
            else:
                objects = _listed_layer_meshes(context)
    zones = []
    for obj in objects:
        if obj is None or obj.type != "MESH" or _placement_source(obj):
            continue
        vertices, faces, key = _zone_from_object(obj, edit and obj == context.active_object)
        height = sum(vertex[2] for vertex in vertices) / len(vertices)
        zones.append((vertices, faces, key, height))
    if not zones:
        raise PlacementError("Выберите слой в списке или полигон в 3D-виде")
    if edit:
        bpy.ops.object.mode_set(mode="OBJECT")
    return zones


def _replace_owned(collection: bpy.types.Collection, zone_key: str, prefix: bool = False) -> None:
    names: list[str] = []
    head = f"{zone_key}:"
    for obj in list(collection.objects):
        try:
            stored = str(obj.get("zone_key") or "")
        except ReferenceError:
            continue
        if stored == zone_key or (prefix and stored.startswith(head)):
            names.append(obj.name)
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        groups = [
            modifier.node_group
            for modifier in obj.modifiers
            if modifier.type == "NODES" and modifier.node_group is not None and modifier.node_group.name != PLANT_GROUP
        ]
        mesh = obj.data if obj.type == "MESH" else None
        curve = obj.data if obj.type == "CURVE" else None
        child_names = [child.name for child in list(obj.children)]
        for child_name in child_names:
            child = bpy.data.objects.get(child_name)
            if child is not None:
                bpy.data.objects.remove(child, do_unlink=True)
        obj = bpy.data.objects.get(name)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, "users", 1) == 0:
            bpy.data.meshes.remove(mesh)
        if curve is not None and getattr(curve, "users", 1) == 0:
            bpy.data.curves.remove(curve)
        for group in groups:
            if group.users == 0:
                bpy.data.node_groups.remove(group)


_PLANT_LOCK = False
_PLANT_PENDING = False
_PLANT_AGAIN = False
_PLANT_UI_SEEN = False
_PLANT_TARGETS: list[str] = []
_PLANT_REQUEST: dict = {"names": None, "lawns": False}
_PLANT_SCOPE = "zone"
_PLAY_LOCK = False
_PLAY_PENDING = False
_PLAY_UI_SEEN = False
_PLAY_SCOPE = "all"


def _is_plant_zone(obj) -> bool:
    """Полигон подложки. Дома и уже стоящие посадки зоной не являются."""
    if obj is None or getattr(obj, "type", None) != "MESH" or _placement_source(obj):
        return False
    return _under_collection(obj, UNDERLAY_NAME)


def _selected_underlay_meshes(context) -> list:
    return [obj for obj in _underlay_selection(context) if _is_plant_zone(obj)]


def _remember_plant_targets(names: list[str]) -> None:
    global _PLANT_TARGETS
    _PLANT_TARGETS = list(names)


def _plant_target_objects(context, *, remember: bool = False) -> list:
    """Выделенные полигоны подложки. Пустое выделение оставляет прошлый набор."""
    selected = _selected_underlay_meshes(context)
    names = choose_plant_names([obj.name for obj in selected], _PLANT_TARGETS)
    if selected:
        if remember:
            _remember_plant_targets(names)
        by_name = {obj.name: obj for obj in selected}
        return [by_name[name] for name in names if name in by_name]
    found = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if _is_plant_zone(obj):
            found.append(obj)
    return found


def _zones_of(context, objects) -> list:
    edit = getattr(context, "mode", "OBJECT") == "EDIT_MESH"
    active = _context_active_object(context)
    zones = []
    for obj in objects:
        if not _is_plant_zone(obj):
            continue
        vertices, faces, key = _zone_from_object(obj, bool(edit and obj == active))
        height = sum(vertex[2] for vertex in vertices) / len(vertices)
        zones.append((vertices, faces, key, height))
    if not zones:
        raise PlacementError("Выделите полигоны подложки")
    if edit:
        bpy.ops.object.mode_set(mode="OBJECT")
    return zones


def _plant_target_ready(context) -> bool:
    return bool(_plant_target_objects(context))


def _model_scale(context, prop: str) -> float:
    return max(float(getattr(context.scene, prop, 1.0) or 1.0), 0.05)


def _scale_placed(kind: str, scale: float) -> None:
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        return
    areas = []
    for obj in root.all_objects:
        if kind == "linear" and obj.get("placement") == "linear":
            for child in obj.children:
                if child.type == "MESH":
                    child.scale = (scale, scale, scale)
        elif obj.get("placement") == kind and obj.type == "MESH":
            obj.scale = (scale, scale, scale)
            if kind == "area":
                areas.append(obj)
    if areas:
        view_layer = getattr(bpy.context, "view_layer", None)
        if view_layer is not None:
            view_layer.update()
    unsafe = sum(not _validate_playground(bpy.context, obj) for obj in areas)
    if unsafe:
        _status(bpy.context, f"После масштабирования зона безопасности не помещается у объектов: {unsafe}")


def _pick_plant(rng: random.Random, trees: list[dict], bushes: list[dict], share: float) -> dict:
    use_tree = bool(trees) and (not bushes or rng.random() < share)
    pool = trees if use_tree else bushes
    if not pool:
        pool = trees or bushes
    return pool[rng.randrange(len(pool))]


def _asset_plan_side(asset: dict, mesh_object) -> float:
    """Сторона карты по сетке после импорта, не по числу из catalog.json."""
    stored = asset.get("plan_side")
    if stored is not None and float(stored) > 0.0:
        return float(stored)
    vertices = [
        (float(vertex.co.x), float(vertex.co.y), float(vertex.co.z))
        for vertex in mesh_object.data.vertices
    ]
    side = mesh_plan_side(vertices)
    asset["plan_side"] = side
    return side


def _object_rings(obj) -> list[list[tuple[float, float]]]:
    world = obj.matrix_world
    vertices = obj.data.vertices
    rings: list[list[tuple[float, float]]] = []
    for polygon in obj.data.polygons:
        ring = []
        for index in polygon.vertices:
            co = world @ vertices[index].co
            ring.append((float(co.x), float(co.y)))
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _category_rings(category: str) -> list[list[tuple[float, float]]]:
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        return []
    rings: list[list[tuple[float, float]]] = []
    for obj in list(root.all_objects):
        try:
            if obj.type != "MESH" or obj.data is None or not obj.data.polygons:
                continue
            if str(obj.get("category") or "") != category:
                continue
            rings.extend(_object_rings(obj))
        except ReferenceError:
            continue
    return rings


def _underlay_ring_groups() -> list[list[list[tuple[float, float]]]]:
    """Кольца каждого объекта подложки, без фильтра категории."""
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        return []
    groups: list[list[list[tuple[float, float]]]] = []
    for obj in list(root.all_objects):
        try:
            if obj.type != "MESH" or obj.data is None or not obj.data.polygons:
                continue
            rings = _object_rings(obj)
            if rings:
                groups.append(rings)
        except ReferenceError:
            continue
    return groups


def _placed_house_marks(context) -> tuple[list[list[tuple[float, float]]], list[tuple[float, float]]]:
    """Контур уже стоящего дома и точка, по которой ищется полигон под ним."""
    from .building_export import buildings_in

    scene = getattr(context, "scene", None)
    if scene is None:
        return [], []
    footprints: list[list[tuple[float, float]]] = []
    points: list[tuple[float, float]] = []
    for obj in buildings_in(scene):
        try:
            origin = obj.matrix_world.translation
            points.append((float(origin.x), float(origin.y)))
            if obj.type == "MESH" and obj.data is not None and obj.data.polygons:
                footprints.extend(_object_rings(obj))
        except ReferenceError:
            continue
    return footprints, points


def _plant_obstacles(context=None) -> PlantObstacles:
    """Здания, покрытия и полигон под уже стоящим домом.

    Контур дома закрывает дерево и куст. Полигон, на котором дом стоит,
    закрывает только дерево, как бы этот полигон ни назывался.
    """
    buildings = _category_rings("building")
    hosts: list[list[tuple[float, float]]] = []
    if context is not None:
        footprints, points = _placed_house_marks(context)
        buildings.extend(footprints)
        if points:
            hosts = host_rings(_underlay_ring_groups(), points)
    return PlantObstacles(buildings, _category_rings("pavement"), hosts)


def _plant_zone_key(obj) -> str:
    stored = str(obj.get("zone_key") or "")
    if stored:
        return stored
    parent = getattr(obj, "parent", None)
    if parent is None:
        return ""
    return str(parent.get("zone_key") or "")


def _is_plant_instance(obj) -> bool:
    if str(obj.get("family") or "") in {"tree", "bush"}:
        return True
    parent = getattr(obj, "parent", None)
    return parent is not None and str(parent.get("placement") or "") == "scatter"


def _stored_plan_side(obj) -> float:
    stored = obj.get("plan_side")
    if stored is not None and float(stored) > 0.0:
        return float(stored)
    data = getattr(obj, "data", None)
    vertices = getattr(data, "vertices", None)
    if not vertices:
        return 0.0
    return mesh_plan_side([
        (float(vertex.co.x), float(vertex.co.y), float(vertex.co.z))
        for vertex in vertices
    ])


def _existing_footprints(context, skip_keys: set[str]) -> OccupiedFootprints:
    """Следы растений, которые этот проход не снимает."""
    occupied = OccupiedFootprints()
    view_layer = getattr(context, "view_layer", None)
    if view_layer is not None and hasattr(view_layer, "update"):
        view_layer.update()
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        return occupied
    for obj in list(root.all_objects):
        try:
            if obj.type != "MESH" or not _is_plant_instance(obj):
                continue
            if _plant_zone_key(obj) in skip_keys:
                continue
            plan_side = _stored_plan_side(obj)
            if plan_side <= 0.0:
                continue
            world = obj.matrix_world
            center = (float(world.translation.x), float(world.translation.y))
            yaw = float(world.to_euler("XYZ").z)
            plant_scale = abs(float(world.to_scale().x))
        except ReferenceError:
            continue
        occupied.add(plant_footprint(center, plan_side, yaw, plant_scale))
    return occupied


def _run_plantings(
    context,
    only: bpy.types.Object | None = None,
    allow_layer_fallback: bool = True,
    occupied: OccupiedFootprints | None = None,
    obstacles: PlantObstacles | None = None,
    targets: list | None = None,
) -> tuple[int, str]:
    """Копии моделей, как у лавок. Геоноды в окне Blender оставляли слой пустым.

    Встаёт не точка, а квадрат карты. occupied общий на проход: None — собрать
    растения чужих зон здесь. Препятствия тоже можно принести снаружи, один раз.
    targets — явный набор полигонов: активный объект и слой списка не подмешиваются.
    """
    if targets is not None:
        zones = _zones_of(context, targets)
    else:
        zones = _selected_zones(context, only=only, allow_layer_fallback=allow_layer_fallback)
    density = float(context.scene.dwgreader_density) / 100.0
    share = float(context.scene.dwgreader_tree_share) / 100.0
    seed = int(context.scene.dwgreader_seed)
    tree_assets = _assets(context, "tree")
    bush_assets = _assets(context, "bush")
    tree_choice = str(getattr(context.scene, "dwgreader_tree_model", "tree_01") or "tree_01")
    bush_choice = str(getattr(context.scene, "dwgreader_bush_model", "all") or "all")
    if tree_choice != "all":
        tree_assets = [asset for asset in tree_assets if asset["id"] == tree_choice]
    if bush_choice != "all":
        bush_assets = [asset for asset in bush_assets if asset["id"] == bush_choice]
    if not tree_assets and not bush_assets:
        raise PlacementError("В каталоге нет выбранных деревьев и кустов")
    scale = _model_scale(context, "dwgreader_plant_scale")
    sources: dict[str, bpy.types.Object] = {}

    def source_for(asset: dict) -> bpy.types.Object:
        cached = sources.get(asset["id"])
        if cached is None:
            cached = _import_mesh(context, asset)
            sources[asset["id"]] = cached
        return cached

    avoid_ground = bool(getattr(context.scene, "dwgreader_trees_avoid_polygons", False))
    if obstacles is None:
        obstacles = _plant_obstacles(context)
    if occupied is None:
        occupied = _existing_footprints(context, {key for _vertices, _faces, key, _height in zones})
    collection = _placement_collection("plantings")
    total = 0
    clamped = False
    for vertices, faces, key, _height in zones:
        area = polygon_area_xy(vertices, faces)
        count = plant_slot_count(area, density)
        if count > MAX_PLANTS:
            clamped = True
            count = MAX_PLANTS
        _replace_owned(collection, key)
        if count <= 0:
            continue
        # Запас точек на отказы. Первые count совпадают с прежней выборкой того же сида.
        points = scatter_points(vertices, faces, count * PLANT_ATTEMPTS, seed)
        if not points:
            raise PlacementError("На полигоне нет места для посадок")
        center = polygon_centroid_xy(vertices, faces) or points[0]
        parent = None
        rng = random.Random(seed + 1)
        cursor = 0
        placed = 0
        for _slot in range(count):
            if cursor >= len(points):
                break
            families = slot_attempt_families(
                lambda: _pick_plant(rng, tree_assets, bush_assets, share),
                PLANT_ATTEMPTS,
            )
            asset = families[0]
            source = source_for(asset)
            plan_side = _asset_plan_side(asset, source)
            for asset in families:
                if cursor >= len(points):
                    break
                point = points[cursor]
                cursor += 1
                yaw = rng.random() * tau
                quad = plant_footprint((point[0], point[1]), plan_side, yaw, scale)
                if obstacles.blocks(quad, str(asset.get("family") or ""), avoid_ground=avoid_ground):
                    continue
                if occupied.blocks(quad):
                    continue
                if parent is None:
                    parent = bpy.data.objects.new(f"PLANT_{key[:24]}", None)
                    parent.empty_display_type = "PLAIN_AXES"
                    parent.empty_display_size = 0.5
                    parent.location = center
                    parent["placement"] = "scatter"
                    parent["zone_key"] = key
                    parent["seed"] = seed
                    parent["density"] = float(context.scene.dwgreader_density)
                    parent["tree_share"] = float(context.scene.dwgreader_tree_share)
                    collection.objects.link(parent)
                inst = source.copy()
                inst.data = source.data
                inst.name = f"PLANT_{asset['id']}_{placed}"
                inst.parent = parent
                inst.rotation_mode = "XYZ"
                inst.location = (point[0] - center[0], point[1] - center[1], point[2] - center[2])
                inst.rotation_euler = (0.0, 0.0, yaw)
                inst.scale = (scale, scale, scale)
                inst.hide_viewport = False
                inst.hide_render = False
                inst.hide_select = False
                inst["asset_id"] = asset["id"]
                inst["family"] = asset["family"]
                inst["plan_side"] = plan_side
                inst["zone_key"] = key
                collection.objects.link(inst)
                inst.hide_set(False)
                occupied.add(quad)
                placed += 1
                total += 1
                break
    if total <= 0:
        return 0, "Посадки: 0 шт."
    if clamped:
        return total, f"Посадки: {total} шт. На слой больше {MAX_PLANTS} не ставим — снизьте плотность, чтобы разредить"
    return total, f"Посадки: {total} шт."


def _on_plant_update(self, context):
    global _PLANT_PENDING, _PLANT_AGAIN, _PLANT_REQUEST
    if not _PLANT_UI_SEEN or context is None or _busy(context):
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    all_lawns = _PLANT_SCOPE == "lawns"
    pending_names: list[str] | None = None
    if not all_lawns:
        if getattr(scene, "dwgreader_place_mode", "plantings") != "plantings":
            return
        targets = _plant_target_objects(context, remember=True)
        if not targets:
            _status(context, "Выделите полигоны подложки")
            return
        pending_names = [obj.name for obj in targets]
    _PLANT_REQUEST = {"names": pending_names, "lawns": all_lawns}
    if _PLANT_LOCK:
        _PLANT_AGAIN = True
        return
    if _PLANT_PENDING:
        return
    _PLANT_PENDING = True

    def _later():
        global _PLANT_LOCK, _PLANT_PENDING, _PLANT_AGAIN
        _PLANT_PENDING = False
        current = bpy.context
        if current is None or _PLANT_LOCK or _busy(current):
            return None
        request = _PLANT_REQUEST
        _PLANT_LOCK = True
        try:
            if request["lawns"]:
                note = _replant_lawns(current)
            else:
                objects = []
                for name in request["names"] or []:
                    obj = bpy.data.objects.get(name)
                    if _is_plant_zone(obj):
                        objects.append(obj)
                if not objects:
                    _status(current, "Выделите полигоны подложки")
                    return None
                _count, note = _run_plantings(current, targets=objects)
            _status(current, note)
        except PlacementError as exc:
            _status(current, str(exc))
        except Exception as exc:
            _status(current, f"Посадки: {exc}")
        finally:
            _PLANT_LOCK = False
            if _PLANT_AGAIN:
                _PLANT_AGAIN = False
                _on_plant_update(None, current)
        return None

    bpy.app.timers.register(_later, first_interval=0.2)


def note_play_scope(scope: str) -> None:
    """all — все найденные площадки. selection — выделенные полигоны, иначе все найденные."""
    global _PLAY_SCOPE, _PLAY_UI_SEEN
    _PLAY_SCOPE = "selection" if scope == "selection" else "all"
    _PLAY_UI_SEEN = True


def _schedule_play_refill(context) -> None:
    global _PLAY_PENDING
    if _PLAY_LOCK or not _PLAY_UI_SEEN or context is None or _busy(context):
        return
    if _PLAY_PENDING:
        return
    _PLAY_PENDING = True

    def _later():
        global _PLAY_LOCK, _PLAY_PENDING
        _PLAY_PENDING = False
        current = bpy.context
        if current is None or _PLAY_LOCK or _busy(current):
            return None
        _PLAY_LOCK = True
        try:
            note = refill_playgrounds(current, prefer_selection=_PLAY_SCOPE == "selection")
            _status(current, note)
        except PlacementError as exc:
            _status(current, str(exc))
        except Exception as exc:
            _status(current, f"Площадки: {exc}")
        finally:
            _PLAY_LOCK = False
        return None

    bpy.app.timers.register(_later, first_interval=0.2)


def _on_play_seed(self, context):
    _schedule_play_refill(context)


def _safety_scale(context) -> float:
    scene = getattr(context, "scene", None)
    raw = float(getattr(scene, "dwgreader_safety_scale", 1.0) or 1.0)
    return min(max(raw, 0.25), 1.0)


def _on_safety_scale(self, context):
    _schedule_play_refill(context)


def _zone_key_for(context, obj) -> str | None:
    edit = getattr(context, "mode", "OBJECT") == "EDIT_MESH"
    active = _context_active_object(context)
    try:
        _vertices, _faces, key = _zone_from_object(obj, bool(edit and obj == active))
    except PlacementError:
        return None
    return key


def _plant_objects(context, objects) -> tuple[int, int, bool]:
    """Сажает полигоны одним проходом: общий след и одни препятствия на всех.

    В след заранее попадают только растения чужих зон — те, что проход не снимает.
    """
    if getattr(context, "mode", "OBJECT") == "EDIT_MESH":
        bpy.ops.object.mode_set(mode="OBJECT")
    ready = []
    skip: set[str] = set()
    for obj in objects:
        key = _zone_key_for(context, obj)
        if key is None:
            continue
        ready.append(obj)
        skip.add(key)
    occupied = _existing_footprints(context, skip)
    obstacles = _plant_obstacles(context)
    total = 0
    used = 0
    clamped = False
    for obj in ready:
        try:
            count, note = _run_plantings(
                context,
                only=obj,
                allow_layer_fallback=False,
                occupied=occupied,
                obstacles=obstacles,
            )
        except PlacementError:
            continue
        total += int(count)
        used += 1
        if "не ставим" in note:
            clamped = True
    return total, used, clamped


def _replant_lawns(context) -> str:
    """Заново ставит деревья и кусты на каждый газон подложки."""
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        raise PlacementError("Подложки в сцене нет")
    lawns = [
        obj for obj in list(root.all_objects)
        if obj.type == "MESH" and str(obj.get("category") or "") == "green"
        and getattr(obj, "data", None) is not None and obj.data.polygons
    ]
    total, used, clamped = _plant_objects(context, lawns)
    if used <= 0:
        raise PlacementError("В подложке нет газонов")
    text = f"Посадки: {total} шт. на газонах {used}"
    if clamped:
        text += ". На слой больше лимита не ставим"
    return text


def draw_plant_controls(layout, context, *, all_lawns: bool) -> None:
    """Плотность, доля деревьев, сид, модель и размер. На шаге сборки — все газоны."""
    global _PLANT_UI_SEEN, _PLANT_SCOPE
    _PLANT_UI_SEEN = True
    _PLANT_SCOPE = "lawns" if all_lawns else "zone"
    scene = context.scene
    layout.prop(scene, "dwgreader_tree_model", text="Дерево")
    layout.prop(scene, "dwgreader_trees_avoid_polygons")
    layout.prop(scene, "dwgreader_bush_model", text="Куст")
    layout.prop(scene, "dwgreader_tree_share")
    layout.prop(scene, "dwgreader_density")
    layout.prop(scene, "dwgreader_seed")
    layout.prop(scene, "dwgreader_plant_scale", text="Размер деревьев и кустов")


def playground_layer_names() -> list[str]:
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for obj in root.all_objects:
        try:
            matched = _playground_mesh(obj)
        except ReferenceError:
            continue
        if not matched:
            continue
        name = str(obj.get("dxf_layer") or obj.name)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _new_mesh_object(name: str, vertices, faces, collection: bpy.types.Collection) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


class DWGREADER_OT_place_plantings(Operator):
    bl_idname = "dwgreader.place_plantings"
    bl_label = "Расставить посадки"
    bl_description = "Случайные деревья и кусты по выделенным полигонам подложки"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return not _busy(context)

    def execute(self, context):
        try:
            targets = _plant_target_objects(context, remember=True)
            if not targets:
                raise PlacementError("Выделите полигоны подложки")
            _count, note = _run_plantings(context, targets=targets)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _status(context, note)
        self.report({"INFO"}, note)
        return {"FINISHED"}


class DWGREADER_OT_place_benches(Operator):
    bl_idname = "dwgreader.place_benches"
    bl_label = "Нарисовать линию лавок"
    bl_description = "Клики задают линию, прямую или извилистую. Enter ставит лавки вдоль неё, Esc отменяет"
    bl_options = {"UNDO"}

    points: list[tuple[float, float]]
    hover: tuple[float, float] | None = None

    @classmethod
    def poll(cls, context):
        return not _busy(context)

    def invoke(self, context, event):
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "Кликайте в окне 3D-вида")
            return {"CANCELLED"}
        try:
            load_catalog(context)
        except PlacementError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if float(context.scene.dwgreader_bench_step) < MIN_BENCH_STEP_M:
            self.report({"ERROR"}, "Шаг меньше 0,5 м")
            return {"CANCELLED"}
        self.points = []
        self.hover = None
        self._area = context.area
        self._handle = bpy.types.SpaceView3D.draw_handler_add(_draw_segment, (self,), "WINDOW", "POST_VIEW")
        context.window_manager.modal_handler_add(self)
        _status(context, "Лавки: кликайте по линии. Enter — поставить, Backspace — шаг назад")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN", "TRACKPADZOOM", "NDOF_MOTION"}:
            return {"PASS_THROUGH"}
        if event.type == "MOUSEMOVE":
            self.hover = _ground_point(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            point = _ground_point(context, event)
            if point is None:
                return {"RUNNING_MODAL"}
            if self.points and hypot(point[0] - self.points[-1][0], point[1] - self.points[-1][1]) < 0.05:
                return {"RUNNING_MODAL"}
            self.points.append(point)
            _status(context, f"Лавки: точек {len(self.points)}. Enter — поставить, Backspace — шаг назад")
            return {"RUNNING_MODAL"}
        if event.type == "BACK_SPACE" and event.value == "PRESS":
            if self.points:
                self.points.pop()
            _status(context, f"Лавки: точек {len(self.points)}. Enter — поставить")
            if context.area is not None:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type in {"RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS":
            if len(self.points) < 2:
                _status(context, "Нужны хотя бы две точки линии")
                return {"RUNNING_MODAL"}
            try:
                count = _place_benches(context, list(self.points))
            except PlacementError as exc:
                self._finish_draw()
                _status(context, str(exc))
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self._finish_draw()
            _status(context, f"Лавки: {count} шт., шаг {context.scene.dwgreader_bench_step:.1f} м")
            return {"FINISHED"}
        if event.type in {"RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            self._finish_draw()
            _status(context, "Линия лавок отменена")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def _finish_draw(self) -> None:
        if getattr(self, "_handle", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, "WINDOW")
            self._handle = None


def _island_mesh(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    group: list[int],
) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    remap: dict[int, int] = {}
    island_vertices: list[tuple[float, float, float]] = []
    island_faces: list[tuple[int, ...]] = []
    for face_index in group:
        polygon = []
        for index in faces[face_index]:
            if index not in remap:
                remap[index] = len(island_vertices)
                island_vertices.append(vertices[index])
            polygon.append(remap[index])
        island_faces.append(tuple(polygon))
    return island_vertices, island_faces


def _world_mesh(obj: bpy.types.Object) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    world = obj.matrix_world
    vertices = [tuple(world @ vertex.co) for vertex in obj.data.vertices]
    faces = [tuple(int(index) for index in polygon.vertices) for polygon in obj.data.polygons]
    return vertices, faces


def _play_spec(asset: dict) -> dict:
    footprint = asset["footprint"]
    fall = asset.get("fall_height")
    return {
        "id": asset["id"],
        "width": float(footprint[0]),
        "depth": float(footprint[1]),
        "clearance": playground_clearance(asset),
        "motion": str(asset.get("motion") or "static"),
        "role": str(asset.get("role") or "activity"),
        "fall_height": float(fall) if fall is not None else min(float(footprint[2]), 3.0),
        "group": str(asset.get("group") or play_group(asset)),
        "runout": asset.get("runout"),
        "runout_side": asset.get("runout_side"),
    }


def _obstacle_meshes(exclude=None) -> list:
    skip = exclude if isinstance(exclude, (set, list, tuple)) else {exclude}
    found = []
    for root_name in (UNDERLAY_NAME, ROOT_NAME):
        root = bpy.data.collections.get(root_name)
        if root is None:
            continue
        for obj in root.all_objects:
            if obj.type == "MESH" and obj.data is not None and obj not in skip:
                found.append(obj)
    return found


def _fence_blocked(meshes, exclude=None) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]] | None:
    """Заборы, здания и уже стоящие МАФ. Деревья, кусты и сам полигон площадки не мешают."""
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    placed_root = bpy.data.collections.get(ROOT_NAME)
    placed_names = {obj.name for obj in placed_root.all_objects} if placed_root is not None else set()
    skip = exclude if isinstance(exclude, (set, list, tuple)) else {exclude}
    for obj in meshes:
        if obj in skip or obj.type != "MESH" or obj.data is None:
            continue
        parent = getattr(obj, "parent", None)
        parent_placement = str(parent.get("placement") or "") if parent is not None else ""
        family = str(obj.get("family") or "")
        if parent is not None and not family:
            family = str(parent.get("family") or "")
        placed = obj.name in placed_names
        if not blocks_playground(
            str(obj.get("category") or ""),
            str(obj.get("placement") or ""),
            parent_placement,
            family,
            placed,
        ):
            continue
        base = len(vertices)
        world = obj.matrix_world
        if placed:
            corners = [world @ Vector(corner) for corner in obj.bound_box]
            if not corners:
                continue
            xs = [float(point.x) for point in corners]
            ys = [float(point.y) for point in corners]
            z = min(float(point.z) for point in corners)
            vertices.extend([
                (min(xs), min(ys), z), (max(xs), min(ys), z),
                (max(xs), max(ys), z), (min(xs), max(ys), z),
            ])
            faces.extend([(base, base + 1, base + 2), (base, base + 2, base + 3)])
            continue
        for vertex in obj.data.vertices:
            point = world @ vertex.co
            vertices.append((float(point.x), float(point.y), float(point.z)))
        for polygon in obj.data.polygons:
            faces.append(tuple(base + int(index) for index in polygon.vertices))
    if not faces:
        return None
    return vertices, faces


def _playground_blocked(exclude=None):
    return _fence_blocked(_obstacle_meshes(exclude=exclude), exclude=exclude)


def _remember_playground_zone(obj, vertices, faces) -> None:
    obj["zone_vertices_json"] = json.dumps(vertices, separators=(",", ":"))
    obj["zone_faces_json"] = json.dumps(faces, separators=(",", ":"))


def _validate_playground(context, obj) -> bool:
    try:
        vertices = [tuple(point) for point in json.loads(str(obj.get("zone_vertices_json") or "[]"))]
        faces = [tuple(face) for face in json.loads(str(obj.get("zone_faces_json") or "[]"))]
        if not vertices or not faces:
            return True
        spec = _play_spec(_asset(context, str(obj.get("asset_id") or "")))
        scale = max(float(obj.scale.x), 0.0)
        inside = safety_inside(
            vertices, faces, spec["width"], spec["depth"], spec["clearance"],
            float(obj.rotation_euler.z), (float(obj.location.x), float(obj.location.y)),
            blocked=_fence_blocked(_obstacle_meshes(exclude=obj), exclude=obj), scale=scale,
        )
    except Exception:
        logging.warning("Не удалось проверить зону безопасности %s", getattr(obj, "name", ""), exc_info=True)
        obj["safety_ok"] = False
        return False
    obj["safety_ok"] = bool(inside)
    return bool(inside)


def _spawn_playground(context, asset: dict, location: tuple[float, float, float], rotation: float, key: str, scale: float):
    collection = _placement_collection("playgrounds")
    source = _import_mesh(context, asset)
    obj = source.copy()
    obj.data = source.data
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_select = False
    obj.name = f"PLAY_{asset['id']}"
    obj.location = location
    obj.rotation_euler = (0.0, 0.0, rotation)
    obj.scale = (scale, scale, scale)
    obj["placement"] = "area"
    obj["family"] = "playground"
    obj["asset_id"] = asset["id"]
    obj["zone_key"] = key
    collection.objects.link(obj)
    obj.hide_set(False)
    return obj


def _rubber_material(obj) -> bool:
    slots = list(getattr(obj, "material_slots", []) or [])
    materials = [getattr(slot, "material", None) for slot in slots]
    data = getattr(obj, "data", None)
    materials.extend(list(getattr(data, "materials", []) or []))
    return any(material is not None and material.name == "MAT_rubber" for material in materials)


def _mesh_islands(obj, edit: bool):
    if edit:
        vertices, faces, _key = _zone_from_object(obj, True)
    else:
        vertices, faces = _world_mesh(obj)
    groups = face_groups(faces) or [list(range(len(faces)))]
    islands = []
    for index, group in enumerate(groups):
        island_vertices, island_faces = _island_mesh(vertices, faces, group)
        islands.append((island_vertices, island_faces, f"{obj.name}:{index}"))
    return islands


def _pad_material(kind: str):
    name = "MAT_play_rubber" if kind == "rubber" else "MAT_play_sand"
    color = (0.23, 0.52, 0.72, 1.0) if kind == "rubber" else (0.78, 0.68, 0.46, 1.0)
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
        material.diffuse_color = color
        if hasattr(material, "use_nodes"):
            material.use_nodes = True
        tree = getattr(material, "node_tree", None)
        node = tree.nodes.get("Principled BSDF") if tree is not None else None
        if node is not None and "Base Color" in node.inputs:
            node.inputs["Base Color"].default_value = color
    return material


def _spawn_pad(ring: list[tuple[float, float]], key: str, kind: str, height: float):
    mesh = bpy.data.meshes.new("PLAY_PAD")
    mesh.from_pydata([(float(x), float(y), height + 0.02) for x, y in ring], [], [tuple(range(len(ring)))])
    mesh.update()
    mesh.materials.append(_pad_material(kind))
    obj = bpy.data.objects.new("PLAY_PAD", mesh)
    obj["placement"] = "pad"
    obj["family"] = "pad"
    obj["zone_key"] = key
    obj["pad_kind"] = kind
    _placement_collection("playgrounds").objects.link(obj)
    obj.hide_set(False)
    return obj


def _layout_bench(context) -> tuple[dict | None, dict | None]:
    try:
        catalog = load_catalog(context)
    except PlacementError:
        return None, None
    asset = next((item for item in catalog if item.get("id") == "bench_01"), None)
    if asset is None:
        return None, None
    urn = next((item for item in catalog if item.get("id") == "urn_01"), None)
    spec = {
        "width": float(asset["footprint"][0]),
        "depth": float(asset["footprint"][1]),
        "urn_width": float(urn["footprint"][0]) if urn is not None else 0.0,
        "urn_depth": float(urn["footprint"][1]) if urn is not None else 0.0,
    }
    return asset, spec


def _spawn_layout_bench(context, asset: dict, point: dict, key: str, height: float):
    source = _import_mesh(context, asset)
    obj = source.copy()
    obj.data = source.data
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_select = False
    obj.name = f"BENCH_{asset['id']}"
    obj.location = (float(point["x"]), float(point["y"]), height)
    obj.rotation_euler = (0.0, 0.0, float(point["angle"]))
    obj["placement"] = "linear"
    obj["family"] = "bench"
    obj["asset_id"] = asset["id"]
    obj["zone_key"] = key
    _placement_collection("benches").objects.link(obj)
    obj.hide_set(False)
    return obj


def _spawn_layout_urn(context, asset: dict, point: dict, key: str, height: float):
    source = _import_mesh(context, asset)
    obj = source.copy()
    obj.data = source.data
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_select = False
    obj.name = f"URN_{asset['id']}"
    obj.location = (float(point["x"]), float(point["y"]), height)
    obj.rotation_euler = (0.0, 0.0, float(point["angle"]))
    obj["placement"] = "point"
    obj["family"] = "urn"
    obj["asset_id"] = asset["id"]
    obj["zone_key"] = key
    _placement_collection("benches").objects.link(obj)
    obj.hide_set(False)
    return obj


def _fill_playground_on(
    context, vertices, faces, key: str, blocked=None, replace: bool = True,
    kind: str | None = None, seed: int | None = None, with_benches: bool = True,
) -> tuple[int, bool]:
    catalog = _play_assets(context, kind)
    if not catalog:
        raise PlacementError("В каталоге нет моделей выбранного типа площадки")
    collection = _placement_collection("playgrounds")
    benches = _placement_collection("benches")
    if replace:
        _replace_owned(collection, key, prefix=True)
        _replace_owned(benches, key, prefix=True)
    if blocked is None:
        blocked = _playground_blocked()
    if seed is None:
        seed_value = int(getattr(context.scene, "dwgreader_play_seed", 1))
    else:
        seed_value = int(seed)
    bench_asset, bench_spec = _layout_bench(context) if with_benches else (None, None)
    layout = compose_playground(
        vertices, faces, [_play_spec(asset) for asset in catalog], blocked=blocked,
        seed=seed_value, bench=bench_spec, clearance_scale=_safety_scale(context),
    )
    catalog_by_id = {asset["id"]: asset for asset in catalog}
    centroid = polygon_centroid_xy(vertices, faces)
    height = float(centroid[2]) if centroid is not None else 0.0
    for item in layout["items"]:
        asset = catalog_by_id[item["id"]]
        obj = _spawn_playground(
            context, asset, (float(item["x"]), float(item["y"]), height), float(item["angle"]), key, 1.0,
        )
        _remember_playground_zone(obj, vertices, faces)
        obj["safety_ok"] = True
        pad = item.get("pad")
        if pad and item.get("pad_kind"):
            _spawn_pad(pad, key, str(item["pad_kind"]), height)
    if bench_asset is not None:
        for point in layout["benches"]:
            _spawn_layout_bench(context, bench_asset, point, key, height)
    urn_asset = None
    if layout.get("urns"):
        try:
            urn_asset = _asset(context, "urn_01")
        except PlacementError:
            urn_asset = None
    if urn_asset is not None:
        for point in layout["urns"]:
            _spawn_layout_urn(context, urn_asset, point, key, height)
    return len(layout["items"]), bool(layout["hero_missing"])


def _fill_islands(context, islands, zones=None, kind: str | None = None, seed: int | None = None, merge_nearby: bool = False) -> tuple[int, bool, int]:
    if not _play_assets(context, kind):
        raise PlacementError("В каталоге нет моделей выбранного типа площадки")
    collection = _placement_collection("playgrounds")
    benches = _placement_collection("benches")
    for _vertices, _faces, key in islands:
        _replace_owned(collection, key, prefix=True)
        _replace_owned(benches, key, prefix=True)
    work = cluster_nearby_islands(islands) if merge_nearby else islands
    blocked = _playground_blocked(exclude=zones)
    total = 0
    missing = False
    bench_keys = bench_island_keys(work)
    for vertices, faces, key in work:
        count, missed = _fill_playground_on(
            context, vertices, faces, key, blocked, replace=False, kind=kind, seed=seed,
            with_benches=key in bench_keys,
        )
        total += count
        missing = missing or missed
    return total, missing, len(work)


def _all_playground_meshes() -> list:
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        return []
    return [
        obj for obj in root.all_objects
        if obj.type == "MESH" and getattr(obj, "data", None) is not None and obj.data.polygons and _playground_mesh(obj)
    ]


def _play_targets(context, prefer_selection: bool) -> list:
    """Выделенные площадки. Газон и дорога в этот набор не входят."""
    selected = [obj for obj in _underlay_selection(context) if _playground_mesh(obj)]
    if prefer_selection or selected:
        return selected
    return _all_playground_meshes()


def refill_playgrounds(context, prefer_selection: bool = False) -> str:
    """Снимает прежние модели полигона и ставит раскладку заново."""
    targets = _play_targets(context, prefer_selection)
    if not targets:
        if prefer_selection:
            return "Выделите площадку, чтобы сменить расстановку"
        return "Площадок для расстановки нет"
    edit = getattr(context, "mode", "OBJECT") == "EDIT_MESH"
    active = _context_active_object(context)
    islands = []
    for obj in targets:
        try:
            islands.extend(_mesh_islands(obj, edit and obj == active))
        except PlacementError:
            continue
    if not islands:
        return "Выберите полигоны площадки"
    total, hero_missing, filled = _fill_islands(context, islands, targets, merge_nearby=prefer_selection)
    if total:
        note = f"На площадках {total} объектов, полигонов {filled}"
        if hero_missing:
            note += ". Крупный объект не влез"
        return note
    return "На полигон не встала ни одна модель: зона безопасности не помещается"


def _place_playground_on(
    context,
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    key: str,
) -> str:
    catalog = _play_assets(context)
    if not catalog:
        raise PlacementError("В каталоге нет площадок")
    flat = [(vertex[0], vertex[1]) for vertex in vertices]
    rectangle = minimum_rectangle(flat)
    if rectangle is None:
        raise PlacementError("У зоны нет габарита")
    zone_width, zone_depth, edge_angle, rect_center = rectangle
    asset_id = str(getattr(context.scene, "dwgreader_play_asset", "") or "")
    asset = next((item for item in catalog if item["id"] == asset_id), None)
    if asset is None:
        asset = catalog[0]
    center = polygon_centroid_xy(vertices, faces)
    if center is None:
        center = (rect_center[0], rect_center[1], 0.0)
    scale = _model_scale(context, "dwgreader_play_scale")
    rotation = orientation_angle(edge_angle, _playground_orientation(zone_width, zone_depth, asset, scale))
    obj = _spawn_playground(context, asset, (float(center[0]), float(center[1]), float(center[2])), rotation, key, scale)
    obj["zone_width"] = zone_width
    obj["zone_depth"] = zone_depth
    obj["edge_angle"] = edge_angle
    _remember_playground_zone(obj, vertices, faces)
    spec = _play_spec(asset)
    clearance = tuple(float(value) * _safety_scale(context) for value in spec["clearance"])
    inside = safety_inside(
        vertices, faces, spec["width"], spec["depth"], clearance, rotation,
        (float(center[0]), float(center[1])),
        blocked=_fence_blocked(_obstacle_meshes(exclude=obj), exclude=obj), scale=scale,
    )
    obj["safety_ok"] = bool(inside)
    note = f"Площадка добавлена: {asset['name']}, полигон {zone_width:.1f}×{zone_depth:.1f} м"
    if not inside:
        note += ". Зона безопасности выходит за полигон или пересекает препятствие"
    return note


def furnish_underlay(context) -> str:
    """Деревья на газонах, модели на площадках и дома на полигонах зданий."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return ""
    trees_on = bool(getattr(scene, "dwgreader_auto_trees", False))
    maf_on = bool(getattr(scene, "dwgreader_auto_maf", False))
    notes: list[str] = []
    root = bpy.data.collections.get(UNDERLAY_NAME)
    meshes = []
    if root is not None:
        meshes = [
            obj for obj in root.all_objects
            if obj.type == "MESH" and getattr(obj, "data", None) is not None and obj.data.polygons
        ]
    if trees_on:
        lawns = [obj for obj in meshes if str(obj.get("category") or "") == "green"]
        plants, used, _clamped = _plant_objects(context, lawns)
        notes.append(f"деревья {plants} на газонах {used}")
    if maf_on:
        zones = [obj for obj in meshes if _playground_mesh(obj)]
        if not zones:
            notes.append("площадки не найдены: нужно имя с «площад», «игров», «спорт» или «резин»")
        else:
            placed = 0
            hero_missing = False
            failed = ""
            seed_value = int(getattr(scene, "dwgreader_play_seed", 1))
            for kind, group in _auto_playground_groups(zones, scene):
                group_islands = []
                for obj in group:
                    group_islands.extend(_mesh_islands(obj, False))
                try:
                    count, missing, _filled = _fill_islands(context, group_islands, group, kind=kind, seed=seed_value)
                except PlacementError as exc:
                    failed = str(exc)
                    continue
                placed += count
                hero_missing = hero_missing or missing
            if failed and placed == 0:
                notes.append(failed)
            else:
                note = f"площадки {placed}"
                if hero_missing:
                    note += ", крупный объект не влез"
                if failed:
                    note += f", {failed}"
                notes.append(note)
    try:
        from . import building_export
        buildings = building_export.place_auto_buildings(context)
    except Exception as exc:
        buildings = f"здания не встали: {exc}"
    if buildings:
        notes.append(buildings)
    if not notes:
        return ""
    return "Расставлено: " + ", ".join(notes)


def _auto_playground_groups(zones, scene) -> list[tuple[str, list]]:
    """Случайно — свой тип на полигон. Детские и спортивные — один каталог на все."""
    ordered = sorted(zones, key=lambda obj: str(obj.get("dxf_layer") or obj.name))
    mode = str(getattr(scene, "dwgreader_auto_play_kind", "random") or "random")
    seed_value = int(getattr(scene, "dwgreader_play_seed", 1))
    kinds = auto_playground_kinds(
        [str(obj.get("dxf_layer") or obj.name) for obj in ordered], mode, seed_value,
    )
    groups: dict[str, list] = {"sports": [], "children": []}
    for obj, kind in zip(ordered, kinds):
        groups[kind].append(obj)
    return [(kind, group) for kind, group in groups.items() if group]


def _playground_mesh(obj) -> bool:
    category = str(obj.get("category") or "")
    if category not in {"pavement", "furniture"}:
        return False
    layer = str(obj.get("dxf_layer") or obj.name)
    texture = str(obj.get("texture") or "")
    if category != "pavement":
        texture = ""
    elif not texture and _rubber_material(obj):
        texture = "rubber"
    return is_playground_surface(layer, texture)


def _underlay_hit(context, event) -> tuple[bpy.types.Object, int] | None:
    region = context.region
    region_3d = context.region_data
    coord = (event.mouse_region_x, event.mouse_region_y)
    if region is None or region.type != "WINDOW" or region_3d is None:
        area = next((item for item in context.screen.areas if item.type == "VIEW_3D"), None) if context.screen else None
        if area is None:
            return None
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
        region_3d = area.spaces.active.region_3d
        if region is None or region_3d is None:
            return None
        coord = (event.mouse_x - region.x, event.mouse_y - region.y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
    scene = context.scene
    if scene is None:
        return None
    best: tuple[float, bpy.types.Object, int] | None = None

    def walk(collection: bpy.types.Collection) -> None:
        nonlocal best
        if collection.name in {LIBRARY_NAME, ROOT_NAME}:
            return
        for obj in collection.objects:
            if obj.type != "MESH" or obj.data is None or not obj.data.polygons:
                continue
            inverse = obj.matrix_world.inverted_safe()
            local_origin = inverse @ origin
            local_direction = inverse.to_3x3() @ direction
            if local_direction.length < 1e-12:
                continue
            local_direction.normalize()
            hit, location, _normal, index = obj.ray_cast(local_origin, local_direction)
            if not hit or index < 0:
                continue
            distance = ((obj.matrix_world @ location) - origin).length
            if best is None or distance < best[0]:
                best = (distance, obj, int(index))
        for child in collection.children:
            walk(child)

    walk(scene.collection)
    if best is None:
        return None
    return best[1], best[2]


class DWGREADER_OT_place_playground(Operator):
    bl_idname = "dwgreader.place_playground"
    bl_label = "Поставить площадку"
    bl_description = "Добавляет модель в центр выбранного полигона. Уже стоящие площадки остаются"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return not _busy(context)

    def execute(self, context):
        try:
            zones = _selected_zones(context)
            for vertices, faces, _key, _height in zones:
                if len(face_groups(faces)) > 1:
                    raise PlacementError("В слое несколько полигонов. Нажмите «Подобрать» и кликните по нужному")
            note = self._place_zones(context, zones)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _status(context, note)
        self.report({"INFO"}, note)
        return {"FINISHED"}

    def invoke(self, context, event):
        try:
            zones = _selected_zones(context)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if any(len(face_groups(faces)) > 1 for vertices, faces, _key, _height in zones):
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            context.window_manager.modal_handler_add(self)
            _status(context, "Кликните по полигону площадки")
            return {"RUNNING_MODAL"}
        try:
            note = self._place_zones(context, zones)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _status(context, note)
        self.report({"INFO"}, note)
        return {"FINISHED"}

    def modal(self, context, event):
        if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN", "TRACKPADZOOM", "NDOF_MOTION"}:
            return {"PASS_THROUGH"}
        if event.type in {"RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            _status(context, "Площадка отменена")
            return {"CANCELLED"}
        if event.type != "LEFTMOUSE" or event.value != "PRESS":
            return {"RUNNING_MODAL"}
        hit = _underlay_hit(context, event)
        if hit is None:
            _status(context, "Кликните по полигону")
            return {"RUNNING_MODAL"}
        obj, face_index = hit
        try:
            vertices, faces = _world_mesh(obj)
            group = next(item for item in face_groups(faces) if face_index in item)
            island_vertices, island_faces = _island_mesh(vertices, faces, group)
            center = polygon_centroid_xy(island_vertices, island_faces)
            if center is None:
                raise PlacementError("У полигона нет площади")
            key = f"{obj.name}:{face_index}:{round(center[0], 1)}:{round(center[1], 1)}"
            note = _place_playground_on(context, island_vertices, island_faces, key)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except StopIteration:
            _status(context, "Не удалось найти полигон под курсором")
            return {"CANCELLED"}
        _status(context, note)
        self.report({"INFO"}, note)
        return {"FINISHED"}

    def _place_zones(self, context, zones) -> str:
        note = ""
        for vertices, faces, key, _height in zones:
            groups = face_groups(faces)
            if len(groups) > 1:
                raise PlacementError("В слое несколько полигонов. Нажмите «Подобрать» и кликните по нужному")
            if len(groups) == 1:
                vertices, faces = _island_mesh(vertices, faces, groups[0])
            note = _place_playground_on(context, vertices, faces, key)
        if not note:
            raise PlacementError("Выберите полигон")
        return note


class DWGREADER_OT_replace_model(Operator):
    bl_idname = "dwgreader.replace_model"
    bl_label = "Заменить модель"
    bl_description = "Следующая модель того же семейства. Точки и шаг остаются"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return _placement_target(context) is not None

    def execute(self, context):
        try:
            target = _placement_target(context)
            if target is None:
                raise PlacementError("Выберите лавки или площадку")
            if target.get("placement") == "area":
                _replace_playground(context, target)
            else:
                _replace_benches(context, target)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def _placement_target(context) -> bpy.types.Object | None:
    obj = context.active_object
    if obj is None:
        return None
    if obj.get("placement") in {"area", "linear"}:
        return obj
    parent = obj.parent
    if parent is not None and parent.get("placement") == "linear":
        return parent
    return None


def _playground_orientation(zone_width: float, zone_depth: float, asset: dict, scale: float = 1.0) -> str:
    spec = _play_spec(asset)
    front, back, left, right = spec["clearance"]
    factor = max(float(scale), 0.0)
    width = (spec["width"] + left + right) * factor
    depth = (spec["depth"] + front + back) * factor
    excess_a = max(width - zone_width, depth - zone_depth, 0.0)
    excess_b = max(width - zone_depth, depth - zone_width, 0.0)
    return "A" if excess_a <= excess_b else "B"


def _replace_playground(context, obj: bpy.types.Object) -> None:
    asset_id = str(getattr(context.scene, "dwgreader_play_asset", "") or "")
    asset = next((item for item in _play_assets(context) if item["id"] == asset_id), None)
    if asset is None:
        raise PlacementError("Выберите модель выбранного типа площадки в списке")
    source = _import_mesh(context, asset)
    obj.data = source.data
    obj["asset_id"] = asset["id"]
    obj.name = f"PLAY_{asset['id']}"
    scale = _model_scale(context, "dwgreader_play_scale")
    obj.scale = (scale, scale, scale)
    if "edge_angle" in obj.keys():
        rotation = orientation_angle(
            float(obj["edge_angle"]),
            _playground_orientation(float(obj["zone_width"]), float(obj["zone_depth"]), asset, scale),
        )
        obj.rotation_euler = (0.0, 0.0, rotation)
    safe = _validate_playground(context, obj)
    note = f"Площадка заменена: {asset['name']}"
    if not safe:
        note += ". Зона безопасности выходит за полигон или пересекает препятствие"
    _status(context, note)


def _replace_benches(context, parent: bpy.types.Object) -> None:
    family = list(_assets(context, "bench"))
    if not family:
        raise PlacementError("В каталоге нет лавок")
    asset_id = str(getattr(context.scene, "dwgreader_bench_asset", "") or "")
    asset = next((item for item in family if item["id"] == asset_id), None)
    if asset is None:
        raise PlacementError("Выберите модель лавки в списке")
    source = _import_mesh(context, asset)
    children = [child for child in parent.children if child.type == "MESH"]
    if not children:
        raise PlacementError("У отрезка нет лавок")
    scale = _model_scale(context, "dwgreader_bench_scale")
    for child in children:
        child.data = source.data
        child.scale = (scale, scale, scale)
    parent["asset_id"] = asset["id"]
    parent["depth"] = float(asset["footprint"][1])
    _status(context, f"Лавки заменены: {asset['name']}")


def _place_benches(context, points: list[tuple[float, float]]) -> int:
    step = float(context.scene.dwgreader_bench_step)
    if step < MIN_BENCH_STEP_M:
        raise PlacementError("Шаг меньше 0,5 м")
    if len(points) < 2:
        raise PlacementError("Нужны хотя бы две точки линии")
    asset_id = str(context.scene.dwgreader_bench_asset or "")
    asset = _asset(context, asset_id)
    if asset["family"] != "bench":
        raise PlacementError("Для линии нужна модель из семейства лавок")
    heading = str(getattr(context.scene, "dwgreader_bench_heading", "tangent") or "tangent")
    turn = pi if bool(getattr(context.scene, "dwgreader_bench_flip", False)) else 0.0
    depth = float(asset["footprint"][1])
    roundness = float(getattr(context.scene, "dwgreader_bench_roundness", 1.0) or 0.0)
    placed = bench_path(points, step, depth, heading, turn, roundness)
    if not placed:
        raise PlacementError("Линия слишком короткая")
    global _BENCH_LOCK
    _BENCH_LOCK = True
    try:
        count = _place_bench_row(
            context, points, asset, step, heading, turn, depth, roundness, placed,
        )
    finally:
        _BENCH_LOCK = False
    return count


def _place_bench_row(context, points, asset, step, heading, turn, depth, roundness, placed) -> int:
    source = _import_mesh(context, asset)
    collection = _placement_collection("benches")
    key = ":".join(f"{round(point[0], 2)},{round(point[1], 2)}" for point in points)
    _replace_owned(collection, key)
    parent = bpy.data.objects.new(f"BENCH_{asset['id']}", None)
    parent.empty_display_type = "PLAIN_AXES"
    parent.empty_display_size = 0.01
    parent["placement"] = "linear"
    parent["family"] = "bench"
    parent["asset_id"] = asset["id"]
    parent["zone_key"] = key
    parent["step"] = step
    parent["heading"] = heading
    parent["turn"] = turn
    parent["depth"] = depth
    parent["path_xy"] = _encode_path(points)
    parent["roundness"] = roundness
    collection.objects.link(parent)
    parent.hide_set(True)
    parent.hide_render = True
    scale = _model_scale(context, "dwgreader_bench_scale")
    for index, (x, y, angle) in enumerate(placed):
        inst = source.copy()
        inst.data = source.data
        inst.hide_viewport = False
        inst.hide_render = False
        inst.hide_select = False
        inst.name = f"BENCH_{asset['id']}_{index}"
        inst.parent = parent
        inst.matrix_parent_inverse.identity()
        inst.location = (x, y, 0.0)
        inst.rotation_mode = "XYZ"
        inst.rotation_euler = (0.0, 0.0, angle)
        inst.scale = (scale, scale, scale)
        inst["asset_id"] = asset["id"]
        collection.objects.link(inst)
    _mark_last_bench(parent)
    _select_only(context, [child for child in parent.children if child.type == "MESH"])
    return len(placed)


def _ground_point(context, event) -> tuple[float, float] | None:
    region = context.region
    region_3d = context.region_data
    if region is None or region_3d is None:
        return None
    coord = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
    if abs(direction.z) < 1e-8:
        return None
    distance = -origin.z / direction.z
    if distance < 0:
        return None
    point = origin + direction * distance
    return (point.x, point.y)


def _draw_segment(operator) -> None:
    points = list(getattr(operator, "points", []) or [])
    if operator.hover is not None:
        points.append(operator.hover)
    if len(points) < 2:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader

        line = []
        for start, end in zip(points, points[1:]):
            line.append((start[0], start[1], 0.05))
            line.append((end[0], end[1], 0.05))
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batch = batch_for_shader(shader, "LINES", {"pos": line})
        shader.bind()
        shader.uniform_float("color", (0.95, 0.75, 0.2, 1.0))
        batch.draw(shader)
    except Exception:
        return


def _family_model_items(family: str, empty_label: str):
    def items(self, context):
        try:
            assets = _assets(context, family)
        except PlacementError:
            assets = []
        rows = [("all", "Все из каталога", "Случайная модель этого семейства")]
        if not assets:
            rows.append(("none", empty_label, ""))
        rows.extend((asset["id"], asset["name"], asset["file"]) for asset in assets)
        return rows
    return items


_tree_model_items = _family_model_items("tree", "Нет деревьев в каталоге")
_bush_model_items = _family_model_items("bush", "Нет кустов в каталоге")


def _play_items(self, context):
    try:
        assets = _play_assets(context)
    except PlacementError:
        assets = []
    if not assets:
        return [("none", "Нет площадок в каталоге", "")]
    return [(asset["id"], asset["name"], asset["file"]) for asset in assets]


def _encode_path(points: list[tuple[float, float]]) -> str:
    return ";".join(f"{point[0]:.6f},{point[1]:.6f}" for point in points)


def _decode_path(raw: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for part in str(raw or "").replace(":", ";").split(";"):
        bits = part.split(",")
        if len(bits) != 2:
            continue
        try:
            points.append((float(bits[0]), float(bits[1])))
        except ValueError:
            continue
    return points


def _stored_bench_points(parent) -> list[tuple[float, float]]:
    points = _decode_path(str(parent.get("path_xy") or ""))
    if len(points) >= 2:
        return points
    return _decode_path(str(parent.get("zone_key") or ""))


def _row_depth(context, parent) -> float:
    if "depth" in parent.keys():
        return float(parent["depth"])
    depth = 0.0
    asset_id = str(parent.get("asset_id") or "")
    if asset_id:
        try:
            depth = float(_asset(context, asset_id)["footprint"][1])
        except Exception:
            depth = 0.0
    parent["depth"] = depth
    return depth


def _row_turn(parent, points, step: float, depth: float, heading: str, roundness: float) -> float:
    if "turn" in parent.keys():
        return float(parent["turn"])
    children = [child for child in parent.children if child.type == "MESH"]
    turn = 0.0
    if children:
        sample = bench_path(points, step, depth, heading, 0.0, roundness)
        if sample:
            delta = float(children[0].rotation_euler.z) - sample[0][2]
            delta = (delta + pi) % (2.0 * pi) - pi
            if abs(abs(delta) - pi) < 0.35:
                turn = pi
    parent["turn"] = turn
    return turn


def _retarget_benches(parent, placed: list[tuple[float, float, float]], scale: float) -> None:
    if not placed:
        return
    children = [child for child in parent.children if child.type == "MESH"]
    children.sort(key=lambda child: child.name)
    if not children:
        return
    collection = next(iter(parent.users_collection), None)
    if collection is None:
        return
    template = children[0]
    asset_id = str(parent.get("asset_id") or "bench")
    while len(children) < len(placed):
        inst = template.copy()
        inst.data = template.data
        inst.parent = parent
        inst.matrix_parent_inverse.identity()
        inst.hide_viewport = False
        inst.hide_render = False
        inst.hide_select = False
        inst.name = f"BENCH_{asset_id}_{len(children)}"
        inst["asset_id"] = asset_id
        collection.objects.link(inst)
        children.append(inst)
    for child, (x, y, angle) in zip(children, placed):
        child.location = (x, y, 0.0)
        child.rotation_mode = "XYZ"
        child.rotation_euler = (0.0, 0.0, angle)
        child.scale = (scale, scale, scale)
    for child in children[len(placed):]:
        bpy.data.objects.remove(child, do_unlink=True)


_BENCH_LOCK = False


def _bench_row_of(obj) -> bpy.types.Object | None:
    current = obj
    seen: set[str] = set()
    while current is not None and current.name not in seen:
        seen.add(current.name)
        if current.get("placement") == "linear" and current.get("family") == "bench":
            return current
        current = current.parent
    return None


def _selected_bench_rows(context) -> list:
    """Ряды, чьи лавки сейчас выделены. Активный ряд идёт первым."""
    selected = list(getattr(context, "selected_objects", None) or [])
    active = _context_active_object(context)
    ordered = [active] + [obj for obj in selected if obj is not active] if active in selected else selected
    rows = []
    seen: set[str] = set()
    for obj in ordered:
        parent = _bench_row_of(obj)
        if parent is None or parent.name in seen:
            continue
        seen.add(parent.name)
        rows.append(parent)
    return rows


def _last_bench_row():
    named = bpy.data.objects.get(_LAST_BENCH_NAME) if _LAST_BENCH_NAME else None
    if _bench_row_of(named) is named and named is not None:
        return named
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        return None
    for obj in root.all_objects:
        if obj.get("placement") == "linear" and obj.get("family") == "bench" and obj.get("bench_last"):
            return obj
    return None


def _roundness_targets(context) -> list:
    picked = _selected_bench_rows(context)
    if picked:
        return picked
    last = _last_bench_row()
    return [last] if last is not None else []


def _mark_last_bench(parent) -> None:
    global _LAST_BENCH_NAME
    _LAST_BENCH_NAME = parent.name
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        parent["bench_last"] = 1
        return
    for obj in root.all_objects:
        if obj.get("family") == "bench" and obj.get("bench_last") and obj.name != parent.name:
            del obj["bench_last"]
    parent["bench_last"] = 1


def _select_only(context, objects) -> None:
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        return
    for obj in list(view_layer.objects):
        if obj.select_get():
            obj.select_set(False)
    active = None
    for obj in objects:
        if obj.name not in view_layer.objects:
            continue
        obj.hide_set(False)
        obj.select_set(True)
        active = active or obj
    if active is not None:
        view_layer.objects.active = active


def _stored_roundness(parent) -> float | None:
    if "roundness" not in parent.keys():
        return None
    return min(max(float(parent["roundness"]), 0.0), 1.0)


def _stored_step(parent) -> float | None:
    if "step" not in parent.keys():
        return None
    return max(float(parent["step"]), MIN_BENCH_STEP_M)


def _sync_bench_roundness(context) -> None:
    """Ползунки показывают шаг и скругление выделенного ряда, иначе последнего поставленного."""
    global _BENCH_LOCK
    if context is None or _BENCH_LOCK:
        return
    rows = _roundness_targets(context)
    if not rows:
        return
    parent = rows[0]
    stored_roundness = _stored_roundness(parent)
    stored_step = _stored_step(parent)
    current_roundness = float(getattr(context.scene, "dwgreader_bench_roundness", 1.0) or 0.0)
    current_step = float(getattr(context.scene, "dwgreader_bench_step", 3.0) or 0.0)
    write_roundness = stored_roundness is not None and abs(current_roundness - stored_roundness) >= 1e-4
    write_step = stored_step is not None and abs(current_step - stored_step) >= 1e-4
    if not write_roundness and not write_step:
        return
    _BENCH_LOCK = True
    try:
        if write_roundness:
            context.scene.dwgreader_bench_roundness = stored_roundness
        if write_step:
            context.scene.dwgreader_bench_step = stored_step
    finally:
        _BENCH_LOCK = False


def _apply_bench_layout(context, parent, step: float, roundness: float, scale: float) -> None:
    points = _stored_bench_points(parent)
    if len(points) < 2:
        return
    heading = str(parent.get("heading") or "tangent")
    depth = _row_depth(context, parent)
    turn = _row_turn(parent, points, step, depth, heading, roundness)
    placed = bench_path(points, step, depth, heading, turn, roundness)
    _retarget_benches(parent, placed, scale)
    parent["step"] = step
    parent["roundness"] = roundness


def _reshape_benches(context, *, step: float | None = None, roundness: float | None = None) -> None:
    """Шаг и скругление двигают выделенные ряды, а без выделения — последний поставленный."""
    global _BENCH_LOCK
    if context is None or _BENCH_LOCK:
        return
    targets = _roundness_targets(context)
    if not targets:
        return
    scale = _model_scale(context, "dwgreader_bench_scale")
    selected = {obj.name for obj in _selected_bench_rows(context)}
    _BENCH_LOCK = True
    try:
        for parent in targets:
            row_step = step if step is not None else float(parent.get("step") or context.scene.dwgreader_bench_step)
            row_roundness = roundness if roundness is not None else _stored_roundness(parent)
            if row_roundness is None:
                row_roundness = float(getattr(context.scene, "dwgreader_bench_roundness", 1.0) or 0.0)
            _apply_bench_layout(context, parent, row_step, row_roundness, scale)
            if parent.name in selected:
                for child in parent.children:
                    if child.type == "MESH":
                        try:
                            child.select_set(True)
                        except RuntimeError:
                            pass
    finally:
        _BENCH_LOCK = False


def _on_bench_step(self, context):
    if context is None:
        return
    _reshape_benches(context, step=max(float(self.dwgreader_bench_step), MIN_BENCH_STEP_M))


def _on_bench_roundness(self, context):
    if context is None:
        return
    _reshape_benches(context, roundness=float(self.dwgreader_bench_roundness))


def _on_bench_scale(self, context):
    if context is None:
        return
    _scale_placed("linear", max(float(self.dwgreader_bench_scale), 0.05))


def _on_play_scale(self, context):
    if context is None:
        return
    _scale_placed("area", max(float(self.dwgreader_play_scale), 0.05))


def _bench_items(self, context):
    try:
        assets = _assets(context, "bench")
    except PlacementError:
        assets = []
    if not assets:
        return [("none", "Нет лавок в каталоге", "")]
    return [(asset["id"], asset["name"], asset["file"]) for asset in assets]


def _mode_items(self, context):
    return (
        ("plantings", "Посадки", "Деревья и кусты по зоне"),
        ("benches", "Лавки", "Модели вдоль нарисованной линии"),
        ("playgrounds", "Площадка", "Комплекс по габариту зоны"),
    )


_MODEL_PREVIEWS = None
_PREVIEW_QUEUE: list[str] = []
_PREVIEW_FAILED: set[str] = set()


def _preview_dir() -> Path:
    root = os.environ.get("APPDATA")
    folder = (Path(root) if root else Path.home()) / "DWGREADER" / "previews"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _render_model_preview(context, asset: dict) -> Path | None:
    dest = _preview_dir() / f"{asset['id']}.png"
    if dest.is_file() and dest.stat().st_size > 32:
        return dest
    selected = [obj for obj in list(getattr(context, "selected_objects", []) or []) if obj is not None]
    active = context.view_layer.objects.active if context.view_layer is not None else None
    previous_mode = getattr(context, "mode", "OBJECT")
    preview_scene = None
    copy = None
    camera = None
    light = None
    world = None
    try:
        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        source = _import_mesh(context, asset)
        preview_scene = bpy.data.scenes.new("_dwg_preview")
        copy = source.copy()
        copy.data = source.data
        preview_scene.collection.objects.link(copy)
        copy.hide_set(False)
        copy.location = (0.0, 0.0, 0.0)
        copy.rotation_euler = (0.9, 0.0, 0.6)
        span = max(float(asset["footprint"][0]), float(asset["footprint"][1]), float(asset["footprint"][2]), 1.0)
        camera_data = bpy.data.cameras.new("_dwg_preview_cam")
        camera = bpy.data.objects.new("_dwg_preview_cam", camera_data)
        preview_scene.collection.objects.link(camera)
        preview_scene.camera = camera
        camera.location = (span * 1.6, -span * 1.8, span * 1.1)
        direction = Vector((0.0, 0.0, span * 0.35)) - camera.location
        camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        light_data = bpy.data.lights.new("_dwg_preview_light", "SUN")
        light_data.energy = 3.0
        light = bpy.data.objects.new("_dwg_preview_light", light_data)
        preview_scene.collection.objects.link(light)
        light.rotation_euler = (0.7, 0.1, 0.5)
        world = bpy.data.worlds.new("_dwg_preview_world")
        preview_scene.world = world
        preview_scene.render.engine = "BLENDER_WORKBENCH"
        preview_scene.render.resolution_x = 256
        preview_scene.render.resolution_y = 192
        preview_scene.render.resolution_percentage = 100
        preview_scene.render.film_transparent = True
        preview_scene.render.filepath = str(dest)
        preview_scene.render.image_settings.file_format = "PNG"
        preview_scene.display.shading.light = "STUDIO"
        preview_scene.display.shading.color_type = "OBJECT"
        preview_scene.view_layers[0].update()
        with context.temp_override(scene=preview_scene, view_layer=preview_scene.view_layers[0]):
            bpy.ops.render.render(write_still=True)
    except Exception:
        if dest.is_file():
            dest.unlink(missing_ok=True)
        return None
    finally:
        if preview_scene is not None:
            bpy.data.scenes.remove(preview_scene)
        for obj in (copy, camera, light):
            if obj is not None and obj.name in bpy.data.objects:
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if data is not None and getattr(data, "users", 1) == 0:
                    if isinstance(data, bpy.types.Camera):
                        bpy.data.cameras.remove(data)
                    elif isinstance(data, bpy.types.Light):
                        bpy.data.lights.remove(data)
        if world is not None and world.name in bpy.data.worlds and world.users == 0:
            bpy.data.worlds.remove(world)
        try:
            if previous_mode != "OBJECT" and context.mode == "OBJECT":
                bpy.ops.object.mode_set(mode="EDIT")
            for obj in selected:
                obj.select_set(True)
            if active is not None and context.view_layer is not None:
                context.view_layer.objects.active = active
        except Exception:
            pass
    if dest.is_file() and dest.stat().st_size > 32:
        return dest
    return None


def _preview_timer():
    if not _PREVIEW_QUEUE:
        return None
    asset_id = _PREVIEW_QUEUE.pop(0)
    context = bpy.context
    try:
        path = _render_model_preview(context, _asset(context, asset_id))
        if path is None:
            _PREVIEW_FAILED.add(asset_id)
    except Exception:
        _PREVIEW_FAILED.add(asset_id)
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass
    return 0.2 if _PREVIEW_QUEUE else None


def model_preview_icon(context, asset_id: str) -> int:
    if not asset_id or asset_id in {"all", "none"} or asset_id in _PREVIEW_FAILED or _MODEL_PREVIEWS is None:
        return 0
    dest = _preview_dir() / f"{asset_id}.png"
    if dest.is_file() and dest.stat().st_size > 32:
        if asset_id not in _MODEL_PREVIEWS:
            _MODEL_PREVIEWS.load(asset_id, str(dest), "IMAGE")
        return _MODEL_PREVIEWS[asset_id].icon_id
    if asset_id not in _PREVIEW_QUEUE:
        _PREVIEW_QUEUE.append(asset_id)
        if not bpy.app.timers.is_registered(_preview_timer):
            bpy.app.timers.register(_preview_timer, first_interval=0.2)
    return 0


def _preview_ids(context) -> list[str]:
    scene = context.scene
    mode = scene.dwgreader_place_mode
    if mode == "benches":
        return [str(scene.dwgreader_bench_asset or "")]
    if mode == "playgrounds":
        return [str(scene.dwgreader_play_asset or "")]
    found = []
    tree = str(scene.dwgreader_tree_model or "")
    bush = str(scene.dwgreader_bush_model or "")
    if tree and tree != "all":
        found.append(tree)
    elif _assets(context, "tree"):
        found.append(_assets(context, "tree")[0]["id"])
    if bush and bush != "all":
        found.append(bush)
    return found


def _draw_model_preview(layout, context) -> None:
    row = layout.row(align=True)
    row.alignment = "CENTER"
    shown = False
    for asset_id in _preview_ids(context):
        icon = model_preview_icon(context, asset_id)
        if not icon:
            continue
        row.template_icon(icon_value=icon, scale=5.0)
        shown = True
    if not shown and any(asset_id not in _PREVIEW_FAILED for asset_id in _preview_ids(context)):
        layout.label(text="Превью модели готовится")


def draw_panel(layout, context) -> None:
    prefs = _prefs(context)
    box = layout.box()
    box.label(text="Озеленение и МАФ", icon="OUTLINER_OB_CURVES")
    if prefs is None:
        box.label(text="Нет настроек аддона")
        return
    try:
        load_catalog(context)
    except PlacementError as exc:
        box.label(text=str(exc))
        return
    global _PLANT_UI_SEEN
    _PLANT_UI_SEEN = True
    scene = context.scene
    try:
        tabs = box.row()
        tabs.scale_y = 1.2
        tabs.prop(scene, "dwgreader_place_mode", expand=True)
        mode = scene.dwgreader_place_mode
        if mode == "plantings":
            box.label(text="Посадки ставятся на выделенные полигоны")
            box.label(text="Снятое выделение сохраняет прошлый набор")
            if len(scene.dwgreader_selected):
                box.template_list(
                    "DWGREADER_UL_place_layers", "place",
                    scene, "dwgreader_selected",
                    scene, "dwgreader_selected_index",
                    rows=5,
                )
            draw_plant_controls(box, context, all_lawns=False)
            box.operator("dwgreader.place_plantings", text="Расставить", icon="OUTLINER_OB_CURVES")
        elif mode == "benches":
            box.prop(scene, "dwgreader_bench_asset", text="Модель")
            box.prop(scene, "dwgreader_bench_step")
            box.prop(scene, "dwgreader_bench_roundness", text="Скругление", slider=True)
            box.prop(scene, "dwgreader_bench_heading", text="Ориентация")
            box.prop(scene, "dwgreader_bench_flip")
            box.prop(scene, "dwgreader_bench_scale", text="Размер лавок и лежаков")
            box.operator("dwgreader.place_benches", text="Нарисовать линию", icon="CURVE_PATH")
            box.operator("dwgreader.replace_model", text="Заменить на выбранную", icon="FILE_REFRESH")
        else:
            note_play_scope("selection")
            box.label(text="«Заполнить» ставит несколько моделей, зона остаётся внутри полигона")
            box.prop(scene, "dwgreader_playground_type", text="Тип площадки")
            box.prop(scene, "dwgreader_play_seed", text="Seed МАФ")
            box.prop(scene, "dwgreader_safety_scale", text="Зона безопасности", slider=True)
            hint = box.column(align=True)
            hint.scale_y = 0.75
            hint.label(text="Тип применяется кнопкой «Заполнить». Сид и зона меняют выделенные полигоны.")
            box.prop(scene, "dwgreader_play_asset", text="Модель")
            box.prop(scene, "dwgreader_play_scale", text="Размер площадки")
            box.operator("dwgreader.fill_playground", text="Заполнить", icon="MESH_GRID")
            box.operator("dwgreader.place_playground", text="Поставить", icon="MESH_PLANE")
            box.operator("dwgreader.replace_model", text="Заменить на выбранную", icon="FILE_REFRESH")
        _draw_model_preview(box, context)
    except Exception as exc:
        box.label(text=str(exc))


class DWGREADER_OT_fill_playground(Operator):
    bl_idname = "dwgreader.fill_playground"
    bl_label = "Заполнить площадку"
    bl_description = "Ставит несколько моделей каталога так, чтобы зоны безопасности остались внутри полигона"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return not _busy(context)

    def execute(self, context):
        global _PLAY_LOCK
        if _PLAY_LOCK:
            return {"CANCELLED"}
        _PLAY_LOCK = True
        try:
            note = refill_playgrounds(context, prefer_selection=True)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            _PLAY_LOCK = False
        _status(context, note)
        self.report({"INFO"}, note)
        return {"FINISHED"}


CLASSES = (
    DWGREADER_OT_place_plantings,
    DWGREADER_OT_place_benches,
    DWGREADER_OT_place_playground,
    DWGREADER_OT_fill_playground,
    DWGREADER_OT_replace_model,
)


def register() -> None:
    global _MODEL_PREVIEWS
    _MODEL_PREVIEWS = previews.new()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.LayerObjects, "active"),
        owner=_PLANT_OWNER,
        args=(),
        notify=_track_clicked_zone,
    )
    bpy.types.Scene.dwgreader_place_mode = EnumProperty(
        name="Режим",
        items=_mode_items,
        default=0,
    )
    bpy.types.Scene.dwgreader_tree_share = FloatProperty(
        name="Доля деревьев, %",
        description="Остальное — кусты. Меняет посадки на полигоне, выбранном в 3D-виде",
        default=DEFAULT_TREE_SHARE,
        min=0.0,
        max=100.0,
        subtype="PERCENTAGE",
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_tree_model = EnumProperty(
        name="Дерево",
        items=_tree_model_items,
        default="tree_01",
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_trees_avoid_polygons = BoolProperty(
        name="Деревья не заходят на полигоны",
        description="Крона дерева не пересекает покрытия, как у куста",
        default=False,
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_bush_model = EnumProperty(
        name="Куст",
        items=_bush_model_items,
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_plant_scale = FloatProperty(
        name="Размер",
        description="Во сколько раз увеличить или уменьшить дерево и куст",
        default=1.0,
        min=0.1,
        soft_max=4.0,
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_density = FloatProperty(
        name="Плотность, шт/100 м²",
        default=DEFAULT_PLANT_DENSITY,
        min=0.1,
        soft_max=40.0,
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_seed = IntProperty(
        name="Seed",
        default=1,
        min=0,
        max=1_000_000,
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_bench_step = FloatProperty(
        name="Шаг, м",
        description="Расстояние между лавками. Двигается выделенный ряд, а без выделения — последний поставленный",
        default=3.0,
        min=0.5,
        soft_max=30.0,
        update=_on_bench_step,
    )
    bpy.types.Scene.dwgreader_bench_roundness = FloatProperty(
        name="Скругление",
        description="0 — лавки по отрезкам между кликами, 1 — плавная дуга. Двигается выделенный ряд, а без выделения — последний поставленный",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_on_bench_roundness,
    )
    bpy.types.Scene.dwgreader_bench_asset = EnumProperty(
        name="Лавка",
        items=_bench_items,
    )
    bpy.types.Scene.dwgreader_bench_heading = EnumProperty(
        name="Ориентация",
        description="По линии: длинная сторона вдоль пути, лавки стоят боком друг к другу. Одно направление: весь ряд смотрит в одну сторону",
        items=(
            ("tangent", "По линии", "Каждая лавка поворачивается по касательной и смотрит поперёк пути"),
            ("uniform", "Одно направление", "Один поворот на всю линию, боком к отрезку от первой точки к последней"),
        ),
        default="tangent",
    )
    bpy.types.Scene.dwgreader_bench_flip = BoolProperty(
        name="Развернуть",
        description="Повернуть лавки на 180 градусов относительно выбранной ориентации",
        default=False,
    )
    bpy.types.Scene.dwgreader_bench_scale = FloatProperty(
        name="Размер лавок и лежаков",
        description="Во сколько раз увеличить или уменьшить лавку и лежак",
        default=1.0,
        min=0.1,
        soft_max=4.0,
        update=_on_bench_scale,
    )
    bpy.types.Scene.dwgreader_playground_type = EnumProperty(
        name="Тип площадки",
        description="Набор моделей для заполнения выделенных площадок и одиночной постановки",
        items=(
            ("children", "Детская", "Игровые комплексы, горки, качели, песочницы и домики"),
            ("sports", "Спортивная", "Спортивные комплексы"),
        ),
        default="children",
        update=_on_playground_type,
    )
    bpy.types.Scene.dwgreader_safety_scale = FloatProperty(
        name="Зона безопасности",
        description="1 — заложенная зона. Меньше сужает полосу, и встают модели, которые раньше не влезали",
        default=1.0,
        min=0.25,
        max=1.0,
        step=5,
        precision=2,
        update=_on_safety_scale,
    )
    bpy.types.Scene.dwgreader_auto_play_kind = EnumProperty(
        name="Тип площадок",
        description="Какой каталог ставить при автоматической расстановке. Случайно выбирает тип отдельно на каждом полигоне",
        items=(
            ("random", "Случайно", "На каждом полигоне отдельно детская или спортивная"),
            ("children", "Детские", "Игровые комплексы, горки, качели, песочницы и домики"),
            ("sports", "Спортивные", "Спортивные комплексы"),
        ),
        default="random",
    )
    bpy.types.Scene.dwgreader_play_seed = IntProperty(
        name="Seed МАФ",
        description="Меняет выбор и позиции моделей и сразу переставляет площадки",
        default=1,
        min=0,
        max=1_000_000,
        update=_on_play_seed,
    )
    bpy.types.Scene.dwgreader_play_asset = EnumProperty(
        name="Площадка",
        items=_play_items,
    )
    bpy.types.Scene.dwgreader_play_scale = FloatProperty(
        name="Размер площадки",
        description="Во сколько раз увеличить или уменьшить площадку",
        default=1.0,
        min=0.1,
        soft_max=4.0,
        update=_on_play_scale,
    )


def unregister() -> None:
    global _MODEL_PREVIEWS
    if bpy.app.timers.is_registered(_preview_timer):
        bpy.app.timers.unregister(_preview_timer)
    if _MODEL_PREVIEWS is not None:
        previews.remove(_MODEL_PREVIEWS)
        _MODEL_PREVIEWS = None
    bpy.msgbus.clear_by_owner(_PLANT_OWNER)
    for name in (
        "dwgreader_play_scale",
        "dwgreader_play_asset",
        "dwgreader_play_seed",
        "dwgreader_safety_scale",
        "dwgreader_auto_play_kind",
        "dwgreader_playground_type",
        "dwgreader_bench_scale",
        "dwgreader_bench_flip",
        "dwgreader_bench_heading",
        "dwgreader_bench_asset",
        "dwgreader_bench_roundness",
        "dwgreader_bench_step",
        "dwgreader_plant_scale",
        "dwgreader_bush_model",
        "dwgreader_trees_avoid_polygons",
        "dwgreader_tree_model",
        "dwgreader_seed",
        "dwgreader_density",
        "dwgreader_tree_share",
        "dwgreader_place_mode",
    ):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
