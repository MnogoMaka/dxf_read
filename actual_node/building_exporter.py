bl_info = {
    "name": "Universal Evaluated GLB Export",
    "author": "OpenAI",
    "version": (2, 3, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > GLB Export",
    "description": "Export selected evaluated objects and convert supported dynamic materials",
    "category": "Import-Export",
}

import os
import shutil
import struct
import tempfile
import zlib
from array import array

import bpy
from bpy.props import StringProperty
from bpy.types import Operator, Panel
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix


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


class UNIVERSAL_OT_export_evaluated_glb(Operator, ExportHelper):
    bl_idname = "export_scene.universal_evaluated_glb"
    bl_label = "Экспортировать выбранные объекты в GLB"
    bl_description = "Применить вычисленную геометрию, трансформации и подготовить поддерживаемые материалы"
    bl_options = {"REGISTER"}

    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})

    def execute(self, context):
        source_objects = [obj for obj in context.selected_objects if obj.type in SUPPORTED_OBJECT_TYPES]
        if not source_objects:
            self.report({"ERROR"}, "Не выбраны объекты, преобразуемые в Mesh")
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
            print("\n[Universal GLB Export] Предупреждения:")
            for warning in warnings:
                print(" -", warning)
            self.report(
                {"WARNING"},
                f"GLB экспортирован; предупреждений: {len(warnings)}. Подробности в системной консоли",
            )
        else:
            self.report({"INFO"}, f"GLB экспортирован: {filepath}")
        return {"FINISHED"}


class UNIVERSAL_PT_export_evaluated_glb(Panel):
    bl_label = "Universal GLB Export"
    bl_idname = "UNIVERSAL_PT_export_evaluated_glb"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GLB Export"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Экспорт выбранных объектов")
        layout.label(text="Атрибуты: glb_color, glb_factor")
        layout.operator(UNIVERSAL_OT_export_evaluated_glb.bl_idname, icon="EXPORT")


CLASSES = (UNIVERSAL_OT_export_evaluated_glb, UNIVERSAL_PT_export_evaluated_glb)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
