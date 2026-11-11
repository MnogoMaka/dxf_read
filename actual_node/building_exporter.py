bl_info = {
    "name": "Universal Evaluated GLB / FBX Export",
    "author": "OpenAI",
    "version": (2, 4, 4),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Model Export",
    "description": "Export evaluated objects to GLB or FBX with supported materials and embedded textures",
    "category": "Import-Export",
}

import os
import hashlib
import shutil
import struct
import tempfile
import zlib
from array import array

import bpy
from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator, Panel
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix


# Recommended names first; the old names remain supported for compatibility.
COLOR_ATTRIBUTE_NAMES = ("glb_color", "export_color", "select_color")
FACTOR_ATTRIBUTE_NAMES = ("glb_factor", "export_factor", "factor")
SUPPORTED_OBJECT_TYPES = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
UNIFORM_TOLERANCE = 1.0e-5
EXPORT_FORMAT_ITEMS = (
    ("GLB", "GLB (.glb)", "GLB с прежней обработкой геометрии и материалов"),
    ("FBX", "FBX (.fbx)", "Применить вычисленную геометрию, подготовить материалы и встроить текстуры"),
)


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
    for node in material.node_tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output:
            shader = _linked_node(node.inputs.get("Surface"))
            if shader is not None and shader.type == "BSDF_PRINCIPLED":
                return shader
    shaders = [node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"]
    return shaders[0] if len(shaders) == 1 else None


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


def _linked_socket(socket):
    """Resolve pass-through Reroute nodes without guessing other expressions."""
    visited = set()
    while socket is not None and len(socket.links) == 1:
        source = socket.links[0].from_socket
        node = source.node
        if node.type != "REROUTE":
            return source
        if node in visited:
            return None
        visited.add(node)
        socket = node.inputs[0]
    return None


def _linked_node(socket):
    source = _linked_socket(socket)
    return source.node if source is not None else None


def _bypass_reroutes(material):
    """FBX's Principled wrapper only reads directly connected texture nodes."""
    if not material.use_nodes or material.node_tree is None:
        return
    tree = material.node_tree
    for link in list(tree.links):
        if link.from_node.type != "REROUTE" or link.to_node.type == "REROUTE":
            continue
        source = _linked_socket(link.to_socket)
        if source is not None:
            target = link.to_socket
            tree.links.remove(link)
            tree.links.new(source, target)


def _copy_image_texture_node(material, source_node, suffix):
    nodes, links = material.node_tree.nodes, material.node_tree.links
    copy_node = nodes.new("ShaderNodeTexImage")
    copy_node.name = source_node.name + suffix
    copy_node.image = source_node.image
    for property_name in ("interpolation", "projection", "projection_blend", "extension"):
        setattr(copy_node, property_name, getattr(source_node, property_name))
    for input_socket in source_node.inputs:
        copy_socket = copy_node.inputs.get(input_socket.name)
        if copy_socket is None:
            continue
        if input_socket.is_linked:
            for link in input_socket.links:
                links.new(link.from_socket, copy_socket)
        elif hasattr(input_socket, "default_value"):
            copy_socket.default_value = input_socket.default_value
    return copy_node


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


def _image_linear_pixels(image):
    """Image.pixels contains encoded file values, not necessarily linear RGB."""
    import numpy as np

    width, height = image.size[:]
    if width <= 0 or height <= 0:
        raise RuntimeError(f'Не удалось прочитать изображение "{image.name}"')
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape((height, width, 4))
    colorspace = image.colorspace_settings.name
    if colorspace == "sRGB":
        rgb = np.maximum(pixels[:, :, :3], 0.0)
        pixels[:, :, :3] = np.where(
            rgb <= 0.04045, rgb / 12.92,
            np.power((rgb + 0.055) / 1.055, 2.4),
        )
    elif colorspace not in {"Non-Color", "Linear", "Linear Rec.709"}:
        raise RuntimeError(
            f'{image.name}: преобразование изображения из "{colorspace}" требует запекания'
        )
    return pixels


def _write_linear_data_png(filepath, pixels):
    """16-bit linear PNG for normals; no sRGB conversion or color tagging."""
    import numpy as np

    height, width = pixels.shape[:2]
    encoded = np.rint(np.clip(pixels, 0.0, 1.0) * 65535.0).astype(">u2")
    scanlines = b"".join(b"\x00" + encoded[row].tobytes()
                         for row in range(height - 1, -1, -1))
    header = struct.pack(">IIBBBBB", width, height, 16, 6, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(scanlines, level=6))
            + _png_chunk(b"IEND", b""))
    with open(filepath, "wb") as image_file:
        image_file.write(data)


def _normal_texture_pattern(normal):
    """Direct image or RGB Separate/Combine with optional green inversion."""
    color_output = _linked_socket(normal.inputs.get("Color"))
    if color_output is None:
        return None
    source = color_output.node
    if source.type == "TEX_IMAGE" and source.image is not None:
        return (source, 0.0) if color_output == source.outputs["Color"] else None
    if source.type not in {"COMBINE_COLOR", "COMBRGB"}:
        return None
    if source.type == "COMBINE_COLOR" and source.mode != "RGB":
        return None
    separate = None
    green_factor = 0.0
    for index, channel in enumerate(source.inputs[:3]):
        output = _linked_socket(channel)
        if output is None:
            return None
        node = output.node
        if index == 1 and node.type == "INVERT":
            factor = node.inputs["Fac"]
            if factor.is_linked:
                return None
            green_factor = float(factor.default_value)
            output = _linked_socket(node.inputs["Color"])
            if output is None:
                return None
            node = output.node
        if node.type not in {"SEPARATE_COLOR", "SEPRGB"}:
            return None
        if node.type == "SEPARATE_COLOR" and node.mode != "RGB":
            return None
        if output != node.outputs[index] or (separate is not None and node != separate):
            return None
        separate = node
    image_node = _linked_node(separate.inputs[0])
    if image_node is None or image_node.type != "TEX_IMAGE" or image_node.image is None:
        return None
    if _linked_socket(separate.inputs[0]) != image_node.outputs["Color"]:
        return None
    return image_node, green_factor


def _prepare_portable_normals(mesh, object_name, temp_directory, temp_materials,
                              temp_images, normal_cache, warnings):
    for slot_index in sorted(_used_material_slots(mesh)):
        if slot_index >= len(mesh.materials):
            continue
        material = mesh.materials[slot_index]
        principled = _find_principled(material)
        if principled is None:
            continue
        normal = _linked_node(principled.inputs.get("Normal"))
        if normal is None or normal.type != "NORMAL_MAP":
            continue
        pattern = _normal_texture_pattern(normal)
        if pattern is None:
            if _find_upstream_image(normal.inputs["Color"]) is not None:
                warnings.append(f'{object_name} / {material.name}: схема normal map '
                                'не поддерживается; требуется запекание')
            continue
        source_node, green_factor = pattern
        source_image = source_node.image
        direct = _linked_node(normal.inputs["Color"]) == source_node
        if direct and source_image.colorspace_settings.name == "Non-Color":
            continue
        key = (source_image.as_pointer(), source_image.colorspace_settings.name, green_factor)
        image = normal_cache.get(key)
        if image is None:
            pixels = _image_linear_pixels(source_image)
            green = pixels[:, :, 1]
            pixels[:, :, 1] = green * (1.0 - green_factor) + (1.0 - green) * green_factor
            pixels[:, :, 3] = 1.0
            path = os.path.join(temp_directory, f"export_normal_{len(normal_cache):04d}.png")
            _write_linear_data_png(path, pixels)
            image = bpy.data.images.load(path, check_existing=False)
            image.name = f"export_normal_{source_image.name}"
            image.colorspace_settings.name = "Non-Color"
            image.alpha_mode = "CHANNEL_PACKED"
            temp_images.append(image)
            normal_cache[key] = image
        export_material = material.copy()
        export_material.name = f"export_{object_name}_{slot_index}_{material.name}"
        temp_materials.append(export_material)
        mesh.materials[slot_index] = export_material
        nodes, links = export_material.node_tree.nodes, export_material.node_tree.links
        normal_copy = nodes[normal.name]
        texture_copy = _copy_image_texture_node(
            export_material, nodes[source_node.name], "_Export_Normal"
        )
        texture_copy.image = image
        _clear_socket_links(export_material.node_tree, normal_copy.inputs["Color"])
        links.new(texture_copy.outputs["Color"], normal_copy.inputs["Color"])


def _mix_image_pixels(source_image, color, factor, image_name, temp_directory):
    width, height = source_image.size[:]
    if width <= 0 or height <= 0:
        raise RuntimeError(f'Не удалось прочитать изображение "{source_image.name}"')

    pixel_count = width * height * 4
    source_pixels = _image_linear_pixels(source_image).ravel()

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


def _export_filepath(filepath, file_format):
    extension = ".fbx" if file_format == "FBX" else ".glb"
    stem, old_extension = os.path.splitext(filepath)
    if old_extension.lower() in {".glb", ".fbx"}:
        return stem + extension
    return bpy.path.ensure_ext(filepath, extension)


def _update_file_format(operator, context):
    extension = ".fbx" if operator.file_format == "FBX" else ".glb"
    # RNA update callbacks can receive a generic Operator proxy that cannot
    # store arbitrary Python attributes. ExportHelper reads the class value.
    UNIVERSAL_OT_export_evaluated_glb.filename_ext = extension
    operator.filter_glob = "*" + extension
    if operator.filepath:
        operator.filepath = _export_filepath(operator.filepath, operator.file_format)


def _fbx_export_kwargs(filepath):
    # Materials have already been converted by the same preparation as GLB.
    # COPY + embed_textures is required before temporary image files are removed.
    return {
        "filepath": filepath,
        "use_selection": True,
        "object_types": {"MESH"},
        "use_mesh_modifiers": False,
        "bake_anim": False,
        "path_mode": "COPY",
        "embed_textures": True,
        "colors_type": "SRGB",
        "prioritize_active_color": True,
        # Preserve flat/smooth face flags alongside custom normals. With
        # normals only, Blender imports every face as smooth and recreates
        # flat shading by marking thousands of edges as sharp.
        "mesh_smooth_type": "FACE",
        "use_mesh_edges": False,
        "use_triangles": False,
        "axis_forward": "-Z",
        "axis_up": "Y",
        "global_scale": 1.0,
        "apply_unit_scale": True,
        "apply_scale_options": "FBX_SCALE_NONE",
        "bake_space_transform": False,
    }


def _select_export_objects(context, objects):
    for obj in list(context.selected_objects):
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    context.view_layer.objects.active = objects[0]


def _prioritize_fbx_uv_map(mesh):
    # FBX readers that ignore UVSet names use the first UV channel. Put the
    # active render layer there, while retaining every other layer by name.
    layers = list(mesh.uv_layers)
    primary = next((layer for layer in layers if layer.active_render), mesh.uv_layers.active)
    if primary is None or not layers or primary.name == layers[0].name:
        return
    active_name = mesh.uv_layers.active.name
    primary_name = primary.name
    saved = []
    for layer in layers:
        values = array("f", [0.0]) * (len(mesh.loops) * 2)
        layer.data.foreach_get("uv", values)
        saved.append((layer.name, values))
    saved.sort(key=lambda item: item[0] != primary_name)
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[-1])
    for name, values in saved:
        layer = mesh.uv_layers.new(name=name)
        layer.data.foreach_set("uv", values)
    mesh.uv_layers.active = mesh.uv_layers[active_name]
    mesh.uv_layers[primary_name].active_render = True


def _upstream_attribute_names(socket, visited=None):
    if visited is None:
        visited = set()
    names = set()
    if socket is None:
        return names
    for link in socket.links:
        node = link.from_node
        if node in visited:
            continue
        visited.add(node)
        name = _node_attribute_name(node)
        if name:
            names.add(name)
        for input_socket in node.inputs:
            names.update(_upstream_attribute_names(input_socket, visited))
    return names


def _referenced_image_nodes(material):
    if not material.use_nodes or material.node_tree is None:
        return []
    visited = set()

    def visit(socket):
        for link in socket.links:
            node = link.from_node
            if node in visited:
                continue
            visited.add(node)
            for input_socket in node.inputs:
                visit(input_socket)

    for node in material.node_tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output:
            for socket in node.inputs:
                visit(socket)
    return [node for node in visited if node.type == "TEX_IMAGE"]


def _needs_fbx_base_color_bake(mesh):
    # glTF can bind COLOR_0 to a shader; FBX does not transfer that node link.
    # Uniform colors were already made static. Bake remaining vertex-color
    # graphs instead of silently exporting a different-looking material.
    for slot_index in _used_material_slots(mesh):
        if slot_index >= len(mesh.materials):
            continue
        principled = _find_principled(mesh.materials[slot_index])
        if principled and _upstream_attribute_names(principled.inputs.get("Base Color")):
            return True
    return False


def _used_mesh_materials(mesh):
    return [mesh.materials[index] for index in sorted(_used_material_slots(mesh))
            if index < len(mesh.materials) and mesh.materials[index] is not None]


def _bake_fbx_base_colors(context, export_object, temp_directory, temp_images):
    """Bake remaining attribute-driven Base Color to a portable FBX atlas."""
    mesh = export_object.data
    original_uv = mesh.uv_layers.active
    # Give implicit source-image UVs an explicit reference before adding the atlas.
    for material in _used_mesh_materials(mesh):
        if material is None or not material.use_nodes:
            continue
        for node in _referenced_image_nodes(material):
            if node.inputs["Vector"].is_linked:
                continue
            if original_uv is None:
                raise RuntimeError(f'{export_object.name}: для текстуры FBX требуется UV-развёртка')
            uv_node = material.node_tree.nodes.new("ShaderNodeUVMap")
            uv_node.uv_map = original_uv.name
            material.node_tree.links.new(uv_node.outputs["UV"], node.inputs["Vector"])

    bake_uv = mesh.uv_layers.new(name="export_fbx_color_uv")
    mesh.uv_layers.active = bake_uv
    bake_uv.active_render = True
    _select_export_objects(context, [export_object])
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(island_margin=0.02)
    finally:
        if export_object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

    resolution = 4096 if len(mesh.polygons) > 10000 else 2048
    image = bpy.data.images.new(
        name=f"{export_object.name}_base_color", width=resolution, height=resolution,
        alpha=True, float_buffer=True,
    )
    temp_images.append(image)
    image.colorspace_settings.name = "sRGB"
    bake_states = []
    previous_engine = context.scene.render.engine
    previous_device = context.scene.cycles.device if hasattr(context.scene, "cycles") else None
    previous_samples = context.scene.cycles.samples if hasattr(context.scene, "cycles") else None
    try:
        for material in _used_mesh_materials(mesh):
            if material is None:
                continue
            principled = _find_principled(material)
            if principled is None:
                raise RuntimeError(f'{material.name}: для запекания FBX нужен Principled BSDF')
            nodes, links = material.node_tree.nodes, material.node_tree.links
            output = next((node for node in nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output), None)
            if output is None:
                raise RuntimeError(f'{material.name}: отсутствует активный Material Output')
            surface = output.inputs["Surface"]
            original_links = [(link.from_socket, link.to_socket) for link in surface.links]
            emission = nodes.new("ShaderNodeEmission")
            target = nodes.new("ShaderNodeTexImage")
            target.image = image
            original_active = nodes.active
            nodes.active = target
            bake_states.append((material, original_links, emission, target, original_active))
            base = principled.inputs["Base Color"]
            if base.is_linked:
                links.new(base.links[0].from_socket, emission.inputs["Color"])
            else:
                emission.inputs["Color"].default_value = base.default_value
            _clear_socket_links(material.node_tree, surface)
            links.new(emission.outputs["Emission"], surface)
        context.scene.render.engine = "CYCLES"
        context.scene.cycles.device = "CPU"
        context.scene.cycles.samples = 1
        result = bpy.ops.object.bake(
            type="EMIT", margin=4, use_clear=True, uv_layer=bake_uv.name,
            use_selected_to_active=False, target="IMAGE_TEXTURES",
        )
        if "FINISHED" not in result:
            raise RuntimeError("Запекание Base Color для FBX отменено")
        pixels = array("f", [0.0]) * (resolution * resolution * 4)
        image.pixels.foreach_get(pixels)
        # Alpha is kept in the existing Principled Alpha graph; this atlas
        # contains color only, so its own alpha must be opaque.
        for index in range(3, len(pixels), 4):
            pixels[index] = 1.0
        image_path = os.path.join(temp_directory, f"{bpy.path.clean_name(image.name)}.png")
        _write_linear_rgba_to_srgb_png(image_path, pixels, resolution, resolution)
        atlas = bpy.data.images.load(image_path, check_existing=False)
        temp_images.append(atlas)
        atlas.colorspace_settings.name = "sRGB"
    finally:
        context.scene.render.engine = previous_engine
        if previous_device is not None:
            context.scene.cycles.device = previous_device
        if previous_samples is not None:
            context.scene.cycles.samples = previous_samples
        for material, original_links, emission, target, original_active in bake_states:
            for source_socket, target_socket in original_links:
                material.node_tree.links.new(source_socket, target_socket)
            material.node_tree.nodes.remove(emission)
            material.node_tree.nodes.remove(target)
            material.node_tree.nodes.active = original_active
    for material, *_ in bake_states:
        principled = _find_principled(material)
        base = principled.inputs["Base Color"]
        texture = material.node_tree.nodes.new("ShaderNodeTexImage")
        texture.image = atlas
        uv_node = material.node_tree.nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = bake_uv.name
        material.node_tree.links.new(uv_node.outputs["UV"], texture.inputs["Vector"])
        _clear_socket_links(material.node_tree, base)
        material.node_tree.links.new(texture.outputs["Color"], base)


def _fbx_file_image(source_image, temp_directory, temp_images, image_cache):
    """Return a real FILE image, preserving packed bytes or current pixels."""
    key = source_image.as_pointer()
    if key in image_cache:
        return image_cache[key]
    if source_image.source not in {"FILE", "GENERATED"}:
        raise RuntimeError(f'{source_image.name}: тип изображения {source_image.source} не поддерживается FBX')

    path = bpy.path.abspath(source_image.filepath, library=source_image.library)
    packed = source_image.packed_file
    dirty = source_image.is_dirty
    safe_name = f"export_fbx_{len(image_cache):04d}_{bpy.path.clean_name(source_image.name)}"

    if packed is not None and not dirty:
        data = bytes(packed.data)
        if not data:
            raise RuntimeError(f'Упакованная текстура "{source_image.name}" пуста')
        extension = os.path.splitext(path)[1].lower()
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            extension = ".jpg"
        elif data.startswith(b"v/1\x01"):
            extension = ".exr"
        if not extension:
            extension = ".png"
        path = os.path.join(temp_directory, safe_name + extension)
        with open(path, "wb") as image_file:
            image_file.write(data)
    elif source_image.source == "FILE" and not dirty and os.path.isfile(path):
        # FBX embeds each file path only once, even if several Image
        # datablocks reference it. Keep a unique path AND basename per
        # prepared image so every linked Video receives its own payload.
        extension = os.path.splitext(path)[1].lower() or ".png"
        unique_path = os.path.join(temp_directory, safe_name + extension)
        shutil.copyfile(path, unique_path)
        path = unique_path
    else:
        width, height = source_image.size[:]
        if width <= 0 or height <= 0:
            raise RuntimeError(f'Не удалось прочитать текстуру "{source_image.name}" для FBX')
        pixels = array("f", [0.0]) * (width * height * 4)
        source_image.pixels.foreach_get(pixels)
        snapshot = bpy.data.images.new(
            name=safe_name, width=width, height=height,
            alpha=True, float_buffer=source_image.is_float,
        )
        temp_images.append(snapshot)
        snapshot.colorspace_settings.name = source_image.colorspace_settings.name
        snapshot.alpha_mode = source_image.alpha_mode
        snapshot.pixels.foreach_set(pixels)
        snapshot.update()
        path = os.path.join(temp_directory, safe_name + ".png")
        snapshot.filepath_raw = path
        snapshot.file_format = "PNG"
        snapshot.save()

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f'Не удалось сохранить данные текстуры "{source_image.name}" для FBX')
    # Saving a GENERATED image does not guarantee a FILE datablock. The FBX
    # material must reference the reloaded file, never the snapshot above.
    image = bpy.data.images.load(path, check_existing=False)
    temp_images.append(image)
    image.name = safe_name
    image.colorspace_settings.name = source_image.colorspace_settings.name
    image.alpha_mode = source_image.alpha_mode
    image_cache[key] = image
    return image


def _prepare_fbx_color_channels(material):
    principled = _find_principled(material)
    if principled is None:
        return
    base = principled.inputs.get("Base Color")
    emission = principled.inputs.get("Emission Color")
    if emission is None:
        emission = principled.inputs.get("Emission")
    base_node = _linked_node(base)
    emission_node = _linked_node(emission)

    if base_node is not None and base_node.type == "TEX_IMAGE":
        # A linked shader ignores the socket default. FBX stores that default
        # as a diffuse multiplier, so neutral white preserves the texture.
        base.default_value = (1.0, 1.0, 1.0, 1.0)
    if emission_node is not None and emission_node.type == "TEX_IMAGE":
        emission.default_value = (1.0, 1.0, 1.0, 1.0)
    if emission_node is None or emission_node is not base_node or emission_node.type != "TEX_IMAGE":
        return

    # Separate texture nodes for diffuse and emissive channels. They can share
    # the same FILE image, which will still be embedded once.
    links = material.node_tree.links
    copy_node = _copy_image_texture_node(material, emission_node, "_FBX_Emission")
    output_name = emission.links[0].from_socket.name
    _clear_socket_links(material.node_tree, emission)
    links.new(copy_node.outputs[output_name], emission)


def _validate_fbx_embedded_textures(filepath, objects):
    # Verify actual Material <- Texture <- Video connections, not merely an
    # occurrence of image bytes elsewhere in the file. Use Blender's parser.
    from io_scene_fbx import parse_fbx

    channels = (
        ("Base Color", b"DiffuseColor"),
        ("Emission Color", b"EmissiveColor"),
        ("Emission", b"EmissiveColor"),
        ("Emission Strength", b"EmissiveFactor"),
        ("Metallic", b"ReflectionFactor"),
        ("Roughness", b"ShininessExponent"),
        ("Specular IOR Level", b"SpecularFactor"),
        ("Alpha", b"TransparencyFactor"),
    )
    required = {}
    image_hashes = {}
    for obj in objects:
        for material in _used_mesh_materials(obj.data):
            principled = _find_principled(material)
            if principled is None:
                continue
            inputs = [(principled.inputs.get(name), channel) for name, channel in channels]
            normal = _linked_node(principled.inputs.get("Normal"))
            if normal is not None and normal.type == "NORMAL_MAP":
                inputs.append((normal.inputs.get("Color"), b"NormalMap"))
            for socket, channel in inputs:
                node = _linked_node(socket)
                if node is None or node.type != "TEX_IMAGE" or node.image is None:
                    if socket is not None and _find_upstream_image(socket) is not None:
                        raise RuntimeError(
                            f'{material.name} / {socket.name}: FBX не может перенести '
                            'эту схему текстуры; требуется запекание'
                        )
                    continue
                image = node.image
                if image.source != "FILE":
                    raise RuntimeError(f'{material.name}: текстура "{image.name}" не подготовлена как файл')
                path = bpy.path.abspath(image.filepath, library=image.library)
                if path not in image_hashes:
                    with open(path, "rb") as image_file:
                        payload = image_file.read()
                    if not payload:
                        raise RuntimeError(f'Текстура "{image.name}" пуста')
                    image_hashes[path] = hashlib.sha256(payload).digest()
                required[(material.name, channel)] = (image_hashes[path], image.name)
    if not required:
        return 0
    root, _version = parse_fbx.parse(filepath)
    object_block = next((item for item in root.elems if item.id == b"Objects"), None)
    connection_block = next((item for item in root.elems if item.id == b"Connections"), None)
    if object_block is None or connection_block is None:
        raise RuntimeError("В FBX отсутствуют Objects или Connections")
    material_ids = {}
    videos = {}
    for item in object_block.elems:
        if item.id == b"Material":
            name = item.props[1].split(b"\x00\x01")[0].decode("utf-8")
            material_ids[name] = item.props[0]
        elif item.id == b"Video":
            content = next((c.props[0] for c in item.elems
                            if c.id == b"Content" and c.props), b"")
            if content:
                videos[item.props[0]] = hashlib.sha256(content).digest()
    video_links = {}
    material_links = {}
    for item in connection_block.elems:
        if item.id != b"C" or len(item.props) < 3:
            continue
        kind, source, target = item.props[:3]
        if kind == b"OO" and source in videos:
            video_links.setdefault(target, set()).add(videos[source])
        elif kind == b"OP" and len(item.props) >= 4:
            material_links.setdefault((target, item.props[3]), []).append(source)
    for (material_name, channel), (digest, image_name) in required.items():
        material_id = material_ids.get(material_name)
        textures = material_links.get((material_id, channel), ())
        if not any(digest in video_links.get(texture, ()) for texture in textures):
            raise RuntimeError(
                f'FBX: текстура "{image_name}" не встроена или не подключена к '
                f'"{material_name}" / {channel.decode("ascii")}'
            )
    return len(set(image_hashes.values()))


def _prepare_fbx_materials(mesh, object_name, temp_directory, temp_materials, temp_images, image_cache):
    # Copy even static materials: materialized packed/generated images must
    # never change image paths in the user's original scene.
    used_slots = _used_material_slots(mesh)
    for slot_index in range(len(mesh.materials)):
        # Blender's FBX exporter includes every slot, even unused ones.
        # Remove their source-material references from this temporary mesh
        # without changing the polygon material indices.
        if slot_index not in used_slots:
            mesh.materials[slot_index] = None
            continue
        if slot_index >= len(mesh.materials) or mesh.materials[slot_index] is None:
            continue
        source = mesh.materials[slot_index]
        material = source.copy()
        material.name = f"export_{object_name}_{slot_index}_fbx_material"
        temp_materials.append(material)
        mesh.materials[slot_index] = material
        if not material.use_nodes or material.node_tree is None:
            continue
        _bypass_reroutes(material)
        for node in _referenced_image_nodes(material):
            if node.image is None:
                continue
            node.image = _fbx_file_image(node.image, temp_directory, temp_images, image_cache)
        _prepare_fbx_color_channels(material)
    for index, attribute in enumerate(mesh.color_attributes):
        if attribute.name in COLOR_ATTRIBUTE_NAMES:
            mesh.color_attributes.active_color_index = index
            mesh.color_attributes.render_color_index = index
            break


class UNIVERSAL_OT_export_evaluated_glb(Operator, ExportHelper):
    bl_idname = "export_scene.universal_evaluated_glb"
    bl_label = "Экспортировать выбранные объекты"
    bl_description = "Применить вычисленную геометрию, трансформации и подготовить поддерживаемые материалы"
    bl_options = {"REGISTER"}

    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})
    file_format: EnumProperty(name="Тип файла", items=EXPORT_FORMAT_ITEMS, default="GLB", update=_update_file_format)

    def check(self, context):
        previous_path = self.filepath
        _update_file_format(self, context)
        return self.filepath != previous_path

    def draw(self, context):
        self.layout.prop(self, "file_format")
        if self.file_format == "FBX":
            self.layout.label(text="Текстуры встраиваются в FBX")

    def execute(self, context):
        if context.mode != "OBJECT":
            self.report({"ERROR"}, "Перейдите в Object Mode перед экспортом")
            return {"CANCELLED"}
        source_objects = [obj for obj in context.selected_objects if obj.type in SUPPORTED_OBJECT_TYPES]
        if not source_objects:
            self.report({"ERROR"}, "Не выбраны объекты, преобразуемые в Mesh")
            return {"CANCELLED"}

        _update_file_format(self, context)
        filepath = bpy.path.abspath(_export_filepath(self.filepath, self.file_format))
        depsgraph = context.evaluated_depsgraph_get()
        export_objects = []
        export_meshes = []
        temp_materials = []
        temp_images = []
        warnings = []
        temp_directory = tempfile.mkdtemp(prefix="universal_model_export_")
        temp_collection = bpy.data.collections.new("__MODEL_EXPORT__")
        context.scene.collection.children.link(temp_collection)
        image_cache = {}
        normal_cache = {}
        embedded_textures = 0
        previous_active = context.view_layer.objects.active
        previously_selected = list(context.selected_objects)

        try:
            context.view_layer.update()
            for source_object in source_objects:
                export_object, export_mesh = _new_evaluated_export_object(
                    source_object, depsgraph, temp_collection
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

                _prepare_portable_normals(
                    export_mesh, source_object.name, temp_directory, temp_materials,
                    temp_images, normal_cache, warnings,
                )

                if self.file_format == "FBX":
                    _prepare_fbx_materials(
                        export_mesh, source_object.name, temp_directory,
                        temp_materials, temp_images, image_cache,
                    )
                    if _needs_fbx_base_color_bake(export_mesh):
                        _bake_fbx_base_colors(context, export_object, temp_directory, temp_images)
                        for material in _used_mesh_materials(export_mesh):
                            _prepare_fbx_color_channels(material)
                    _prioritize_fbx_uv_map(export_mesh)

            _select_export_objects(context, export_objects)
            if self.file_format == "FBX":
                result = bpy.ops.export_scene.fbx(**_fbx_export_kwargs(filepath))
            else:
                result = bpy.ops.export_scene.gltf(**_gltf_export_kwargs(filepath))
            if "FINISHED" not in result:
                raise RuntimeError(f"Экспорт {self.file_format} отменён")
            if self.file_format == "FBX":
                embedded_textures = _validate_fbx_embedded_textures(filepath, export_objects)

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
            if temp_collection.name in bpy.data.collections:
                bpy.data.collections.remove(temp_collection)
            for material in reversed(temp_materials):
                if material.users == 0:
                    bpy.data.materials.remove(material)
            for image in temp_images:
                if image.users == 0:
                    bpy.data.images.remove(image)
            shutil.rmtree(temp_directory, ignore_errors=True)

            for obj in list(context.selected_objects):
                obj.select_set(False)
            for obj in previously_selected:
                if obj.name in bpy.data.objects:
                    obj.select_set(True)
            if previous_active and previous_active.name in bpy.data.objects:
                context.view_layer.objects.active = previous_active
            elif previous_active is None:
                context.view_layer.objects.active = None

        if warnings:
            print(f"\n[Universal {self.file_format} Export] Предупреждения:")
            for warning in warnings:
                print(" -", warning)
            self.report(
                {"WARNING"},
                f"{self.file_format} экспортирован; предупреждений: {len(warnings)}. Подробности в системной консоли",
            )
        else:
            texture_info = f"; встроенных текстур: {embedded_textures}" if self.file_format == "FBX" else ""
            self.report({"INFO"}, f"{self.file_format} экспортирован{texture_info}: {filepath}")
        return {"FINISHED"}


class UNIVERSAL_PT_export_evaluated_glb(Panel):
    bl_label = "Universal GLB / FBX Export"
    bl_idname = "UNIVERSAL_PT_export_evaluated_glb"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Model Export"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Экспорт выбранных объектов")
        layout.label(text="Атрибуты: glb_color, glb_factor")
        layout.prop(context.scene, "universal_model_export_format", text="Тип файла")
        operator = layout.operator(UNIVERSAL_OT_export_evaluated_glb.bl_idname, icon="EXPORT")
        operator.file_format = context.scene.universal_model_export_format


CLASSES = (UNIVERSAL_OT_export_evaluated_glb, UNIVERSAL_PT_export_evaluated_glb)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.universal_model_export_format = EnumProperty(
        name="Тип файла", items=EXPORT_FORMAT_ITEMS, default="GLB",
    )


def unregister():
    if hasattr(bpy.types.Scene, "universal_model_export_format"):
        del bpy.types.Scene.universal_model_export_format
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
