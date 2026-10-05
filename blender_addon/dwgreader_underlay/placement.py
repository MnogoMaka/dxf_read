"""Расстановка деревьев, лавок и площадок по подложке.

Модели читаются из папки с catalog.json, по одному GLB на объект.
Общий файл библиотеки не используется.
"""

from __future__ import annotations

import json
import random
from math import tau
from pathlib import Path

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty
from bpy.types import Operator
from bpy_extras import view3d_utils
from mathutils import Vector

from .placement_math import (
    bench_positions,
    face_groups,
    forward_angle,
    minimum_rectangle,
    orientation_angle,
    polygon_area_xy,
    polygon_centroid_xy,
    rank_playgrounds,
    scatter_points,
)

ROOT_NAME = "SITE_PLACEMENT"
UNDERLAY_NAME = "SITE_UNDERLAY"
LIBRARY_NAME = "SITE_LIBRARY"
PLANT_GROUP = "DWGREADER_Plantings"
GROUP_VERSION = 3
MIN_ZONE_AREA_M2 = 1.0
MIN_BENCH_STEP_M = 0.5
MAX_PLANTS = 2500

_CATALOG: list[dict] | None = None
_CATALOG_STAMP: tuple[str, int] | None = None
_DRAW = None


class PlacementError(RuntimeError):
    pass


def _prefs(context):
    package = (__package__ or "dwgreader_underlay").split(".")[0]
    addon = context.preferences.addons.get(package)
    return addon.preferences if addon is not None else None


def library_path(context) -> Path | None:
    prefs = _prefs(context)
    if prefs is None:
        return None
    raw = str(prefs.library_dir or "").strip()
    if not raw:
        return None
    path = Path(bpy.path.abspath(raw))
    return path if path.is_dir() else None


def load_catalog(context) -> list[dict]:
    global _CATALOG, _CATALOG_STAMP
    folder = library_path(context)
    if folder is None:
        raise PlacementError("Укажите папку библиотеки с catalog.json")
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
        assets.append({
            "id": str(item["id"]),
            "family": str(item.get("family") or ""),
            "placement": str(item.get("placement") or ""),
            "file": str(item["file"]),
            "name": str(item.get("name") or item["id"]),
            "footprint": [float(footprint[0]), float(footprint[1]), float(footprint[2])],
        })
    _CATALOG = assets
    _CATALOG_STAMP = stamp
    return assets


def _assets(context, family: str) -> list[dict]:
    return [asset for asset in load_catalog(context) if asset["family"] == family]


def _asset(context, asset_id: str) -> dict:
    for asset in load_catalog(context):
        if asset["id"] == asset_id:
            return asset
    raise PlacementError(f"В каталоге нет модели {asset_id}")


def _collection(name: str, parent: bpy.types.Collection | None) -> bpy.types.Collection:
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if parent is None:
        if collection.name not in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.link(collection)
    elif collection.name not in {child.name for child in parent.children}:
        parent.children.link(collection)
    return collection


def _placement_collection(kind: str) -> bpy.types.Collection:
    root = _collection(ROOT_NAME, None)
    return _collection({"plantings": "Plantings", "benches": "Benches", "playgrounds": "Playgrounds"}[kind], root)


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


def _import_mesh(context, asset: dict) -> bpy.types.Object:
    name = f"LIB_{asset['id']}"
    existing = bpy.data.objects.get(name)
    if existing is not None and existing.type == "MESH":
        _park_library_object(existing)
        return existing
    folder = library_path(context)
    if folder is None:
        raise PlacementError("Укажите папку библиотеки")
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
    mesh_object.name = name
    mesh_object.data.name = name
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
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        return []
    found: list[bpy.types.Object] = []

    def walk(collection: bpy.types.Collection) -> None:
        for obj in collection.objects:
            if obj.type != "MESH":
                continue
            stored = str(obj.get("dxf_layer") or "")
            if stored == layer_name or obj.name == layer_name[:63]:
                found.append(obj)
        for child in collection.children:
            walk(child)

    walk(root)
    return found


def _single_underlay_mesh(context) -> bpy.types.Object | None:
    """Один полигон в 3D-виде. После сборки выделены все слои сразу — это не выбор зоны."""
    active = context.active_object
    if active is None or active.type != "MESH" or not _under_collection(active, UNDERLAY_NAME):
        return None
    selected = [
        obj for obj in context.selected_objects
        if obj.type == "MESH" and _under_collection(obj, UNDERLAY_NAME)
    ]
    if len(selected) > 1:
        return None
    return active


def _selected_zones(context) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]], str, float]]:
    edit = context.mode == "EDIT_MESH"
    active = context.active_object
    picked = _single_underlay_mesh(context)
    if edit:
        objects = [active]
    elif picked is not None:
        objects = [picked]
    else:
        objects = _listed_layer_meshes(context)
        if not objects:
            objects = [
                obj for obj in context.selected_objects
                if obj.type == "MESH" and _under_collection(obj, UNDERLAY_NAME)
            ]
    zones = []
    for obj in objects:
        if obj is None or obj.type != "MESH":
            continue
        if not _under_collection(obj, UNDERLAY_NAME):
            raise PlacementError("Нужен полигон из подложки")
        vertices, faces, key = _zone_from_object(obj, edit and obj == context.active_object)
        height = sum(vertex[2] for vertex in vertices) / len(vertices)
        zones.append((vertices, faces, key, height))
    if not zones:
        raise PlacementError("Выберите слой в списке или полигон подложки в 3D-виде")
    if edit:
        bpy.ops.object.mode_set(mode="OBJECT")
    return zones


def _replace_owned(collection: bpy.types.Collection, zone_key: str) -> None:
    names: list[str] = []
    for obj in list(collection.objects):
        try:
            if obj.get("zone_key") == zone_key:
                names.append(obj.name)
        except ReferenceError:
            continue
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
_PLANT_UI_SEEN = False


def _plant_target_ready(context) -> bool:
    if bpy.data.collections.get(UNDERLAY_NAME) is None:
        return False
    if _single_underlay_mesh(context) is not None:
        return True
    return bool(_listed_layer_meshes(context))


def _pick_plant(rng: random.Random, trees: list[dict], bushes: list[dict], share: float) -> dict:
    use_tree = bool(trees) and (not bushes or rng.random() < share)
    pool = trees if use_tree else bushes
    if not pool:
        pool = trees or bushes
    return pool[rng.randrange(len(pool))]


def _run_plantings(context) -> tuple[int, str]:
    """Копии моделей, как у лавок. Геоноды в окне Blender оставляли слой пустым."""
    zones = _selected_zones(context)
    density = float(context.scene.dwgreader_density) / 100.0
    share = float(context.scene.dwgreader_tree_share) / 100.0
    seed = int(context.scene.dwgreader_seed)
    tree_assets = _assets(context, "tree")
    bush_assets = _assets(context, "bush")
    tree_choice = str(getattr(context.scene, "dwgreader_tree_model", "all") or "all")
    bush_choice = str(getattr(context.scene, "dwgreader_bush_model", "all") or "all")
    if tree_choice != "all":
        tree_assets = [asset for asset in tree_assets if asset["id"] == tree_choice]
    if bush_choice != "all":
        bush_assets = [asset for asset in bush_assets if asset["id"] == bush_choice]
    if not tree_assets and not bush_assets:
        raise PlacementError("В каталоге нет выбранных деревьев и кустов")
    scale = max(float(getattr(context.scene, "dwgreader_plant_scale", 1.0) or 1.0), 0.05)
    sources: dict[str, bpy.types.Object] = {}

    def source_for(asset: dict) -> bpy.types.Object:
        cached = sources.get(asset["id"])
        if cached is None:
            cached = _import_mesh(context, asset)
            sources[asset["id"]] = cached
        return cached

    collection = _placement_collection("plantings")
    total = 0
    clamped = False
    for vertices, faces, key, _height in zones:
        area = polygon_area_xy(vertices, faces)
        expected = int(area * density + 0.5)
        count = max(expected, 1)
        if count > MAX_PLANTS:
            clamped = True
            count = MAX_PLANTS
        points = scatter_points(vertices, faces, count, seed)
        if not points:
            raise PlacementError("На полигоне нет места для посадок")
        center = polygon_centroid_xy(vertices, faces) or points[0]
        _replace_owned(collection, key)
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
        rng = random.Random(seed + 1)
        for index, point in enumerate(points):
            asset = _pick_plant(rng, tree_assets, bush_assets, share)
            source = source_for(asset)
            inst = source.copy()
            inst.data = source.data
            inst.name = f"PLANT_{asset['id']}_{index}"
            inst.parent = parent
            inst.location = (point[0] - center[0], point[1] - center[1], point[2] - center[2])
            inst.rotation_euler = (0.0, 0.0, rng.random() * tau)
            inst.scale = (scale, scale, scale)
            inst.hide_viewport = False
            inst.hide_render = False
            inst.hide_select = False
            inst["asset_id"] = asset["id"]
            collection.objects.link(inst)
            inst.hide_set(False)
            total += 1
    if total <= 0:
        raise PlacementError("Посадки не встали. Выберите слой подложки и нажмите «Расставить»")
    if clamped:
        return total, f"Посадки: {total} шт. На слой больше {MAX_PLANTS} не ставим — снизьте плотность, чтобы разредить"
    return total, f"Посадки: {total} шт."


def _on_plant_update(self, context):
    global _PLANT_PENDING
    if _PLANT_LOCK or _PLANT_PENDING or not _PLANT_UI_SEEN or context is None or _busy(context):
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    if getattr(scene, "dwgreader_place_mode", "plantings") != "plantings":
        return
    if not _plant_target_ready(context):
        _status(context, "Выберите слой в списке или полигон подложки в 3D-виде")
        return
    _PLANT_PENDING = True

    def _later():
        global _PLANT_LOCK, _PLANT_PENDING
        _PLANT_PENDING = False
        current = bpy.context
        if current is None or _PLANT_LOCK or _busy(current) or not _plant_target_ready(current):
            return None
        _PLANT_LOCK = True
        try:
            _count, note = _run_plantings(current)
            _status(current, note)
        except PlacementError as exc:
            _status(current, str(exc))
        except Exception as exc:
            _status(current, f"Посадки: {exc}")
        finally:
            _PLANT_LOCK = False
        return None

    bpy.app.timers.register(_later, first_interval=0.2)


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
    bl_description = "Случайные деревья и кусты по выбранному полигону подложки"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return not _busy(context) and bpy.data.collections.get(UNDERLAY_NAME) is not None

    def execute(self, context):
        try:
            _count, note = _run_plantings(context)
        except PlacementError as exc:
            _status(context, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _status(context, note)
        self.report({"INFO"}, note)
        return {"FINISHED"}


class DWGREADER_OT_place_benches(Operator):
    bl_idname = "dwgreader.place_benches"
    bl_label = "Указать отрезок лавок"
    bl_description = "Два клика: начало и конец. Лавки встают вдоль отрезка с заданным шагом"
    bl_options = {"UNDO"}

    start: tuple[float, float] | None = None
    hover: tuple[float, float] | None = None

    @classmethod
    def poll(cls, context):
        return not _busy(context) and bpy.data.collections.get(UNDERLAY_NAME) is not None

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
        self.start = None
        self.hover = None
        self._area = context.area
        self._handle = bpy.types.SpaceView3D.draw_handler_add(_draw_segment, (self,), "WINDOW", "POST_VIEW")
        context.window_manager.modal_handler_add(self)
        _status(context, "Лавки: укажите начало отрезка")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "MOUSEMOVE":
            self.hover = _ground_point(context, event)
            if context.area is not None:
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            point = _ground_point(context, event)
            if point is None:
                return {"RUNNING_MODAL"}
            if self.start is None:
                self.start = point
                _status(context, "Лавки: укажите конец отрезка")
                return {"RUNNING_MODAL"}
            try:
                count = _place_benches(context, self.start, point)
            except PlacementError as exc:
                self._finish_draw()
                _status(context, str(exc))
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self._finish_draw()
            _status(context, f"Лавки: {count} шт., шаг {context.scene.dwgreader_bench_step:.1f} м")
            return {"FINISHED"}
        if event.type in {"RIGHTMOUSE", "ESC"}:
            self._finish_draw()
            _status(context, "Отрезок лавок отменён")
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


def _place_playground_on(
    context,
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    key: str,
) -> str:
    catalog = _assets(context, "playground")
    if not catalog:
        raise PlacementError("В каталоге нет площадок")
    flat = [(vertex[0], vertex[1]) for vertex in vertices]
    rectangle = minimum_rectangle(flat)
    if rectangle is None:
        raise PlacementError("У зоны нет габарита")
    zone_width, zone_depth, edge_angle, rect_center = rectangle
    fitting, excess = rank_playgrounds(zone_width, zone_depth, catalog)
    if not fitting:
        extra = 0.0 if excess is None else excess
        raise PlacementError(f"Нет модели, которая влезает. Ближайшая больше на {extra:.1f} м")
    center = polygon_centroid_xy(vertices, faces)
    if center is None:
        center = (rect_center[0], rect_center[1], 0.0)
    collection = _placement_collection("playgrounds")
    _replace_owned(collection, key)
    ids = [item[0] for item in fitting]
    rotations = [orientation_angle(edge_angle, item[1]) for item in fitting]
    asset = _asset(context, ids[0])
    source = _import_mesh(context, asset)
    obj = source.copy()
    obj.data = source.data
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_select = False
    obj.name = f"PLAY_{asset['id']}"
    obj.location = center
    obj.rotation_euler = (0.0, 0.0, rotations[0])
    obj["placement"] = "area"
    obj["family"] = "playground"
    obj["asset_id"] = asset["id"]
    obj["zone_key"] = key
    obj["fit_ids"] = ",".join(ids)
    obj["fit_rotations"] = ",".join(f"{angle:.6f}" for angle in rotations)
    obj["fit_index"] = 0
    collection.objects.link(obj)
    obj.hide_set(False)
    return f"Площадка: {asset['name']}, полигон {zone_width:.1f}×{zone_depth:.1f} м"


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
    root = bpy.data.collections.get(UNDERLAY_NAME)
    if root is None:
        return None
    best: tuple[float, bpy.types.Object, int] | None = None

    def walk(collection: bpy.types.Collection) -> None:
        nonlocal best
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

    walk(root)
    if best is None:
        return None
    return best[1], best[2]


class DWGREADER_OT_place_playground(Operator):
    bl_idname = "dwgreader.place_playground"
    bl_label = "Подобрать площадку"
    bl_description = "Ставит комплекс в центр выбранного полигона. Если в слое их несколько — кликните по нужному"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return not _busy(context) and bpy.data.collections.get(UNDERLAY_NAME) is not None

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
            _status(context, "Кликните по полигону подложки")
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
            raise PlacementError("Выберите полигон подложки")
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


def _replace_playground(context, obj: bpy.types.Object) -> None:
    ids = [item for item in str(obj.get("fit_ids") or "").split(",") if item]
    rotations = [float(item) for item in str(obj.get("fit_rotations") or "").split(",") if item]
    if len(ids) < 2 or len(rotations) != len(ids):
        raise PlacementError("Для этой зоны другой подходящей модели нет")
    index = (int(obj.get("fit_index") or 0) + 1) % len(ids)
    asset = _asset(context, ids[index])
    source = _import_mesh(context, asset)
    obj.data = source.data
    obj.rotation_euler = (0.0, 0.0, rotations[index])
    obj["asset_id"] = asset["id"]
    obj["fit_index"] = index
    obj.name = f"PLAY_{asset['id']}"
    _status(context, f"Площадка заменена: {asset['name']}")


def _replace_benches(context, parent: bpy.types.Object) -> None:
    family = list(_assets(context, "bench"))
    if len(family) < 2:
        raise PlacementError("В каталоге одна лавка, заменять нечем")
    current = str(parent.get("asset_id") or "")
    ids = [asset["id"] for asset in family]
    index = (ids.index(current) + 1) % len(ids) if current in ids else 0
    asset = family[index]
    source = _import_mesh(context, asset)
    children = [child for child in parent.children if child.type == "MESH"]
    if not children:
        raise PlacementError("У отрезка нет лавок")
    for child in children:
        child.data = source.data
    parent["asset_id"] = asset["id"]
    _status(context, f"Лавки заменены: {asset['name']}")


def _place_benches(context, start: tuple[float, float], end: tuple[float, float]) -> int:
    step = float(context.scene.dwgreader_bench_step)
    if step < MIN_BENCH_STEP_M:
        raise PlacementError("Шаг меньше 0,5 м")
    asset_id = str(context.scene.dwgreader_bench_asset or "")
    asset = _asset(context, asset_id)
    if asset["family"] != "bench":
        raise PlacementError("Для отрезка нужна модель из семейства лавок")
    points = bench_positions(start, end, step, asset["footprint"][1])
    if not points:
        raise PlacementError("Отрезок слишком короткий")
    source = _import_mesh(context, asset)
    collection = _placement_collection("benches")
    key = f"{round(start[0], 2)}:{round(start[1], 2)}:{round(end[0], 2)}:{round(end[1], 2)}"
    _replace_owned(collection, key)
    curve_data = bpy.data.curves.new(f"BENCH_LINE_{key}", "CURVE")
    curve_data.dimensions = "3D"
    curve_data.bevel_depth = 0.03
    spline = curve_data.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (start[0], start[1], 0.0, 1.0)
    spline.points[1].co = (end[0], end[1], 0.0, 1.0)
    parent = bpy.data.objects.new(f"BENCH_{asset['id']}", curve_data)
    parent["placement"] = "linear"
    parent["family"] = "bench"
    parent["asset_id"] = asset["id"]
    parent["zone_key"] = key
    parent["step"] = step
    collection.objects.link(parent)
    angle = forward_angle(end[0] - start[0], end[1] - start[1])
    for index, point in enumerate(points):
        inst = source.copy()
        inst.data = source.data
        inst.hide_viewport = False
        inst.hide_render = False
        inst.hide_select = False
        inst.name = f"BENCH_{asset['id']}_{index}"
        inst.parent = parent
        inst.location = (point[0], point[1], 0.0)
        inst.rotation_euler = (0.0, 0.0, angle)
        inst["asset_id"] = asset["id"]
        collection.objects.link(inst)
    return len(points)


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
    if operator.start is None or operator.hover is None:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batch = batch_for_shader(
            shader, "LINES",
            {"pos": [(operator.start[0], operator.start[1], 0.05), (operator.hover[0], operator.hover[1], 0.05)]},
        )
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
        ("benches", "Лавки", "Модели вдоль отрезка"),
        ("playgrounds", "Площадка", "Комплекс по габариту зоны"),
    )


def draw_panel(layout, context) -> None:
    prefs = _prefs(context)
    box = layout.box()
    box.label(text="4. Расстановка")
    if prefs is None:
        box.label(text="Нет настроек аддона")
        return
    box.prop(prefs, "library_dir", text="Библиотека")
    try:
        load_catalog(context)
    except PlacementError as exc:
        box.label(text=str(exc))
        return
    if bpy.data.collections.get(UNDERLAY_NAME) is None:
        box.label(text="Сначала соберите подложку")
        return
    global _PLANT_UI_SEEN
    _PLANT_UI_SEEN = True
    scene = context.scene
    try:
        box.prop(scene, "dwgreader_place_mode", expand=True)
        mode = scene.dwgreader_place_mode
        if mode == "plantings":
            box.label(text="Деревья и кусты встают на весь выбранный слой")
            box.prop(scene, "dwgreader_tree_model", text="Дерево")
            box.prop(scene, "dwgreader_bush_model", text="Куст")
            box.prop(scene, "dwgreader_tree_share")
            box.prop(scene, "dwgreader_density")
            box.prop(scene, "dwgreader_seed")
            box.prop(scene, "dwgreader_plant_scale")
            box.operator("dwgreader.place_plantings", text="Расставить", icon="OUTLINER_OB_CURVES")
        elif mode == "benches":
            box.prop(scene, "dwgreader_bench_asset", text="Модель")
            box.prop(scene, "dwgreader_bench_step")
            box.operator("dwgreader.place_benches", text="Указать отрезок", icon="CURVE_PATH")
            box.operator("dwgreader.replace_model", text="Заменить модель", icon="FILE_REFRESH")
        else:
            box.label(text="Кликните по полигону. Модель встанет в его центр")
            box.operator("dwgreader.place_playground", text="Подобрать", icon="MESH_PLANE")
            box.operator("dwgreader.replace_model", text="Заменить модель", icon="FILE_REFRESH")
    except Exception as exc:
        box.label(text=str(exc))


CLASSES = (
    DWGREADER_OT_place_plantings,
    DWGREADER_OT_place_benches,
    DWGREADER_OT_place_playground,
    DWGREADER_OT_replace_model,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dwgreader_place_mode = EnumProperty(
        name="Режим",
        items=_mode_items,
        default=0,
    )
    bpy.types.Scene.dwgreader_tree_share = FloatProperty(
        name="Доля деревьев, %",
        description="Остальное — кусты. Меняет уже выбранный слой подложки",
        default=30.0,
        min=0.0,
        max=100.0,
        subtype="PERCENTAGE",
        update=_on_plant_update,
    )
    bpy.types.Scene.dwgreader_tree_model = EnumProperty(
        name="Дерево",
        items=_tree_model_items,
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
        default=6.0,
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
        default=3.0,
        min=0.5,
        soft_max=30.0,
    )
    bpy.types.Scene.dwgreader_bench_asset = EnumProperty(
        name="Лавка",
        items=_bench_items,
    )


def unregister() -> None:
    for name in (
        "dwgreader_bench_asset",
        "dwgreader_bench_step",
        "dwgreader_plant_scale",
        "dwgreader_bush_model",
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
