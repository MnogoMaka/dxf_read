"""Жилой дом на геометрических нодах и выгрузка оценённой сетки в GLB.

Файл здания и текстуры лежат рядом с аддоном, в папке building. Отдельного
аддона нет: кнопки стоят в панели «Подложка».
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import tempfile
import zlib
from array import array
from pathlib import Path

import bpy
from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper
from mathutils import Euler, Matrix


# Recommended names first; the old names remain supported for compatibility.
COLOR_ATTRIBUTE_NAMES = ("glb_color", "export_color", "select_color")
FACTOR_ATTRIBUTE_NAMES = ("glb_factor", "export_factor", "factor")
SUPPORTED_OBJECT_TYPES = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
UNIFORM_TOLERANCE = 1.0e-5


def _rgba(value, fallback=(1.0, 1.0, 1.0, 1.0)):
    try:
        values = tuple(float(component) for component in value)
    except (TypeError, ValueError):
        return tuple(fallback)
    if len(values) == 3:
        return values + (1.0,)
    if len(values) >= 4:
        return values[:4]
    return tuple(fallback)


def _node_attribute_name(node):
    if node is None:
        return ""
    if node.type == "ATTRIBUTE":
        return getattr(node, "attribute_name", "")
    if node.type == "VERTEX_COLOR":
        return getattr(node, "layer_name", "")
    return ""


def _material_attribute_names(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return set()
    return {
        name
        for node in material.node_tree.nodes
        if (name := _node_attribute_name(node))
    }


def _first_matching_name(candidates, referenced_names, available_names):
    for name in candidates:
        if name in referenced_names and name in available_names:
            return name
    return None


def _first_referenced_name(candidates, referenced_names):
    return next((name for name in candidates if name in referenced_names), None)


def _find_principled(material):
    if material is None or not material.use_nodes or material.node_tree is None:
        return None
    return next(
        (node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"),
        None,
    )


def _find_upstream_image(input_socket, visited=None):
    if visited is None:
        visited = set()
    for link in input_socket.links:
        node = link.from_node
        if node in visited:
            continue
        visited.add(node)
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node
        for socket in node.inputs:
            image_node = _find_upstream_image(socket, visited)
            if image_node is not None:
                return image_node
    return None


def _clear_socket_links(node_tree, socket):
    for link in list(socket.links):
        node_tree.links.remove(link)


def _active_input(node, name):
    for socket in node.inputs:
        if socket.name != name:
            continue
        if getattr(socket, "is_unavailable", False):
            continue
        if not getattr(socket, "enabled", True):
            continue
        return socket
    return None


def _linked_node(socket):
    if socket is None or len(socket.links) != 1:
        return None
    return socket.links[0].from_node


def _constant_color_mix(base_color, color_attribute_name, factor_attribute_name):
    """Return (constant RGBA, attribute_is_B) for the supported Mix pattern."""
    mix_node = _linked_node(base_color)
    if mix_node is None or mix_node.type != "MIX":
        return None
    if getattr(mix_node, "data_type", "") not in {"RGBA", "COLOR"}:
        return None

    factor_socket = _active_input(mix_node, "Factor")
    input_a = _active_input(mix_node, "A")
    input_b = _active_input(mix_node, "B")
    if factor_socket is None or input_a is None or input_b is None:
        return None

    factor_node = _linked_node(factor_socket)
    if _node_attribute_name(factor_node) != factor_attribute_name:
        return None

    node_a = _linked_node(input_a)
    node_b = _linked_node(input_b)
    a_is_attribute = _node_attribute_name(node_a) == color_attribute_name
    b_is_attribute = _node_attribute_name(node_b) == color_attribute_name

    if b_is_attribute and node_a is None:
        return _rgba(input_a.default_value), True
    if a_is_attribute and node_b is None:
        return _rgba(input_b.default_value), False
    return None


def _mix_rgba(color_a, color_b, factor):
    inverse = 1.0 - factor
    return tuple(a * inverse + b * factor for a, b in zip(color_a, color_b))


def _set_solid_base_color(material, color):
    principled = _find_principled(material)
    if principled is None:
        raise RuntimeError(f'В материале "{material.name}" не найден Principled BSDF')
    base_color = principled.inputs.get("Base Color")
    if base_color is None:
        raise RuntimeError(f'В материале "{material.name}" нет входа Base Color')
    _clear_socket_links(material.node_tree, base_color)
    base_color.default_value = tuple(color)


def _used_material_slots(mesh):
    return {polygon.material_index for polygon in mesh.polygons}


def _indices_for_material(mesh, domain, slot_index):
    if domain == "FACE":
        for polygon in mesh.polygons:
            if polygon.material_index == slot_index:
                yield polygon.index
        return

    if domain == "CORNER":
        for polygon in mesh.polygons:
            if polygon.material_index != slot_index:
                continue
            yield from range(polygon.loop_start, polygon.loop_start + polygon.loop_total)
        return

    if domain == "POINT":
        seen = set()
        for polygon in mesh.polygons:
            if polygon.material_index != slot_index:
                continue
            for loop_index in range(polygon.loop_start, polygon.loop_start + polygon.loop_total):
                vertex_index = mesh.loops[loop_index].vertex_index
                if vertex_index not in seen:
                    seen.add(vertex_index)
                    yield vertex_index


def _uniform_value(values, tolerance=UNIFORM_TOLERANCE):
    iterator = iter(values)
    try:
        first = next(iterator)
    except StopIteration:
        return None, False

    if isinstance(first, tuple):
        for value in iterator:
            if any(abs(a - b) > tolerance for a, b in zip(first, value)):
                return first, False
    else:
        for value in iterator:
            if abs(first - value) > tolerance:
                return first, False
    return first, True


def _read_material_color(mesh, attribute_name, slot_index):
    attribute = mesh.color_attributes.get(attribute_name)
    if attribute is None:
        return None, False

    def values():
        for index in _indices_for_material(mesh, attribute.domain, slot_index):
            try:
                yield _rgba(attribute.data[index].color)
            except (AttributeError, IndexError):
                continue

    return _uniform_value(values())


def _read_material_factor(mesh, attribute_name, slot_index):
    attribute = mesh.attributes.get(attribute_name)
    if attribute is None:
        return None, False

    def values():
        for index in _indices_for_material(mesh, attribute.domain, slot_index):
            try:
                yield max(0.0, min(1.0, float(attribute.data[index].value)))
            except (AttributeError, TypeError, ValueError, IndexError):
                continue

    return _uniform_value(values())


def _linear_to_srgb(value):
    value = max(0.0, min(1.0, float(value)))
    if value <= 0.0031308:
        return 12.92 * value
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def _png_chunk(chunk_type, data):
    payload = chunk_type + data
    return (
        struct.pack(">I", len(data))
        + payload
        + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def _write_linear_rgba_to_srgb_png(filepath, pixels, width, height):
    """Encode scene-linear RGBA as an explicitly tagged 8-bit sRGB PNG."""
    try:
        import numpy as np

        linear = np.asarray(pixels, dtype=np.float32).reshape((height, width, 4))
        rgb = np.clip(linear[:, :, :3], 0.0, 1.0)
        srgb = np.where(
            rgb <= 0.0031308,
            rgb * 12.92,
            1.055 * np.power(rgb, 1.0 / 2.4) - 0.055,
        )
        encoded = np.empty((height, width, 4), dtype=np.uint8)
        encoded[:, :, :3] = np.rint(np.clip(srgb, 0.0, 1.0) * 255.0).astype(np.uint8)
        encoded[:, :, 3] = np.rint(
            np.clip(linear[:, :, 3], 0.0, 1.0) * 255.0
        ).astype(np.uint8)
        scanlines = b"".join(
            b"\x00" + encoded[row].tobytes()
            for row in range(height - 1, -1, -1)
        )
    except ImportError:
        scanlines = bytearray()
        row_stride = width * 4
        for row in range(height - 1, -1, -1):
            scanlines.append(0)
            row_start = row * row_stride
            for column in range(width):
                index = row_start + column * 4
                scanlines.extend(
                    (
                        round(_linear_to_srgb(pixels[index]) * 255.0),
                        round(_linear_to_srgb(pixels[index + 1]) * 255.0),
                        round(_linear_to_srgb(pixels[index + 2]) * 255.0),
                        round(max(0.0, min(1.0, pixels[index + 3])) * 255.0),
                    )
                )
        scanlines = bytes(scanlines)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png_data = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"sRGB", b"\x00")
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=6))
        + _png_chunk(b"IEND", b"")
    )
    with open(filepath, "wb") as png_file:
        png_file.write(png_data)


def _mix_image_pixels(source_image, color, factor, image_name, temp_directory):
    width, height = source_image.size[:]
    if width <= 0 or height <= 0:
        raise RuntimeError(f'Не удалось прочитать изображение "{source_image.name}"')

    pixel_count = width * height * 4
    source_pixels = array("f", [0.0]) * pixel_count
    source_image.pixels.foreach_get(source_pixels)

    try:
        import numpy as np

        pixels = np.frombuffer(source_pixels, dtype=np.float32).reshape((-1, 4)).copy()
        target = np.asarray(color, dtype=np.float32)
        pixels[:] = pixels * (1.0 - factor) + target * factor
        output_pixels = pixels.ravel()
    except ImportError:
        output_pixels = source_pixels
        inverse = 1.0 - factor
        for index in range(0, pixel_count, 4):
            output_pixels[index] = output_pixels[index] * inverse + color[0] * factor
            output_pixels[index + 1] = output_pixels[index + 1] * inverse + color[1] * factor
            output_pixels[index + 2] = output_pixels[index + 2] * inverse + color[2] * factor
            output_pixels[index + 3] = output_pixels[index + 3] * inverse + color[3] * factor

    image_path = os.path.join(temp_directory, f"{image_name}.png")
    _write_linear_rgba_to_srgb_png(
        image_path,
        output_pixels,
        width,
        height,
    )

    mixed_image = bpy.data.images.load(image_path, check_existing=False)
    mixed_image.name = image_name
    mixed_image.colorspace_settings.name = "sRGB"
    mixed_image.alpha_mode = "STRAIGHT"
    return mixed_image


def _prepare_mixed_material(
    material,
    color,
    factor,
    object_name,
    slot_index,
    temp_directory,
    temp_images,
):
    principled = _find_principled(material)
    if principled is None:
        raise RuntimeError(f'В материале "{material.name}" не найден Principled BSDF')
    base_color = principled.inputs.get("Base Color")
    if base_color is None:
        raise RuntimeError(f'В материале "{material.name}" нет входа Base Color')

    if factor >= 1.0 - UNIFORM_TOLERANCE:
        _set_solid_base_color(material, color)
        return

    image_node = _find_upstream_image(base_color)
    if image_node is None or image_node.image is None:
        raise RuntimeError(
            f'В материале "{material.name}" не найдена Image Texture перед Base Color'
        )

    if factor > UNIFORM_TOLERANCE:
        safe_object_name = "".join(
            character if character.isalnum() or character in "_-" else "_"
            for character in object_name
        )
        image_name = f"export_{safe_object_name}_{slot_index}_texture"
        mixed_image = _mix_image_pixels(
            image_node.image,
            color,
            factor,
            image_name,
            temp_directory,
        )
        temp_images.append(mixed_image)
        image_node.image = mixed_image

    _clear_socket_links(material.node_tree, base_color)
    material.node_tree.links.new(image_node.outputs["Color"], base_color)


def _prepare_material_slot(
    mesh,
    slot_index,
    source_material,
    object_name,
    temp_directory,
    temp_materials,
    temp_images,
    warnings,
):
    referenced = _material_attribute_names(source_material)
    available_colors = set(mesh.color_attributes.keys())
    available_attributes = set(mesh.attributes.keys())

    referenced_color_name = _first_referenced_name(COLOR_ATTRIBUTE_NAMES, referenced)
    referenced_factor_name = _first_referenced_name(FACTOR_ATTRIBUTE_NAMES, referenced)
    color_name = _first_matching_name(COLOR_ATTRIBUTE_NAMES, referenced, available_colors)
    factor_name = _first_matching_name(FACTOR_ATTRIBUTE_NAMES, referenced, available_attributes)

    if referenced_color_name is not None and color_name is None:
        warnings.append(
            f'{object_name} / {source_material.name}: материал ожидает атрибут '
            f'"{referenced_color_name}", но в Mesh его нет; материал оставлен без преобразования'
        )
        return source_material

    if referenced_factor_name is not None and factor_name is None:
        warnings.append(
            f'{object_name} / {source_material.name}: материал ожидает атрибут '
            f'"{referenced_factor_name}", но в Mesh его нет; материал оставлен без преобразования'
        )
        return source_material

    # Static or already glTF-compatible material: keep it unchanged.
    if color_name is None and factor_name is None:
        return source_material

    if color_name is None and factor_name is not None:
        warnings.append(
            f'{object_name} / {source_material.name}: найден фактор "{factor_name}", '
            "но не найден цветовой атрибут; материал оставлен без преобразования"
        )
        return source_material

    color, color_is_uniform = _read_material_color(mesh, color_name, slot_index)
    if color is None:
        warnings.append(
            f'{object_name} / {source_material.name}: атрибут "{color_name}" не содержит данных'
        )
        return source_material

    principled = _find_principled(source_material)
    if principled is None:
        warnings.append(
            f"{object_name} / {source_material.name}: нет Principled BSDF; материал оставлен без преобразования"
        )
        return source_material
    base_color = principled.inputs.get("Base Color")
    image_node = _find_upstream_image(base_color) if base_color else None

    if image_node is None and factor_name is not None:
        mix_pattern = _constant_color_mix(
            base_color,
            color_name,
            factor_name,
        )
        if mix_pattern is not None:
            factor, factor_is_uniform = _read_material_factor(mesh, factor_name, slot_index)
            if factor is None:
                raise RuntimeError(
                    f'{object_name} / {source_material.name}: атрибут "{factor_name}" не содержит данных'
                )
            if not color_is_uniform or not factor_is_uniform:
                raise RuntimeError(
                    f"{object_name} / {source_material.name}: цвет или фактор различается внутри одного материала. "
                    "Для преобразования в единый Base Color они должны быть постоянными"
                )

            constant_color, attribute_is_b = mix_pattern
            if attribute_is_b:
                final_color = _mix_rgba(constant_color, color, factor)
            else:
                final_color = _mix_rgba(color, constant_color, factor)

            export_material = source_material.copy()
            export_material.name = f"export_{object_name}_{slot_index}_{source_material.name}"
            temp_materials.append(export_material)
            _set_solid_base_color(export_material, final_color)
            return export_material

        warnings.append(
            f'{object_name} / {source_material.name}: атрибут "{factor_name}" найден, '
            "но не обнаружена поддерживаемая схема Mix Color; материал оставлен без преобразования"
        )
        return source_material

    if image_node is not None and factor_name is not None:
        factor, factor_is_uniform = _read_material_factor(mesh, factor_name, slot_index)
        if factor is None:
            raise RuntimeError(
                f'{object_name} / {source_material.name}: атрибут "{factor_name}" не содержит данных'
            )
        if not color_is_uniform or not factor_is_uniform:
            raise RuntimeError(
                f"{object_name} / {source_material.name}: цвет или фактор различается внутри одного материала. "
                "Для смешивания с текстурой они должны быть постоянными на всех его полигонах"
            )

        export_material = source_material.copy()
        export_material.name = f"export_{object_name}_{slot_index}_{source_material.name}"
        temp_materials.append(export_material)
        _prepare_mixed_material(
            export_material,
            color,
            factor,
            object_name,
            slot_index,
            temp_directory,
            temp_images,
        )
        return export_material

    if image_node is None:
        if not color_is_uniform:
            # Direct Color Attribute materials are valid vertex-color materials in glTF.
            return source_material
        export_material = source_material.copy()
        export_material.name = f"export_{object_name}_{slot_index}_{source_material.name}"
        temp_materials.append(export_material)
        _set_solid_base_color(export_material, color)
        return export_material

    # A texture and vertex color without glb_factor normally represents the
    # glTF-compatible Texture * COLOR_0 workflow. Leave it to the glTF exporter.
    return source_material


def _new_evaluated_export_object(source_object, depsgraph, collection):
    evaluated_object = source_object.evaluated_get(depsgraph)
    export_mesh = bpy.data.meshes.new_from_object(
        evaluated_object,
        preserve_all_data_layers=True,
        depsgraph=depsgraph,
    )
    if export_mesh is None:
        raise RuntimeError(f'Не удалось получить Mesh из объекта "{source_object.name}"')
    export_mesh.name = f"export_{source_object.name}_mesh"

    # Use the evaluated transform so parents, constraints and drivers are baked too.
    world_matrix = evaluated_object.matrix_world.copy()
    export_mesh.transform(world_matrix)
    if world_matrix.to_3x3().determinant() < 0.0:
        export_mesh.flip_normals()
    export_mesh.update()

    export_object = bpy.data.objects.new(f"export_{source_object.name}", export_mesh)
    collection.objects.link(export_object)
    export_object.matrix_world = Matrix.Identity(4)
    return export_object, export_mesh


def _gltf_export_kwargs(filepath):
    kwargs = {"filepath": filepath, "export_format": "GLB", "use_selection": True}
    properties = bpy.ops.export_scene.gltf.get_rna_type().properties
    if "export_apply" in properties:
        kwargs["export_apply"] = True
    if "export_vertex_color" in properties:
        kwargs["export_vertex_color"] = "MATERIAL"
    if "export_all_vertex_colors" in properties:
        kwargs["export_all_vertex_colors"] = False
    if "export_animations" in properties:
        kwargs["export_animations"] = False
    return kwargs


BUILDING_COLLECTION = "SITE_BUILDING"
BUILDING_BLEND = "Building 2.0.blend"
BUILDING_GROUP = "1_Building"
MODULES_COLLECTION = "modules"
DEFAULT_BUILDING_PRESET = "main002"
_SETTING_LABELS = {"Колчисетво этажей": "Количество этажей", "Тип модуля": "Номер формы"}
_SKIP_COLLECTIONS = ("SITE_BUILDING", "SITE_PLACEMENT", "SITE_LIBRARY")
_CATALOG = {"key": "", "drivers": []}
_NODE_ITEMS: list[tuple[str, str, str]] = [("none", "Нет ноды", "В файле здания нет ноды дома")]

BUILDING_PRESETS = (
    ('circle', 'Круглая башня', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 13,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 1,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.0999, 0.0999, 0.0999, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': True,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 15,
        'Socket_42': 0.0,
        'Socket_67': -9.77,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': False,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': False,
        'Input_7': 4,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 0.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 0.0,
    }),
    ('main001', 'Дом 1', {
        'Socket_69': 'Material 5',
        'Socket_50': True,
        'Socket_38': 5,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 1,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (1.0, 1.0, 1.0, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': True,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 4,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 1,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 0.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 0.0,
    }),
    ('main002', 'Дом 2', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 8,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.0999, 0.0999, 0.0999, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 3,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 1,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main003', 'Дом 3', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 9,
        'Socket_29': 0,
        'Socket_63': 1,
        'Socket_65': 1,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.0999, 0.0999, 0.0999, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 6.0,
        'Socket_58': 10.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 1.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 2,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 1.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 2,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 1.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 2,
        'Socket_59': 3,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 0.0,
    }),
    ('main003b', 'Дом 3 б', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 9,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.7991, 0.7991, 0.7991, 1.0),
        'Socket_76': True,
        'Socket_75': (0.0297, 0.3121, 1.0, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 10,
        'Socket_42': -1.0,
        'Socket_67': 0.0,
        'Socket_20': False,
        'Socket_48': 2,
        'Socket_57': 2,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': -1.0,
        'Socket_16': False,
        'Socket_47': 2,
        'Socket_56': 2,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': -1.0,
        'Socket_66': 0.0,
        'Socket_12': False,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 3,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 0.0,
    }),
    ('main004', 'Дом 4', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 11,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.0999, 0.0999, 0.0999, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 1,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 3,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 3,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 3,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main005', 'Дом 5', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 12,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 4,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.2382, 0.2382, 0.2382, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 3,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main006', 'Дом 6', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 13,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.0999, 0.0999, 0.0999, 1.0),
        'Socket_77': 0.0,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 3.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 1,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main007', 'Дом 7', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 10,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': False,
        'Socket_75': (0.0999, 0.0999, 0.0999, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 3,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main008', 'Дом 8', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 11,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.2135, 0.862, 0.0794, 1.0),
        'Socket_76': True,
        'Socket_75': (0.1004, 0.443, 1.0, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': -1.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 2,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main009', 'Дом 9', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 8,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.8, 0.8, 0.8, 1.0),
        'Socket_76': True,
        'Socket_75': (0.8752, 0.6234, 0.3662, 1.0),
        'Socket_77': 0.6,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 2,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 1.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 1.0,
        'Socket_66': 1.0,
        'Socket_12': True,
        'Socket_46': 1,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 2,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main010', 'Дом 10', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 10,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 2,
        'Socket_70': (0.2, 0.2, 0.2, 1.0),
        'Socket_76': True,
        'Socket_75': (0.5995, 0.5995, 0.5995, 1.0),
        'Socket_77': 1.0,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 3,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 3,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 3,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 3,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
    ('main011', 'Дом 11', {
        'Socket_69': 'Material 1',
        'Socket_50': False,
        'Socket_38': 12,
        'Socket_29': 0,
        'Socket_63': 0,
        'Socket_65': 3,
        'Socket_70': (0.9573, 0.6956, 0.2638, 1.0),
        'Socket_76': False,
        'Socket_75': (0.568, 0.568, 0.568, 1.0),
        'Socket_77': 0.5,
        'Socket_22': True,
        'Socket_35': 0,
        'Socket_36': 50,
        'Socket_3': False,
        'Socket_34': True,
        'Socket_62': 0.0,
        'Socket_58': 0.0,
        'Socket_32': True,
        'Socket_18': 5,
        'Socket_42': 0.0,
        'Socket_67': 0.0,
        'Socket_20': True,
        'Socket_48': 3,
        'Socket_57': 1,
        'Socket_27': True,
        'Socket_14': 5,
        'Socket_41': 0.0,
        'Socket_16': True,
        'Socket_47': 2,
        'Socket_56': 1,
        'Socket_25': True,
        'Input_7': 5,
        'Socket_39': 0.0,
        'Socket_66': 0.0,
        'Socket_12': True,
        'Socket_46': 2,
        'Socket_53': 1,
        'Socket_59': 0,
        'Socket_60': 5.0,
        'Socket_9': True,
        'Socket_64': 3,
        'Socket_40': 0.0,
        'Socket_4': True,
        'Socket_54': 0,
        'Socket_52': 5.0,
    }),
)



def _values_match(current, expected) -> bool:
    if isinstance(expected, str):
        return str(current) == expected
    if isinstance(expected, tuple):
        try:
            return all(abs(float(left) - right) < 1e-3 for left, right in zip(current, expected))
        except TypeError:
            return False
    if isinstance(expected, float):
        try:
            return abs(float(current) - expected) < 1e-3
        except TypeError:
            return False
    if isinstance(expected, bool):
        return bool(current) is expected
    return current == expected


def preset_label(obj) -> str:
    modifier = building_modifier(obj)
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if inputs is None:
        return "Тип дома"
    for _key, label, values in BUILDING_PRESETS:
        if all(
            _values_match(getattr(getattr(inputs, ident, None), "value", None), value)
            for ident, value in values.items()
        ):
            return label
    return "Свой тип"


def preset_fits_ids(socket_ids, needed) -> bool:
    """Пресет подходит только если у ноды есть все его сокеты.

    Иначе «Дом 2» записал бы общие Socket_38 и Socket_29 в чужой граф.
    """
    return set(needed).issubset({str(item) for item in socket_ids})


def _group_key(name: str) -> str:
    """«1_Building.001» после повторной загрузки — тот же граф."""
    text = str(name or "")
    head, dot, tail = text.rpartition(".")
    if dot and tail.isdigit():
        return head
    return text


def _same_group(left: str, right: str) -> bool:
    key = _group_key(left)
    return bool(key) and key == _group_key(right)


def _inside_modules(collections) -> bool:
    for name in collections or ():
        text = str(name)
        if text == MODULES_COLLECTION or text.startswith(f"{MODULES_COLLECTION}."):
            return True
    return False


def _prefer_main(drivers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    main = [item for item in drivers if _same_group(item[1], BUILDING_GROUP)]
    rest = [item for item in drivers if not _same_group(item[1], BUILDING_GROUP)]
    return main + rest


def driver_records(rows) -> list[tuple[str, str]]:
    """Объекты с нодой вне коллекции деталей. Первая заготовка каждой ноды."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for object_name, group_name, collections in rows:
        group = str(group_name or "")
        if not group or _inside_modules(collections):
            continue
        key = _group_key(group)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append((str(object_name), key))
    return _prefer_main(found)


def chosen_driver(drivers, selected: str):
    """Выбранная нода, иначе основной 1_Building, иначе первая в файле."""
    if not drivers:
        return None
    for item in drivers:
        if _same_group(item[1], selected):
            return item
    for item in drivers:
        if _same_group(item[1], BUILDING_GROUP):
            return item
    return drivers[0]


def apply_preset(obj, key: str) -> None:
    values = next(item[2] for item in BUILDING_PRESETS if item[0] == key)
    if not preset_fits_ids(_socket_ids(obj), values):
        return
    modifier = building_modifier(obj)
    if modifier is None:
        return
    inputs = modifier.properties.inputs
    for ident, value in values.items():
        socket = getattr(inputs, ident, None)
        if socket is None or not hasattr(socket, "value"):
            continue
        socket.value = value
    obj.update_tag()


def blend_path() -> Path:
    return Path(__file__).resolve().parent / "building" / BUILDING_BLEND


def _building_collection(scene):
    collection = bpy.data.collections.get(BUILDING_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(BUILDING_COLLECTION)
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    return collection


def _hide_module_collection(scene, collection) -> None:
    """Прячет заготовки глазом. Монитор коллекции и исключение ноду выключают."""
    collection.hide_viewport = False
    collection.hide_render = True

    def walk(layer) -> None:
        if layer.collection == collection:
            layer.exclude = False
            layer.hide_viewport = True
            return
        for child in layer.children:
            walk(child)

    for view_layer in scene.view_layers:
        walk(view_layer.layer_collection)


def _held_by_warehouse(parent) -> bool:
    name = str(getattr(parent, "name", "") or "")
    return _inside_modules([name]) or name == "SITE_LIBRARY" or name.startswith("SITE_LIBRARY.")


def _detach_warehouse(collection) -> None:
    """Склад деталей остаётся в файле для ноды и не входит в сцену, поэтому не попадает в GLB."""
    if collection is None:
        return
    scene = bpy.context.scene
    master = getattr(scene, "collection", None) if scene is not None else None
    if master is not None and collection.name in master.children:
        master.children.unlink(collection)
    for parent in list(bpy.data.collections):
        if parent == collection or _held_by_warehouse(parent):
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


def _has_local_modules() -> bool:
    for collection in bpy.data.collections:
        if getattr(collection, "library", None) is not None:
            continue
        try:
            name = collection.name
        except ReferenceError:
            continue
        if _inside_modules([name]):
            return True
    return False


def _release_modules(scene, root) -> None:
    """Прячет уже лежащие в сцене заготовки и вынимает их вместе с библиотекой моделей."""
    del root
    for collection in list(bpy.data.collections):
        if getattr(collection, "library", None) is not None:
            continue
        try:
            name = collection.name
        except ReferenceError:
            continue
        if _inside_modules([name]):
            _hide_module_collection(scene, collection)
            _detach_warehouse(collection)
        elif name == "SITE_LIBRARY" or name.startswith("SITE_LIBRARY."):
            _detach_warehouse(collection)


def _in_building_collection(obj) -> bool:
    for collection in getattr(obj, "users_collection", ()) or ():
        if getattr(collection, "name", "") == BUILDING_COLLECTION:
            return True
    return False


def building_modifier(obj):
    """Дом — объект в SITE_BUILDING с любой группой Geometry Nodes."""
    if obj is None:
        return None
    modifiers = list(getattr(obj, "modifiers", []) or [])
    try:
        marked = bool(obj.get("dwgreader_building"))
    except ReferenceError:
        return None
    if not marked and not _in_building_collection(obj):
        return None
    for modifier in modifiers:
        group = getattr(modifier, "node_group", None)
        if getattr(modifier, "type", "") == "NODES" and group is not None:
            return modifier
    return None


def _socket_ids(obj) -> set[str]:
    modifier = building_modifier(obj)
    group = getattr(modifier, "node_group", None)
    if group is None:
        return set()
    found = set()
    for item in group.interface.items_tree:
        if getattr(item, "in_out", "") != "INPUT":
            continue
        if getattr(item, "socket_type", "") == "NodeSocketGeometry":
            continue
        found.add(str(item.identifier))
    return found


def _preset_fits(obj, key: str = DEFAULT_BUILDING_PRESET) -> bool:
    values = next(item[2] for item in BUILDING_PRESETS if item[0] == key)
    return preset_fits_ids(_socket_ids(obj), values)


def _group_of(obj) -> str:
    modifier = building_modifier(obj)
    group = getattr(getattr(modifier, "node_group", None), "name", "")
    if group:
        return _group_key(group)
    try:
        return str(obj.get("dwgreader_building_group") or "")
    except ReferenceError:
        return ""


def buildings_in(scene) -> list:
    collection = bpy.data.collections.get(BUILDING_COLLECTION)
    if collection is None:
        return []
    return [obj for obj in collection.objects if building_modifier(obj) is not None]


def chosen_building(context):
    """Выделенный дом. Если выделен не дом, панель открывает последний поставленный."""
    active = context.view_layer.objects.active if context.view_layer is not None else None
    if active is not None and building_modifier(active) is not None:
        return active
    found = buildings_in(context.scene)
    if not found:
        return None
    return found[-1]


def draw_controls(layout, context) -> None:
    found = buildings_in(context.scene)
    if not found:
        return
    obj = chosen_building(context)
    if obj is None:
        layout.label(text="Выделите один дом: у каждого свои этажи и фасад", icon="INFO")
        return
    modifier = building_modifier(obj)
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if inputs is None:
        return
    box = layout.box()
    group = _group_of(obj)
    title = obj.name if not group else f"{obj.name} · {group}"
    box.label(text=title, icon="HOME")
    if _preset_fits(obj):
        box.operator_menu_enum("dwgreader.building_preset", "kind", text=preset_label(obj))
        hint = box.column(align=True)
        hint.scale_y = 0.75
        hint.label(text="Список меняет форму целиком. Новый дом — «Дом 2».")
        hint.label(text="Номер формы сам по себе тип не сменяет.")
    if len(found) > 1:
        note = box.column(align=True)
        note.scale_y = 0.75
        active = context.view_layer.objects.active if context.view_layer is not None else None
        if active is obj:
            note.label(text="Меняется только выделенный дом.")
        else:
            note.label(text="Выделите дом, чтобы менять именно его.")
    column = box.column(align=True)
    titled = False
    for item in modifier.node_group.interface.items_tree:
        if getattr(item, "item_type", "") == "PANEL":
            column.separator()
            column.label(text=item.name)
            titled = True
            continue
        if getattr(item, "in_out", "") != "INPUT":
            continue
        if getattr(item, "socket_type", "") == "NodeSocketGeometry":
            continue
        socket = getattr(inputs, item.identifier, None)
        if socket is None or not hasattr(socket, "value"):
            continue
        if not titled:
            column.label(text="Дом")
            titled = True
        column.prop(socket, "value", text=_SETTING_LABELS.get(item.name, item.name))


def _cursor(scene) -> tuple[float, float, float]:
    location = scene.cursor.location
    return (float(location.x), float(location.y), float(location.z))


def _blocked_collection(obj) -> bool:
    for collection in obj.users_collection:
        if collection.name.startswith(_SKIP_COLLECTIONS):
            return True
    return False


def _mesh_center(obj, edit: bool):
    from .placement_math import polygon_centroid_xy

    if edit and obj.mode == "EDIT":
        import bmesh

        mesh = bmesh.from_edit_mesh(obj.data)
        faces = [face for face in mesh.faces if face.select]
        if not faces:
            return None
        world = obj.matrix_world
        vertices: list[tuple[float, float, float]] = []
        index_of: dict[int, int] = {}
        polygons: list[tuple[int, ...]] = []
        for face in faces:
            polygon = []
            for vertex in face.verts:
                if vertex.index not in index_of:
                    index_of[vertex.index] = len(vertices)
                    point = world @ vertex.co
                    vertices.append((float(point.x), float(point.y), float(point.z)))
                polygon.append(index_of[vertex.index])
            polygons.append(tuple(polygon))
        return polygon_centroid_xy(vertices, polygons)
    if obj.data is None or not obj.data.polygons:
        return None
    world = obj.matrix_world
    vertices = [tuple(world @ vertex.co) for vertex in obj.data.vertices]
    polygons = [tuple(int(index) for index in polygon.vertices) for polygon in obj.data.polygons]
    return polygon_centroid_xy(vertices, polygons)


def _placement_spot(context) -> tuple[float, float, float]:
    """Центр выделенного полигона. Без полигона остаётся 3D-курсор."""
    active = context.view_layer.objects.active if context.view_layer is not None else None
    ordered = []
    if active is not None:
        ordered.append(active)
    for obj in list(getattr(context, "selected_objects", []) or []):
        if obj not in ordered:
            ordered.append(obj)
    edit = getattr(context, "mode", "") == "EDIT_MESH"
    for obj in ordered:
        if obj.type != "MESH" or building_modifier(obj) is not None or _blocked_collection(obj):
            continue
        center = _mesh_center(obj, edit and obj == active)
        if center is not None:
            return (float(center[0]), float(center[1]), float(center[2]))
    return _cursor(context.scene)


def _mesh_faces(obj, edit: bool):
    """Мировые вершины и грани выделенного полигона. В режиме объекта — весь меш."""
    if edit and obj.mode == "EDIT":
        import bmesh

        mesh = bmesh.from_edit_mesh(obj.data)
        faces = [face for face in mesh.faces if face.select]
        if not faces:
            return [], []
        world = obj.matrix_world
        vertices: list[tuple[float, float, float]] = []
        index_of: dict[int, int] = {}
        polygons: list[tuple[int, ...]] = []
        for face in faces:
            polygon = []
            for vertex in face.verts:
                if vertex.index not in index_of:
                    index_of[vertex.index] = len(vertices)
                    point = world @ vertex.co
                    vertices.append((float(point.x), float(point.y), float(point.z)))
                polygon.append(index_of[vertex.index])
            polygons.append(tuple(polygon))
        return vertices, polygons
    if obj.data is None or not obj.data.polygons:
        return [], []
    world = obj.matrix_world
    vertices = [tuple(float(point) for point in (world @ vertex.co)) for vertex in obj.data.vertices]
    polygons = [tuple(int(index) for index in polygon.vertices) for polygon in obj.data.polygons]
    return vertices, polygons


def _islands(vertices, faces):
    from .placement_math import face_groups

    islands = []
    for group in face_groups(faces):
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
        if len(island_vertices) >= 3 and island_faces:
            islands.append((island_vertices, island_faces))
    return islands


def _selected_islands(context):
    active = context.view_layer.objects.active if context.view_layer is not None else None
    ordered = []
    if active is not None:
        ordered.append(active)
    for obj in list(getattr(context, "selected_objects", []) or []):
        if obj not in ordered:
            ordered.append(obj)
    edit = getattr(context, "mode", "") == "EDIT_MESH"
    found = []
    for obj in ordered:
        if obj.type != "MESH" or building_modifier(obj) is not None or _blocked_collection(obj):
            continue
        vertices, faces = _mesh_faces(obj, edit and obj == active)
        found.extend(_islands(vertices, faces))
    return found


def _mark_building(obj, group_name: str = ""):
    obj["dwgreader_building"] = True
    group = _group_key(group_name) or _group_of(obj)
    if group:
        obj["dwgreader_building_group"] = group
    _unlock_transform(obj)
    return obj


def _unlock_transform(obj) -> None:
    """Дом можно сдвигать, крутить и масштабировать. Резак контура больше не привязан."""
    obj.hide_select = False
    for index in range(3):
        obj.lock_location[index] = False
        obj.lock_rotation[index] = False
        obj.lock_scale[index] = False


def _copy_building(root, source):
    copy = source.copy()
    copy.data = source.data
    copy.name = "Здание"
    root.objects.link(copy)
    _mark_building(copy, _group_of(source))
    _drop_clip(copy)
    return copy


def _file_stamp(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _library_name(obj, known: set[str]) -> str:
    name = obj.name
    if name in known:
        return name
    head, dot, tail = name.rpartition(".")
    if dot and tail.isdigit() and head in known:
        return head
    return name


def _node_group_name(obj) -> str:
    for modifier in getattr(obj, "modifiers", []) or []:
        group = getattr(modifier, "node_group", None)
        if getattr(modifier, "type", "") == "NODES" and group is not None:
            return _group_key(group.name)
    return ""


def _collect_pointers(collection, found: set[int]) -> None:
    for obj in collection.objects:
        found.add(obj.as_pointer())
    for child in collection.children:
        _collect_pointers(child, found)


def _driver_rows(collection, hidden: set[int], known: set[str], rows: list) -> None:
    for obj in collection.objects:
        if obj.as_pointer() in hidden:
            continue
        rows.append((_library_name(obj, known), _node_group_name(obj), []))
    for child in collection.children:
        if _inside_modules([child.name]):
            continue
        _driver_rows(child, hidden, known, rows)


def _library_blocks(library) -> list:
    blocks = []
    for name in dir(bpy.data):
        if name.startswith("_"):
            continue
        collection = getattr(bpy.data, name, None)
        if not callable(getattr(collection, "remove", None)):
            continue
        try:
            found = [block for block in collection if getattr(block, "library", None) == library]
        except TypeError:
            continue
        blocks.extend(found)
    return blocks


def _discard_library(library) -> None:
    """Убирает временную связь. У этого файла счётчик библиотеки залипает на 1."""
    blocks = _library_blocks(library)
    if blocks:
        bpy.data.batch_remove(blocks)
    if getattr(library, "users", 0):
        library.user_clear()
    if getattr(library, "users", 1) == 0:
        bpy.data.libraries.remove(library)


def _scan_drivers(path: Path) -> list[tuple[str, str]]:
    """Читает ноды-дома и убирает временную связь с файлом."""
    before = {lib.as_pointer() for lib in bpy.data.libraries}
    known: set[str] = set()
    with bpy.data.libraries.load(str(path), link=True) as (data_from, data_to):
        known = set(data_from.objects)
        data_to.scenes = list(data_from.scenes)
    try:
        rows = []
        for scene in data_to.scenes:
            if scene is None:
                continue
            hidden: set[int] = set()
            for child in scene.collection.children:
                if _inside_modules([child.name]):
                    _collect_pointers(child, hidden)
            _driver_rows(scene.collection, hidden, known, rows)
        return driver_records(rows)
    finally:
        for library in list(bpy.data.libraries):
            if library.as_pointer() not in before:
                _discard_library(library)


def node_catalog(path: Path | None = None) -> list[tuple[str, str]]:
    """Список (объект, нода) на размер и время файла здания."""
    source = Path(path) if path is not None else blend_path()
    if not source.is_file():
        return []
    key = f"{source.resolve()}|{_file_stamp(source)}"
    if _CATALOG["key"] == key:
        return list(_CATALOG["drivers"])
    drivers = _scan_drivers(source)
    _CATALOG["key"] = key
    _CATALOG["drivers"] = list(drivers)
    return list(drivers)


def _find_node_group(group_name: str):
    matches = []
    for group in bpy.data.node_groups:
        if getattr(group, "bl_idname", "") != "GeometryNodeTree":
            continue
        if _same_group(getattr(group, "name", ""), group_name):
            matches.append(group)
    if not matches:
        return None
    key = _group_key(group_name)
    for group in matches:
        if group.name == key:
            return group
    return matches[0]


def _reset_node_inputs(modifier) -> None:
    """Старые значения сокетов не переносятся: у новой ноды свои числа по умолчанию."""
    group = getattr(modifier, "node_group", None)
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if group is None or inputs is None:
        return
    for item in group.interface.items_tree:
        if getattr(item, "in_out", "") != "INPUT":
            continue
        if getattr(item, "socket_type", "") == "NodeSocketGeometry":
            continue
        socket = getattr(inputs, item.identifier, None)
        if socket is None or not hasattr(socket, "value"):
            continue
        default = getattr(item, "default_value", None)
        if default is None:
            continue
        try:
            socket.value = default
        except (TypeError, ValueError, AttributeError):
            try:
                socket.value = tuple(default)
            except (TypeError, ValueError):
                continue


def _drop_temp_driver(obj) -> None:
    mesh = obj.data if getattr(obj, "type", "") == "MESH" else None
    for collection in list(getattr(obj, "users_collection", ()) or ()):
        collection.objects.unlink(obj)
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and getattr(mesh, "users", 1) == 0 and isinstance(mesh, bpy.types.Mesh):
        bpy.data.meshes.remove(mesh)


def _append_group_source(scene, root, group_name: str):
    """Догружает граф из файла здания и не оставляет вторую заготовку в сцене."""
    path = blend_path()
    if not path.is_file():
        raise FileNotFoundError(path)
    driver = chosen_driver(node_catalog(path), group_name)
    if driver is None:
        raise RuntimeError("В файле здания нет такой ноды")
    object_name, _group = driver
    need_modules = not _has_local_modules()
    before = {collection.as_pointer() for collection in bpy.data.collections}
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        if object_name not in set(data_from.objects):
            raise RuntimeError("В файле здания нет объекта дома")
        data_to.objects = [object_name]
        data_to.collections = [MODULES_COLLECTION] if need_modules and MODULES_COLLECTION in set(data_from.collections) else []
    created = [obj for obj in data_to.objects if obj is not None]
    if not created:
        raise RuntimeError("В файле здания нет объекта дома")
    temp = created[0]
    group = None
    for modifier in getattr(temp, "modifiers", []) or []:
        candidate = getattr(modifier, "node_group", None)
        if getattr(modifier, "type", "") == "NODES" and candidate is not None and _same_group(candidate.name, group_name):
            group = candidate
            break
    for collection in data_to.collections:
        if collection is None:
            continue
        _hide_module_collection(scene, collection)
        _detach_warehouse(collection)
    _link_new_modules(scene, root, before)
    if group is None:
        _drop_temp_driver(temp)
        raise RuntimeError("В заготовке нет ноды дома")
    root["dwgreader_building_blend"] = _file_stamp(path)
    return group, temp


def _retarget_building(context, obj, group_name: str) -> None:
    """Ставит выбранную ноду на уже стоящий дом. Контур и место не меняются."""
    if obj is None or _same_group(_group_of(obj), group_name):
        return
    modifier = building_modifier(obj)
    if modifier is None:
        raise RuntimeError("У дома нет ноды")
    scene = context.scene
    root = _building_collection(scene)
    _release_modules(scene, root)
    group = _find_node_group(group_name)
    temp = None
    if group is None:
        group, temp = _append_group_source(scene, root, group_name)
    modifier.node_group = group
    _reset_node_inputs(modifier)
    obj["dwgreader_building_group"] = _group_key(group.name)
    _drop_clip(obj)
    if temp is not None:
        _drop_temp_driver(temp)
    obj.update_tag()


def node_enum_items(self, context):
    try:
        drivers = node_catalog()
    except Exception:
        logging.warning("Не удалось прочитать ноды здания", exc_info=True)
        drivers = []
    items = [(group, group, object_name) for object_name, group in drivers]
    if not items:
        items = [("none", "Нет ноды", "В файле здания нет ноды дома")]
    _NODE_ITEMS[:] = items
    return _NODE_ITEMS


def _link_new_modules(scene, root, before: set[int]) -> None:
    fresh = [
        collection for collection in bpy.data.collections
        if collection.as_pointer() not in before and _inside_modules([collection.name])
    ]
    nested: set[int] = set()
    for collection in fresh:
        for child in collection.children_recursive:
            nested.add(child.as_pointer())
    for collection in fresh:
        if collection.as_pointer() in nested:
            continue
        _hide_module_collection(scene, collection)
        _detach_warehouse(collection)


def _append_driver(scene, root, path: Path, object_name: str):
    need_modules = not _has_local_modules()
    before = {collection.as_pointer() for collection in bpy.data.collections}
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        names = set(data_from.objects)
        if object_name not in names:
            raise RuntimeError("В файле здания нет объекта дома")
        data_to.objects = [object_name]
        data_to.collections = [MODULES_COLLECTION] if need_modules and MODULES_COLLECTION in set(data_from.collections) else []
    created = [obj for obj in data_to.objects if obj is not None]
    if not created:
        raise RuntimeError("В файле здания нет объекта дома")
    obj = created[0]
    group_name = _node_group_name(obj)
    obj.name = "Здание"
    root.objects.link(obj)
    _mark_building(obj, group_name)
    for collection in data_to.collections:
        if collection is None:
            continue
        _hide_module_collection(scene, collection)
        _detach_warehouse(collection)
    _link_new_modules(scene, root, before)
    _drop_clip(obj)
    return obj


def _spawn_building(context, template=None):
    scene = context.scene
    root = _building_collection(scene)
    _release_modules(scene, root)
    path = blend_path()
    if not path.is_file():
        raise FileNotFoundError(path)
    stamp = _file_stamp(path)
    driver = chosen_driver(node_catalog(path), str(getattr(scene, "dwgreader_building_node", "") or ""))
    if driver is None:
        raise RuntimeError("В файле здания нет ноды дома")
    object_name, group_name = driver
    same_file = str(root.get("dwgreader_building_blend") or "") == stamp
    if same_file and template is not None and _same_group(_group_of(template), group_name):
        return _copy_building(root, template)
    if same_file:
        pool = [
            obj for obj in root.objects
            if building_modifier(obj) is not None and _same_group(_group_of(obj), group_name)
        ]
        if pool:
            source = chosen_building(context)
            if source not in pool:
                source = pool[-1]
            return _copy_building(root, source)
    obj = _append_driver(scene, root, path, object_name)
    root["dwgreader_building_blend"] = stamp
    return obj


def _use_house_preset(obj) -> None:
    """Круглая башня основного нода сменяется на «Дом 2». Чужая нода не переписывается."""
    if not _preset_fits(obj):
        return
    if preset_label(obj) == "Круглая башня":
        apply_preset(obj, DEFAULT_BUILDING_PRESET)


def _boundary_loops(faces: list[tuple[int, ...]]) -> list[list[int]]:
    """Внешние рёбра полигона, по одному замкнутому контуру."""
    counts: dict[tuple[int, int], int] = {}
    for face in faces:
        count = len(face)
        for index in range(count):
            a = face[index]
            b = face[(index + 1) % count]
            key = (a, b) if a < b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    neighbors: dict[int, list[int]] = {}
    unused: set[tuple[int, int]] = set()
    for (a, b), count in counts.items():
        if count != 1:
            continue
        neighbors.setdefault(a, []).append(b)
        neighbors.setdefault(b, []).append(a)
        unused.add((a, b))
    loops: list[list[int]] = []
    while unused:
        a, b = unused.pop()
        loop = [a, b]
        previous, current = a, b
        while True:
            nxt = next((item for item in neighbors.get(current, []) if item != previous), None)
            if nxt is None:
                break
            key = (current, nxt) if current < nxt else (nxt, current)
            if key not in unused:
                break
            if nxt == loop[0]:
                unused.discard(key)
                break
            unused.discard(key)
            loop.append(nxt)
            previous, current = current, nxt
            if len(loop) > len(counts) + 2:
                break
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _ring_area(points: list[tuple[float, float, float]]) -> float:
    area = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index][0], points[index][1]
        x2, y2 = points[(index + 1) % count][0], points[(index + 1) % count][1]
        area += x1 * y2 - x2 * y1
    return area * 0.5


def _oriented_loops(vertices, faces, origin) -> list[list[tuple[float, float, float]]]:
    """Внешний контур против часовой, дыры по часовой. Координаты в метрах от центроида."""
    cx, cy = float(origin[0]), float(origin[1])
    rings: list[list[tuple[float, float, float]]] = []
    for loop in _boundary_loops(faces):
        points = []
        for vertex_index in loop:
            x, y, _z = vertices[vertex_index]
            points.append((float(x) - cx, float(y) - cy, 0.0))
        if abs(_ring_area(points)) > 1e-6:
            rings.append(points)
    if not rings:
        return []
    rings.sort(key=lambda points: abs(_ring_area(points)), reverse=True)
    outer = rings[0]
    if _ring_area(outer) < 0:
        outer = list(reversed(outer))
    holes = []
    for hole in rings[1:]:
        if _ring_area(hole) > 0:
            hole = list(reversed(hole))
        holes.append(hole)
    return [outer, *holes]


def _almost_same(a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 <= 1e-8


def _on_straight_wall(a, b, c) -> bool:
    """Точка лежит на отрезке между соседями. Угол дома, даже маленький, остаётся."""
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cx, cy = float(c[0]), float(c[1])
    acx, acy = cx - ax, cy - ay
    length = (acx * acx + acy * acy) ** 0.5
    if length <= 1e-4:
        return True
    cross = (bx - ax) * acy - (by - ay) * acx
    if abs(cross) / length > 1e-3:
        return False
    dot = (bx - ax) * acx + (by - ay) * acy
    return -1e-4 <= dot <= length * length + 1e-4


def _clean_ring(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Повторы и точки на прямой стене. Скругление угла в дугу не делается."""
    ring = [(float(point[0]), float(point[1]), 0.0) for point in points]
    if len(ring) > 1 and _almost_same(ring[0], ring[-1]):
        ring = ring[:-1]
    unique: list[tuple[float, float, float]] = []
    for point in ring:
        if unique and _almost_same(unique[-1], point):
            continue
        unique.append(point)
    if len(unique) > 1 and _almost_same(unique[0], unique[-1]):
        unique.pop()
    guard = 0
    changed = True
    while changed and len(unique) >= 3 and guard < len(points) + 4:
        guard += 1
        changed = False
        count = len(unique)
        kept: list[tuple[float, float, float]] = []
        for index in range(count):
            if _on_straight_wall(unique[(index - 1) % count], unique[index], unique[(index + 1) % count]):
                changed = True
                continue
            kept.append(unique[index])
        unique = kept
    if len(unique) < 3 or abs(_ring_area(unique)) <= 1e-6:
        return []
    return unique


def _largest_polygon(geometry):
    kind = getattr(geometry, "geom_type", "")
    if kind == "Polygon":
        return geometry
    parts = getattr(geometry, "geoms", None)
    if not parts:
        return None
    found = []
    for item in parts:
        polygon = _largest_polygon(item)
        if polygon is not None and not polygon.is_empty:
            found.append(polygon)
    if not found:
        return None
    return max(found, key=lambda item: item.area)


def _rings_from_polygon(polygon) -> list[list[tuple[float, float, float]]]:
    outer = _clean_ring([(float(x), float(y), 0.0) for x, y in list(polygon.exterior.coords)[:-1]])
    if len(outer) < 3:
        return []
    if _ring_area(outer) < 0:
        outer = list(reversed(outer))
    holes = []
    for interior in polygon.interiors:
        hole = _clean_ring([(float(x), float(y), 0.0) for x, y in list(interior.coords)[:-1]])
        if len(hole) < 3:
            continue
        if _ring_area(hole) > 0:
            hole = list(reversed(hole))
        holes.append(hole)
    return [outer, *holes]


def _repair_rings(loops: list[list[tuple[float, float, float]]]) -> list[list[tuple[float, float, float]]]:
    """Самопересечения чинит shapely, если библиотека уже стоит. Иначе контур остаётся как есть."""
    if not loops:
        return []
    try:
        import sys

        vendor = Path(__file__).resolve().parent / "vendor"
        folder = str(vendor)
        if vendor.is_dir() and folder not in sys.path:
            sys.path.insert(0, folder)
        from shapely.geometry import Polygon
        from shapely.validation import make_valid
    except Exception:
        return loops
    try:
        outer = [(point[0], point[1]) for point in loops[0]]
        holes = [[(point[0], point[1]) for point in hole] for hole in loops[1:]]
        polygon = Polygon(outer, holes)
        if polygon.is_empty or polygon.is_valid:
            return loops
        polygon = _largest_polygon(make_valid(polygon))
        if polygon is None or polygon.is_empty:
            return loops
        return _rings_from_polygon(polygon) or loops
    except Exception:
        return loops


def _clean_loops(loops: list[list[tuple[float, float, float]]]) -> list[list[tuple[float, float, float]]]:
    """Внешний контур против часовой, дыры по часовой. Углы дома не скругляются."""
    cleaned: list[list[tuple[float, float, float]]] = []
    for ring in loops:
        ring = _clean_ring(list(ring))
        if len(ring) >= 3:
            cleaned.append(ring)
    if not cleaned:
        return []
    outer = cleaned[0]
    if _ring_area(outer) < 0:
        outer = list(reversed(outer))
    holes = []
    for hole in cleaned[1:]:
        if _ring_area(hole) > 0:
            hole = list(reversed(hole))
        holes.append(hole)
    return _repair_rings([outer, *holes])


def _footprint_mesh(loops: list[list[tuple[float, float, float]]]):
    """Грань внешнего контура и рёбра дыр. Диагоналей нет, нода читает сам контур."""
    verts: list[tuple[float, float, float]] = [point for loop in loops for point in loop]
    edges: list[tuple[int, int]] = []
    start = len(loops[0])
    for loop in loops[1:]:
        count = len(loop)
        for index in range(count):
            edges.append((start + index, start + (index + 1) % count))
        start += count
    mesh = bpy.data.meshes.new("Здание.след")
    mesh.from_pydata(verts, edges, [tuple(range(len(loops[0])))])
    mesh.update()
    return mesh


def _drop_clip(obj) -> None:
    for modifier in list(obj.modifiers):
        if modifier.type == "BOOLEAN" and modifier.name == "Полигон":
            obj.modifiers.remove(modifier)
    for child in list(obj.children):
        if not child.name.startswith("Здание.контур"):
            continue
        mesh = child.data
        bpy.data.objects.remove(child, do_unlink=True)
        if mesh is not None and getattr(mesh, "users", 1) == 0 and isinstance(mesh, bpy.types.Mesh):
            bpy.data.meshes.remove(mesh)


def _apply_polygon(obj, vertices, faces, origin, height: float) -> None:
    """След дома — очищенный контур в метрах. Масштаб X и Y равен 1, высота не меняется."""
    raw = _oriented_loops(vertices, faces, origin)
    loops = _clean_loops(raw) or raw
    cx, cy = float(origin[0]), float(origin[1])
    if loops:
        mesh = _footprint_mesh(loops)
        previous = obj.data
        obj.data = mesh
        if previous is not None and previous.users == 0:
            bpy.data.meshes.remove(previous)
    kept_z = float(obj.scale.z)
    obj.location = (cx, cy, float(height))
    obj.rotation_euler = Euler((obj.rotation_euler.x, obj.rotation_euler.y, 0.0), "XYZ")
    obj.scale = (1.0, 1.0, kept_z)
    _unlock_transform(obj)
    _drop_clip(obj)


def place_building(context):
    """Дом повторяет контур выделенного полигона. Без полигона — один дом в 3D-курсор."""
    islands = _selected_islands(context)
    if not islands:
        obj = _spawn_building(context)
        _use_house_preset(obj)
        obj.location = _cursor(context.scene)
        return [obj]
    return _place_islands(context, islands)


def _place_islands(context, islands, template=None, auto: bool = False):
    from .placement_math import polygon_centroid_xy

    made = []
    for vertices, faces in islands:
        centroid = polygon_centroid_xy(vertices, faces)
        if centroid is None:
            origin = (vertices[0][0], vertices[0][1])
            height = float(vertices[0][2])
        else:
            origin = (float(centroid[0]), float(centroid[1]))
            height = float(centroid[2])
        obj = _spawn_building(context, template)
        if auto:
            apply_preset(obj, DEFAULT_BUILDING_PRESET)
        else:
            _use_house_preset(obj)
        template = obj
        _apply_polygon(obj, vertices, faces, origin, height)
        if auto:
            obj["dwgreader_auto"] = True
        made.append(obj)
    return made


def clear_auto_buildings() -> None:
    """Удаляет дома прошлой авторасстановки. Дом, поставленный кнопкой, остаётся."""
    collection = bpy.data.collections.get(BUILDING_COLLECTION)
    if collection is None:
        return
    for obj in list(collection.objects):
        try:
            auto = bool(obj.get("dwgreader_auto"))
        except ReferenceError:
            continue
        if not auto:
            continue
        _drop_clip(obj)
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, "users", 1) == 0 and isinstance(mesh, bpy.types.Mesh):
            bpy.data.meshes.remove(mesh)


def _id_numbers(source, name: str) -> list[float]:
    try:
        raw = source.get(name)
    except ReferenceError:
        return []
    if raw is None:
        return []
    try:
        return [float(item) for item in list(raw)]
    except (TypeError, ValueError):
        return []


def _footprint_area(vertices, faces) -> float:
    """Площадь контура ноды: внешнее кольцо минус дыры, в единицах сетки."""
    loops = _oriented_loops(vertices, faces, (0.0, 0.0))
    if not loops:
        return 0.0
    area = abs(_ring_area(loops[0]))
    for hole in loops[1:]:
        area -= abs(_ring_area(hole))
    return max(area, 0.0)


def _building_spots(source, vertices, faces):
    """Пятна, на которые садится нода: треугольники одного полигона."""
    counts = _id_numbers(source, "polygon_face_counts") or _id_numbers(source, "polygon_faces")
    whole = [int(count) for count in counts if int(count) > 0]
    if whole and sum(whole) == len(faces):
        start = 0
        spots = []
        for count in whole:
            spots.append((vertices, faces[start:start + count]))
            start += count
        return spots
    return list(_islands(vertices, faces))


def place_auto_buildings(context) -> str:
    """Дом на каждый полигон зданий подложки. Мелкие пятна пропускаются, если фильтр включён."""
    scene = getattr(context, "scene", None)
    if scene is None or not bool(getattr(scene, "dwgreader_auto_buildings", False)):
        return ""
    from .build_scene import ROOT_NAME

    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        return ""

    from .placement_math import areas_to_square_meters, footprint_longer_side

    skip_small = bool(getattr(scene, "dwgreader_skip_small_buildings", False))
    min_area = float(getattr(scene, "dwgreader_min_building_area_m2", 0.0) or 0.0)
    pending = []
    for source in list(root.all_objects):
        if source.type != "MESH" or str(source.get("category") or "") != "building":
            continue
        if getattr(source, "data", None) is None or not source.data.polygons:
            continue
        vertices, faces = _mesh_faces(source, False)
        for spot_vertices, spot_faces in _building_spots(source, vertices, faces):
            pending.append((
                source.name,
                spot_vertices,
                spot_faces,
                _footprint_area(spot_vertices, spot_faces),
                footprint_longer_side(spot_vertices, spot_faces),
            ))
    areas_m2 = areas_to_square_meters(
        [item[3] for item in pending],
        [item[4] for item in pending],
    )
    made = 0
    skipped = 0
    template = None
    for (source_name, spot_vertices, spot_faces, _area, _side), area_m2 in zip(pending, areas_m2):
        if skip_small and area_m2 < min_area:
            skipped += 1
            continue
        try:
            built = _place_islands(context, [(spot_vertices, spot_faces)], template, auto=True)
        except Exception:
            logging.warning("Не удалось построить здание из %s", source_name, exc_info=True)
            continue
        if not built:
            continue
        template = built[-1]
        made += len(built)
    if made <= 0 and skipped <= 0:
        return ""
    view_layer = getattr(context, "view_layer", None)
    if template is not None and view_layer is not None:
        try:
            for item in list(context.selected_objects):
                item.select_set(False)
            template.select_set(True)
            view_layer.objects.active = template
        except (AttributeError, ReferenceError):
            logging.warning("Не удалось выделить поставленный дом", exc_info=True)
    note = f"здания {made}"
    if skipped:
        note += f", мельче порога {skipped}"
    return note


class DWGREADER_OT_building_preset(Operator):
    bl_idname = "dwgreader.building_preset"
    bl_label = "Тип дома"
    bl_description = "Подставляет готовую форму дома: этажи, секции и номер модуля"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(
        name="Тип дома",
        items=[(key, label, "") for key, label, _values in BUILDING_PRESETS],
    )

    def execute(self, context):
        obj = chosen_building(context)
        if obj is None:
            self.report({"WARNING"}, "Сначала выделите дом")
            return {"CANCELLED"}
        if not _preset_fits(obj, self.kind):
            self.report({"WARNING"}, "У этой ноды другие параметры")
            return {"CANCELLED"}
        try:
            apply_preset(obj, self.kind)
        except StopIteration:
            self.report({"ERROR"}, "Такого типа дома нет")
            return {"CANCELLED"}
        if context.view_layer is not None:
            context.view_layer.update()
        label = preset_label(obj)
        context.scene.dwgreader_status = f"Тип дома: {label}"
        self.report({"INFO"}, label)
        return {"FINISHED"}


class DWGREADER_OT_place_building(Operator):
    bl_idname = "dwgreader.place_building"
    bl_label = "Поставить здание"
    bl_description = "Ставит дом по контуру выделенного полигона. Без полигона — в 3D-курсор"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not blend_path().is_file():
            self.report({"ERROR"}, "В аддоне нет файла здания")
            return {"CANCELLED"}
        try:
            made = place_building(context)
        except Exception as error:
            self.report({"ERROR"}, f"Не удалось поставить здание: {error}")
            return {"CANCELLED"}
        for item in list(context.selected_objects):
            item.select_set(False)
        for obj in made:
            obj.select_set(True)
        context.view_layer.objects.active = made[-1]
        if len(made) == 1:
            note = "Здание поставлено. Параметры ниже меняют выделенный дом"
        else:
            note = f"Здания поставлены: {len(made)}. Параметры ниже меняют выделенный дом"
        context.scene.dwgreader_status = note
        self.report({"INFO"}, note)
        return {"FINISHED"}


class DWGREADER_OT_export_building_glb(Operator, ExportHelper):
    bl_idname = "dwgreader.export_building_glb"
    bl_label = "Экспорт здания в GLB"
    bl_description = "Запекает выбранные ноды и цвета фасада в один GLB"
    bl_options = {"REGISTER"}

    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})

    def execute(self, context):
        source_objects = [obj for obj in context.selected_objects if obj.type in SUPPORTED_OBJECT_TYPES]
        if not source_objects:
            self.report({"ERROR"}, "Выделите здание или его части")
            return {"CANCELLED"}

        filepath = bpy.path.ensure_ext(self.filepath, self.filename_ext)
        depsgraph = context.evaluated_depsgraph_get()
        export_objects = []
        export_meshes = []
        temp_materials = []
        temp_images = []
        warnings = []
        temp_directory = tempfile.mkdtemp(prefix="universal_glb_export_")
        previous_active = context.view_layer.objects.active
        previously_selected = list(context.selected_objects)

        try:
            for source_object in source_objects:
                export_object, export_mesh = _new_evaluated_export_object(
                    source_object, depsgraph, context.collection
                )
                export_objects.append(export_object)
                export_meshes.append(export_mesh)

                for slot_index in _used_material_slots(export_mesh):
                    if slot_index >= len(export_mesh.materials):
                        warnings.append(
                            f"{source_object.name}: полигон использует отсутствующий слот материала {slot_index}"
                        )
                        continue
                    source_material = export_mesh.materials[slot_index]
                    if source_material is None:
                        warnings.append(f"{source_object.name}: пустой слот материала {slot_index} пропущен")
                        continue
                    export_mesh.materials[slot_index] = _prepare_material_slot(
                        export_mesh,
                        slot_index,
                        source_material,
                        source_object.name,
                        temp_directory,
                        temp_materials,
                        temp_images,
                        warnings,
                    )

            bpy.ops.object.select_all(action="DESELECT")
            for export_object in export_objects:
                export_object.select_set(True)
            context.view_layer.objects.active = export_objects[0]
            bpy.ops.export_scene.gltf(**_gltf_export_kwargs(filepath))

        except Exception as error:
            self.report({"ERROR"}, f"Ошибка экспорта: {error}")
            return {"CANCELLED"}
        finally:
            for export_object in export_objects:
                if export_object.name in bpy.data.objects:
                    bpy.data.objects.remove(export_object, do_unlink=True)
            for export_mesh in export_meshes:
                if export_mesh.users == 0:
                    bpy.data.meshes.remove(export_mesh)
            for material in temp_materials:
                if material.users == 0:
                    bpy.data.materials.remove(material)
            for image in temp_images:
                if image.users == 0:
                    bpy.data.images.remove(image)
            shutil.rmtree(temp_directory, ignore_errors=True)

            for obj in previously_selected:
                if obj.name in bpy.data.objects:
                    obj.select_set(True)
            if previous_active and previous_active.name in bpy.data.objects:
                context.view_layer.objects.active = previous_active

        if warnings:
            print("\n[Здание] Предупреждения экспорта:")
            for warning in warnings:
                print(" -", warning)
            self.report(
                {"WARNING"},
                f"GLB записан, предупреждений: {len(warnings)}. Подробности в системной консоли",
            )
        else:
            self.report({"INFO"}, f"GLB записан: {filepath}")
        context.scene.dwgreader_status = f"GLB записан: {Path(filepath).name}"
        return {"FINISHED"}


class DWGREADER_OT_pick_building_node(Operator):
    bl_idname = "dwgreader.pick_building_node"
    bl_label = "Нода здания"
    bl_description = "Какую ноду ставить. Уже выбранный дом пересобирается на эту ноду"
    bl_options = {"REGISTER", "UNDO"}

    group: EnumProperty(name="Нода", items=node_enum_items)

    def execute(self, context):
        if not self.group or self.group == "none":
            return {"CANCELLED"}
        obj = chosen_building(context)
        if obj is not None:
            try:
                _retarget_building(context, obj, self.group)
            except Exception as error:
                message = f"Не удалось сменить ноду: {error}"
                self.report({"ERROR"}, message)
                context.scene.dwgreader_status = message[:180]
                return {"CANCELLED"}
            if context.view_layer is not None:
                context.view_layer.update()
        context.scene.dwgreader_building_node = self.group
        if obj is None:
            return {"FINISHED"}
        label = _group_of(obj) or self.group
        context.scene.dwgreader_status = f"Нода дома: {label}"
        return {"FINISHED"}


def draw_node_picker(layout, context) -> None:
    try:
        drivers = node_catalog()
    except Exception:
        logging.warning("Не удалось прочитать ноды здания", exc_info=True)
        layout.label(text="Не удалось прочитать ноды здания", icon="ERROR")
        return
    if not drivers:
        layout.label(text="В файле здания нет ноды дома", icon="ERROR")
        return
    current = str(getattr(context.scene, "dwgreader_building_node", "") or "")
    picked = chosen_driver(drivers, current)
    label = picked[1] if picked else drivers[0][1]
    row = layout.row(align=True)
    row.label(text="Нода")
    row.operator_menu_enum("dwgreader.pick_building_node", "group", text=label)


CLASSES = (
    DWGREADER_OT_building_preset,
    DWGREADER_OT_pick_building_node,
    DWGREADER_OT_place_building,
    DWGREADER_OT_export_building_glb,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
