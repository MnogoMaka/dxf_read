"""Подбор габарита и точки лавок. Без Blender, чтобы это можно было проверить отдельно."""

from __future__ import annotations

import random
import struct
import sys
import zlib
from bisect import bisect_right
from math import atan2, cos, hypot, pi, sin
from pathlib import Path


FIT_SLACK_M = 0.05


def _face_triangles(vertices, face: tuple[int, ...]) -> list[tuple[int, int, int]]:
    """Корректно триангулирует также вогнутый n-gon; веер из первой вершины не используется."""
    clean: list[int] = []
    for raw_index in face:
        index = int(raw_index)
        if index < 0 or index >= len(vertices):
            return []
        if not clean or index != clean[-1]:
            clean.append(index)
    if len(clean) > 1 and clean[0] == clean[-1]:
        clean.pop()
    if len(clean) < 3:
        return []
    if len(clean) == 3:
        return [(clean[0], clean[1], clean[2])]
    try:
        import mapbox_earcut as earcut
        import numpy as np

        ring = np.asarray([(vertices[index][0], vertices[index][1]) for index in clean], dtype=np.float64)
        indices = earcut.triangulate_float64(ring, np.asarray([len(clean)], dtype=np.uint32))
    except Exception:
        return []
    return [
        (clean[int(indices[pos])], clean[int(indices[pos + 1])], clean[int(indices[pos + 2])])
        for pos in range(0, len(indices) - 2, 3)
    ]


def _mesh_triangles(vertices, faces):
    triangles = []
    for face in faces:
        for a_index, b_index, c_index in _face_triangles(vertices, face):
            a, b, c = vertices[a_index], vertices[b_index], vertices[c_index]
            weight = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) * 0.5
            if weight >= 1e-12:
                triangles.append((a, b, c, weight))
    return triangles


def polygon_area_xy(vertices: list[tuple[float, float, float]], faces: list[tuple[int, ...]]) -> float:
    return sum(triangle[3] for triangle in _mesh_triangles(vertices, faces))


def footprint_longer_side(vertices, faces) -> float:
    """Длинная сторона пятна в единицах сетки."""
    xs: list[float] = []
    ys: list[float] = []
    for face in faces:
        for index in face:
            point = vertices[index]
            xs.append(float(point[0]))
            ys.append(float(point[1]))
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def mesh_plan_side(vertices: list[tuple[float, float, float]]) -> float:
    """Сторона квадрата плана: большая сторона рамки XY уже поставленной на землю сетки."""
    if not vertices:
        return 0.0
    xs = [float(vertex[0]) for vertex in vertices]
    ys = [float(vertex[1]) for vertex in vertices]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def areas_to_square_meters(areas: list[float], longer_sides: list[float]) -> list[float]:
    """Площади контуров в м². Один множитель на все пятна плана.

    Длинная сторона больше 2000 единиц сцены — чертёж ещё в миллиметрах.
    """
    if not areas:
        return []
    sides = [float(side) for side in longer_sides]
    median = sorted(sides)[len(sides) // 2] if sides else 0.0
    factor = 1e-6 if median > 2000.0 else 1.0
    return [max(float(area), 0.0) * factor for area in areas]


def face_groups(faces: list[tuple[int, ...]]) -> list[list[int]]:
    """Грани одного полигона склеены общим ребром. Отдельные пятна слоя — отдельные группы."""
    parent = list(range(len(faces)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    edges: dict[tuple[int, int], int] = {}
    for face_index, face in enumerate(faces):
        count = len(face)
        for index in range(count):
            a = face[index]
            b = face[(index + 1) % count]
            key = (a, b) if a < b else (b, a)
            other = edges.get(key)
            if other is None:
                edges[key] = face_index
            else:
                union(face_index, other)
    groups: dict[int, list[int]] = {}
    for face_index in range(len(faces)):
        groups.setdefault(find(face_index), []).append(face_index)
    return list(groups.values())


def polygon_centroid_xy(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
) -> tuple[float, float, float] | None:
    """Центр площади полигона, а не центр рамки вокруг всех пятен слоя."""
    weight_sum = 0.0
    cx = cy = cz = 0.0
    for origin, a, b, weight in _mesh_triangles(vertices, faces):
        weight_sum += weight
        cx += (origin[0] + a[0] + b[0]) / 3.0 * weight
        cy += (origin[1] + a[1] + b[1]) / 3.0 * weight
        cz += (origin[2] + a[2] + b[2]) / 3.0 * weight
    if weight_sum < 1e-12:
        return None
    return cx / weight_sum, cy / weight_sum, cz / weight_sum


def scatter_points(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    count: int,
    seed: int,
) -> list[tuple[float, float, float]]:
    """Случайные точки внутри граней. Плотность задаёт число, сид — рисунок."""
    triangles = _mesh_triangles(vertices, faces)
    if not triangles or count <= 0:
        return []
    cumulative: list[float] = []
    running = 0.0
    for _origin, _a, _b, weight in triangles:
        running += weight
        cumulative.append(running)
    total = running
    rng = random.Random(seed)
    points: list[tuple[float, float, float]] = []
    for _ in range(count):
        mark = rng.random() * total
        chosen = triangles[min(bisect_right(cumulative, mark), len(triangles) - 1)]
        origin, a, b, _weight = chosen
        r1 = rng.random()
        r2 = rng.random()
        if r1 + r2 > 1.0:
            r1 = 1.0 - r1
            r2 = 1.0 - r2
        w0 = 1.0 - r1 - r2
        points.append((
            w0 * origin[0] + r1 * a[0] + r2 * b[0],
            w0 * origin[1] + r1 * a[1] + r2 * b[1],
            w0 * origin[2] + r1 * a[2] + r2 * b[2],
        ))
    return points


def _box_gap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    gap_x = max(left[0] - right[2], right[0] - left[2], 0.0)
    gap_y = max(left[1] - right[3], right[1] - left[3], 0.0)
    return hypot(gap_x, gap_y)


def split_far_parts(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    gap_m: float,
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]]:
    """Близкие пятна одного слоя остаются вместе. Далёкие становятся отдельными сетками."""
    return [(part_vertices, part_faces) for part_vertices, part_faces, _faces in _gap_meshes(vertices, faces, gap_m)]


def _gap_meshes(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    gap_m: float,
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]], list[int]]]:
    if not faces:
        return []
    if len(faces) <= 1:
        return [(vertices, faces, list(range(len(faces))))]
    groups = face_groups(faces)
    if len(groups) <= 1:
        return [(vertices, faces, list(range(len(faces))))]
    boxes: list[tuple[float, float, float, float]] = []
    for group in groups:
        xs: list[float] = []
        ys: list[float] = []
        for face_index in group:
            for index in faces[face_index]:
                xs.append(vertices[index][0])
                ys.append(vertices[index][1])
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    parent = list(range(len(groups)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    distance = max(float(gap_m), 0.0)
    active: list[int] = []
    for left in sorted(range(len(groups)), key=lambda index: (boxes[index][0], index)):
        ax0 = boxes[left][0]
        active = [index for index in active if boxes[index][2] >= ax0 - distance]
        for right in active:
            if _box_gap(boxes[left], boxes[right]) <= distance:
                union(left, right)
        active.append(left)
    clustered: dict[int, list[int]] = {}
    for index, group in enumerate(groups):
        clustered.setdefault(find(index), []).extend(group)
    return _mesh_parts(vertices, faces, list(clustered.values()))


def _mesh_parts(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    face_sets: list[list[int]],
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]], list[int]]]:
    """Сетки по наборам граней, слева направо. Третий элемент — исходные номера граней."""
    parts: list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]], list[int]]] = []
    for face_indices in face_sets:
        if not face_indices:
            continue
        remap: dict[int, int] = {}
        part_vertices: list[tuple[float, float, float]] = []
        part_faces: list[tuple[int, ...]] = []
        for face_index in face_indices:
            polygon = []
            for index in faces[face_index]:
                if index not in remap:
                    remap[index] = len(part_vertices)
                    part_vertices.append(vertices[index])
                polygon.append(remap[index])
            part_faces.append(tuple(polygon))
        parts.append((part_vertices, part_faces, list(face_indices)))
    parts.sort(key=lambda item: (min(vertex[0] for vertex in item[0]), min(vertex[1] for vertex in item[0])))
    return parts


def _sub_meshes(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    face_sets: list[list[int]],
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]]:
    """Сетки по наборам граней, слева направо (min x, затем min y)."""
    return [(part_vertices, part_faces) for part_vertices, part_faces, _faces in _mesh_parts(vertices, faces, face_sets)]


def polygon_tags_for_faces(
    face_indices: list[int],
    polygon_face_counts: list[int],
    polygon_areas: list[float],
) -> tuple[list[int], list[float]]:
    """Пятна в порядке граней одной сетки: число треугольников и площадь, м².

    Соседние пятна не склеиваются, даже если у них общее ребро.
    Без пары «число треугольников — площадь» списки пустые, и площадь считается по контуру.
    """
    if len(polygon_face_counts) != len(polygon_areas):
        return [], []
    owners: list[int] = []
    for polygon_index, count in enumerate(polygon_face_counts):
        owners.extend([polygon_index] * int(count))
    if not face_indices or any(index < 0 or index >= len(owners) for index in face_indices):
        return [], []
    counts: list[int] = []
    areas: list[float] = []
    current: int | None = None
    running = 0
    for face_index in face_indices:
        owner = owners[face_index]
        if owner != current:
            if current is not None:
                counts.append(running)
                areas.append(float(polygon_areas[current]) if current < len(polygon_areas) else 0.0)
            current = owner
            running = 0
        running += 1
    if current is not None:
        counts.append(running)
        areas.append(float(polygon_areas[current]) if current < len(polygon_areas) else 0.0)
    return counts, areas


def _polygon_face_sets(
    faces: list[tuple[int, ...]],
    polygon_faces: list[int] | None,
) -> list[list[int]]:
    counts = [int(count) for count in (polygon_faces or []) if int(count) > 0]
    if counts and sum(counts) == len(faces):
        face_sets: list[list[int]] = []
        start = 0
        for count in counts:
            face_sets.append(list(range(start, start + count)))
            start += count
        return face_sets
    return face_groups(faces)


def tagged_mesh_parts(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    split_mode: str,
    part_gap_m: float,
    polygon_face_counts: list[int],
    polygon_areas: list[float],
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]], list[int], list[float]]]:
    """Сетки выбранного режима и пятна каждой сетки в порядке её граней."""
    if not faces:
        return []
    if split_mode == "layer":
        meshes = [(vertices, faces, list(range(len(faces))))]
    elif split_mode == "polygon":
        meshes = _mesh_parts(vertices, faces, _polygon_face_sets(faces, polygon_face_counts))
    else:
        meshes = _gap_meshes(vertices, faces, part_gap_m)
    tagged = []
    for part_vertices, part_faces, face_indices in meshes:
        counts, areas = polygon_tags_for_faces(face_indices, polygon_face_counts, polygon_areas)
        tagged.append((part_vertices, part_faces, counts, areas))
    return tagged


def split_polygons(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    polygon_faces: list[int] | None = None,
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]]:
    """Каждый замкнутый полигон слоя — своя сетка.

    polygon_faces — число треугольников каждого полигона по порядку граней.
    Без него полигоном считается связный по рёбрам кусок сетки.
    """
    if not faces:
        return []
    return _sub_meshes(vertices, faces, _polygon_face_sets(faces, polygon_faces))


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    unique = sorted(set(points))
    if len(unique) <= 2:
        return unique

    def turn(origin, left, right) -> float:
        return (left[0] - origin[0]) * (right[1] - origin[1]) - (left[1] - origin[1]) * (right[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def minimum_rectangle(points: list[tuple[float, float]]) -> tuple[float, float, float, tuple[float, float]] | None:
    """Минимальный повёрнутый прямоугольник: ширина вдоль ребра, глубина поперёк, угол ребра, центр."""
    hull = convex_hull(points)
    if len(hull) < 2:
        return None
    best: tuple[float, float, float, float, tuple[float, float]] | None = None
    count = len(hull)
    for index in range(count):
        x1, y1 = hull[index]
        x2, y2 = hull[(index + 1) % count]
        edge = hypot(x2 - x1, y2 - y1)
        if edge < 1e-8:
            continue
        ux, uy = (x2 - x1) / edge, (y2 - y1) / edge
        vx, vy = -uy, ux
        along = [px * ux + py * uy for px, py in hull]
        across = [px * vx + py * vy for px, py in hull]
        span_u = max(along) - min(along)
        span_v = max(across) - min(across)
        area = span_u * span_v
        mid_u = (min(along) + max(along)) * 0.5
        mid_v = (min(across) + max(across)) * 0.5
        center = (mid_u * ux + mid_v * vx, mid_u * uy + mid_v * vy)
        if best is None or area < best[0]:
            best = (area, span_u, span_v, atan2(uy, ux), center)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def is_playground_layer(name: str) -> bool:
    """Покрытие площадки: в имени есть площадка, игровая, спорт или резина, и нет ТБО."""
    text = str(name or "").casefold()
    if "тбо" in text:
        return False
    return "площад" in text or "игров" in text or "спорт" in text or "резин" in text


def playground_fill_kind(name: str) -> str:
    """Слой со «спорт» в имени заполняется спортивными моделями."""
    return "sports" if "спорт" in str(name or "").casefold() else "children"


def scale_clearance(clearance, factor: float) -> tuple[float, float, float, float]:
    """1 оставляет заложенную полосу. Меньше единицы сужает все четыре стороны."""
    value = min(max(float(factor), 0.0), 1.0)
    front, back, left, right = clearance
    return (float(front) * value, float(back) * value, float(left) * value, float(right) * value)


def auto_playground_kinds(names: list[str], mode: str, seed: int) -> list[str]:
    """Тип каждого полигона. Случайный выбор повторяется при том же сиде и не смотрит на имя."""
    choice = str(mode or "random")
    if choice == "children":
        return ["children"] * len(names)
    if choice == "sports":
        return ["sports"] * len(names)
    rng = random.Random(int(seed))
    return ["sports" if rng.random() < 0.5 else "children" for _name in names]


def is_playground_surface(name: str, texture: str = "") -> bool:
    """Площадка по имени или по резиновому покрытию. ТБО не берём."""
    if "тбо" in str(name or "").casefold():
        return False
    if is_playground_layer(name):
        return True
    return str(texture or "") == "rubber"


def blocks_playground(
    category: str,
    placement: str,
    parent_placement: str,
    family: str,
    placed: bool,
) -> bool:
    """Забор, здание и уже стоящий МАФ мешают. Дерево и куст — нет."""
    if placement == "scatter" or parent_placement == "scatter" or family in {"tree", "bush"}:
        return False
    if placed:
        return (
            placement in {"area", "linear"}
            or parent_placement in {"area", "linear"}
            or family in {"playground", "bench"}
        )
    return category in {"fence", "building"}


def playground_capacity(area_m2: float) -> int:
    """Сколько моделей ещё имеет смысл пробовать. Место проверяет упаковщик."""
    count = int(float(area_m2) / 20.0)
    if count < 8:
        return 8
    if count > 40:
        return 40
    return count


def rank_playgrounds(
    zone_width: float,
    zone_depth: float,
    assets: list[dict],
) -> tuple[list[tuple[str, str, float]], float | None]:
    """Подходящие модели: id, ориентация A или B, заполнение. Иначе минимальный выход за габарит."""
    zone_area = max(zone_width * zone_depth, 1e-6)
    fitting: list[tuple[float, str, str]] = []
    closest_excess: float | None = None
    for asset in assets:
        width, depth = float(asset["footprint"][0]), float(asset["footprint"][1])
        options: list[tuple[str, float]] = []
        if width <= zone_width + FIT_SLACK_M and depth <= zone_depth + FIT_SLACK_M:
            options.append(("A", (width * depth) / zone_area))
        if width <= zone_depth + FIT_SLACK_M and depth <= zone_width + FIT_SLACK_M:
            options.append(("B", (width * depth) / zone_area))
        if options:
            orientation, coverage = max(options, key=lambda item: item[1])
            fitting.append((coverage, str(asset["id"]), orientation))
            continue
        excess = min(
            max(width - zone_width, depth - zone_depth, 0.0),
            max(width - zone_depth, depth - zone_width, 0.0),
        )
        if closest_excess is None or excess < closest_excess:
            closest_excess = excess
    fitting.sort(key=lambda item: item[0], reverse=True)
    return [(asset_id, orientation, coverage) for coverage, asset_id, orientation in fitting], closest_excess


def orientation_angle(edge_angle: float, orientation: str) -> float:
    """A: ширина модели вдоль ребра зоны. B: вперёд модели вдоль ребра."""
    if orientation == "B":
        return forward_angle(cos(edge_angle), sin(edge_angle))
    return edge_angle


def forward_angle(dx: float, dy: float) -> float:
    """Поворот вокруг Z, при котором локальная ось +Y смотрит вдоль вектора."""
    return atan2(-dx, dy)


def bench_facing(dx: float, dy: float) -> float:
    """Длинная сторона лавки вдоль линии, взгляд поперёк неё: соседние стоят боком друг к другу."""
    return forward_angle(dx, dy) + pi / 2


def bench_positions(
    start: tuple[float, float],
    end: tuple[float, float],
    step: float,
    depth: float,
) -> list[tuple[float, float]]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = hypot(dx, dy)
    if length < 1e-6 or step <= 0:
        return []
    ux, uy = dx / length, dy / length
    if length < step:
        return [((start[0] + end[0]) * 0.5, (start[1] + end[1]) * 0.5)]
    inset = max(depth, 0.0) * 0.5
    if length <= inset * 2:
        return [((start[0] + end[0]) * 0.5, (start[1] + end[1]) * 0.5)]
    span = length - inset * 2
    count = int(span // step) + 1
    used = (count - 1) * step
    lead = inset + (span - used) * 0.5
    return [
        (start[0] + ux * (lead + index * step), start[1] + uy * (lead + index * step))
        for index in range(count)
    ]


def mirrored_keep_from(xs: list[float]) -> float | None:
    """Порог X второй копии модели. Вершины левее порога — дубль по другую сторону линии."""
    if len(xs) < 8:
        return None
    ordered = sorted(xs)
    low, high = ordered[0], ordered[-1]
    span = high - low
    if span < 0.5:
        return None
    best_gap = 0.0
    best_cut: float | None = None
    for left, right in zip(ordered, ordered[1:]):
        gap = right - left
        middle = (left + right) * 0.5
        if middle < low + span * 0.25 or middle > high - span * 0.25:
            continue
        if gap > best_gap:
            best_gap = gap
            best_cut = right
    if best_cut is None or best_gap < span * 0.08:
        return None
    kept = sum(1 for value in ordered if value >= best_cut)
    if kept < len(ordered) * 0.2 or kept > len(ordered) * 0.8:
        return None
    return best_cut


def _catmull(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    t2 = t * t
    t3 = t2 * t
    def coord(index: int) -> float:
        return 0.5 * (
            2.0 * p1[index]
            + (-p0[index] + p2[index]) * t
            + (2.0 * p0[index] - 5.0 * p1[index] + 4.0 * p2[index] - p3[index]) * t2
            + (-p0[index] + 3.0 * p1[index] - 3.0 * p2[index] + p3[index]) * t3
        )
    return (coord(0), coord(1))


def smooth_path(points: list[tuple[float, float]], spacing: float = 0.25) -> list[tuple[float, float]]:
    """Две точки остаются отрезком. Дальше линия проходит через все клики плавной кривой."""
    if len(points) < 3:
        return list(points)
    dense = [points[0]]
    step = max(spacing, 0.05)
    for index in range(len(points) - 1):
        p0 = points[index - 1] if index > 0 else points[0]
        p1 = points[index]
        p2 = points[index + 1]
        p3 = points[index + 2] if index + 2 < len(points) else points[-1]
        chord = hypot(p2[0] - p1[0], p2[1] - p1[1])
        pieces = max(2, int(chord / step))
        for piece in range(1, pieces + 1):
            dense.append(_catmull(p0, p1, p2, p3, piece / pieces))
    return dense


def _path_length(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for start, end in zip(points, points[1:]):
        total += hypot(end[0] - start[0], end[1] - start[1])
    return total


def _sample_path(points: list[tuple[float, float]], distance: float) -> tuple[float, float, float, float]:
    travelled = 0.0
    last = points[0]
    for start, end in zip(points, points[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = hypot(dx, dy)
        if length < 1e-9:
            continue
        if travelled + length >= distance - 1e-8:
            ratio = min(max((distance - travelled) / length, 0.0), 1.0)
            return (start[0] + dx * ratio, start[1] + dy * ratio, dx, dy)
        travelled += length
        last = end
    dx = last[0] - points[0][0]
    dy = last[1] - points[0][1]
    return (last[0], last[1], dx, dy)


def _blend_paths(
    straight: list[tuple[float, float]],
    curved: list[tuple[float, float]],
    amount: float,
) -> list[tuple[float, float]]:
    """Одна и та же доля длины: отрезок и дуга смешиваются."""
    straight_len = _path_length(straight)
    curved_len = _path_length(curved)
    if straight_len < 1e-6 or curved_len < 1e-6:
        return list(straight)
    pieces = max(len(curved), 8)
    blended: list[tuple[float, float]] = []
    for index in range(pieces + 1):
        ratio = index / pieces
        sx, sy, _dx, _dy = _sample_path(straight, ratio * straight_len)
        cx, cy, _dx, _dy = _sample_path(curved, ratio * curved_len)
        blended.append((sx + (cx - sx) * amount, sy + (cy - sy) * amount))
    return blended


def bench_path(
    points: list[tuple[float, float]],
    step: float,
    depth: float,
    heading: str = "tangent",
    turn: float = 0.0,
    roundness: float = 1.0,
) -> list[tuple[float, float, float]]:
    """Точки и поворот лавок вдоль линии. Длинная сторона идёт по пути, взгляд — поперёк.

    roundness 0 — строго по отрезкам между кликами, 1 — плавная дуга.
    """
    amount = min(max(float(roundness), 0.0), 1.0)
    if amount <= 0.0 or len(points) < 3:
        path = list(points)
    elif amount >= 1.0:
        path = smooth_path(points)
    else:
        path = _blend_paths(points, smooth_path(points), amount)
    if len(path) < 2 or step <= 0:
        return []
    total = 0.0
    for start, end in zip(path, path[1:]):
        total += hypot(end[0] - start[0], end[1] - start[1])
    if total < 1e-6:
        return []
    if total < step:
        distances = [total * 0.5]
    else:
        inset = max(depth, 0.0) * 0.5
        if total <= inset * 2:
            distances = [total * 0.5]
        else:
            span = total - inset * 2
            count = int(span // step) + 1
            used = (count - 1) * step
            lead = inset + (span - used) * 0.5
            distances = [lead + index * step for index in range(count)]
    end_dx = path[-1][0] - path[0][0]
    end_dy = path[-1][1] - path[0][1]
    shared = bench_facing(end_dx, end_dy) if heading == "uniform" else None
    placed: list[tuple[float, float, float]] = []
    for distance in distances:
        x, y, dx, dy = _sample_path(path, distance)
        angle = shared if shared is not None else bench_facing(dx, dy)
        placed.append((x, y, angle + turn))
    return placed


def cluster_boxes(
    boxes: list[tuple[float, float, float, float]],
    gap: float,
) -> list[list[int]]:
    """Группы прямоугольников с зазором не больше gap.

    Связь транзитивная: цепочка соседних пятен одного чертежа остаётся
    одной группой. Группы идут слева направо по левому нижнему углу.
    """
    count = len(boxes)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def unite(left: int, right: int) -> None:
        root_a, root_b = find(left), find(right)
        if root_a != root_b:
            parent[root_a] = root_b

    distance = max(float(gap), 0.0)
    limit = distance * distance
    active: list[int] = []
    for left in sorted(range(count), key=lambda index: (boxes[index][0], index)):
        ax0, ay0, ax1, ay1 = boxes[left]
        active = [index for index in active if boxes[index][2] >= ax0 - distance]
        for right in active:
            bx0, by0, bx1, by1 = boxes[right]
            dx = max(0.0, ax0 - bx1, bx0 - ax1)
            dy = max(0.0, ay0 - by1, by0 - ay1)
            if dx * dx + dy * dy <= limit:
                unite(left, right)
        active.append(left)

    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)

    def origin(members: list[int]) -> tuple[float, float]:
        return (
            min(boxes[index][0] for index in members),
            min(boxes[index][1] for index in members),
        )

    return [members for _root, members in sorted(groups.items(), key=lambda item: origin(item[1]))]


def small_sheet_indexes(counts: list[int], keep_ratio: float = 0.75) -> list[int]:
    """Индексы групп, в которых объектов строго меньше keep_ratio от самой большой.

    Самая большая остаётся. Группа с долей keep_ratio и выше тоже остаётся.
    """
    if not counts:
        return []
    largest = max(int(count) for count in counts)
    if largest <= 0:
        return []
    limit = largest * float(keep_ratio)
    return [index for index, count in enumerate(counts) if int(count) < limit]


def choose_plant_names(selected: list[str], remembered: list[str]) -> list[str]:
    """Полигоны для посадок: непустое выделение, иначе прошлый набор.

    Активный объект и слой из списка сюда не передаются, поэтому пустое
    выделение не подменяет запомненные полигоны.
    """
    def unique(names: list[str]) -> list[str]:
        chosen: list[str] = []
        seen: set[str] = set()
        for name in names:
            text = str(name or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            chosen.append(text)
        return chosen

    picked = unique(selected)
    if picked:
        return picked
    return unique(remembered)


def impact_margin(height: float) -> float:
    """Зазор зоны падения по ГОСТ 34614.1: до 1.5 м падения — 1.5 м, дальше не больше 2.5 м."""
    fall = min(max(float(height), 0.0), 3.0)
    if fall <= 1.5:
        return 1.5
    return min(1.5 + (2.0 / 3.0) * (fall - 1.5), 2.5)


def _rotate(x: float, y: float, angle: float, origin: tuple[float, float]) -> tuple[float, float]:
    ca, sa = cos(angle), sin(angle)
    return (origin[0] + x * ca - y * sa, origin[1] + x * sa + y * ca)


def equipment_quads(
    width: float,
    depth: float,
    clearance: tuple[float, float, float, float],
    angle: float,
    origin: tuple[float, float],
    inflate: float = 0.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Коробка модели и зона безопасности. clearance: вперёд, назад, влево, вправо."""
    front, back, left, right = clearance
    pad = max(float(inflate), 0.0)
    body = [
        _rotate(-width * 0.5 - pad, -depth * 0.5 - pad, angle, origin),
        _rotate(width * 0.5 + pad, -depth * 0.5 - pad, angle, origin),
        _rotate(width * 0.5 + pad, depth * 0.5 + pad, angle, origin),
        _rotate(-width * 0.5 - pad, depth * 0.5 + pad, angle, origin),
    ]
    safety = [
        _rotate(-(width * 0.5 + left), -(depth * 0.5 + back), angle, origin),
        _rotate(width * 0.5 + right, -(depth * 0.5 + back), angle, origin),
        _rotate(width * 0.5 + right, depth * 0.5 + front, angle, origin),
        _rotate(-(width * 0.5 + left), depth * 0.5 + front, angle, origin),
    ]
    return body, safety


_SHAPE = None


def _shapely():
    """Библиотека ставится кнопкой после включения аддона, поэтому импорт не на загрузке."""
    global _SHAPE
    if _SHAPE is not None:
        return _SHAPE
    vendor = Path(__file__).resolve().parent / "vendor"
    folder = str(vendor)
    if vendor.is_dir() and folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        from shapely.geometry import Point, Polygon
        from shapely.ops import unary_union
        from shapely.prepared import prep
    except ImportError as error:
        raise RuntimeError(
            "Сначала нажмите «Установить библиотеки» на панели «Подложка»."
        ) from error
    _SHAPE = (Point, Polygon, unary_union, prep)
    return _SHAPE


class _Triangles:
    """Единая GEOS-геометрия слоя: быстрые и точные предикаты вместо выборочных точек."""

    def __init__(self, vertices: list[tuple[float, float, float]], faces: list[tuple[int, ...]]):
        Point, Polygon, unary_union, prep = _shapely()
        polygons = [
            Polygon(((a[0], a[1]), (b[0], b[1]), (c[0], c[1])))
            for a, b, c, _weight in _mesh_triangles(vertices, faces)
        ]
        self.geometry = unary_union(polygons) if polygons else Polygon()
        self._prepared = prep(self.geometry)

    def contains(self, x: float, y: float) -> bool:
        Point, _Polygon, _union, _prep = _shapely()
        return self._prepared.covers(Point(x, y))

    def covers(self, quad: list[tuple[float, float]]) -> bool:
        _Point, Polygon, _union, _prep = _shapely()
        return self._prepared.covers(Polygon(quad))

    def intersects(self, quad: list[tuple[float, float]]) -> bool:
        _Point, Polygon, _union, _prep = _shapely()
        return self._prepared.intersects(Polygon(quad))


def _axes(poly: list[tuple[float, float]]) -> list[tuple[float, float]]:
    found = []
    count = len(poly)
    for index in range(count):
        x1, y1 = poly[index]
        x2, y2 = poly[(index + 1) % count]
        found.append((y1 - y2, x2 - x1))
    return found


def _project(poly: list[tuple[float, float]], axis: tuple[float, float]) -> tuple[float, float]:
    dots = [point[0] * axis[0] + point[1] * axis[1] for point in poly]
    return min(dots), max(dots)


def quads_overlap(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> bool:
    for axis in _axes(left) + _axes(right):
        a0, a1 = _project(left, axis)
        b0, b1 = _project(right, axis)
        if a1 <= b0 + 1e-7 or b1 <= a0 + 1e-7:
            return False
    return True


# Ниже этой площади пересечение — общая кромка или численный шум, не наложение карты.
FOOTPRINT_SLIVER_M2 = 1e-6
# Два растения можно поставить, пока пересечение не больше этой доли меньшего следа.
PLANT_OVERLAP_LIMIT = 0.30
# Стартовые значения и для кнопки, и для авторасстановки: оба читают одни свойства сцены.
DEFAULT_PLANT_DENSITY = 1.0  # шт/100 м²
DEFAULT_TREE_SHARE = 30.0  # проценты


def adopt_plant_defaults(density: float, share: float) -> tuple[float, float]:
    """Сохранённые плотность и долю деревьев не переписывает.

    Новая сцена берёт стартовые 1 шт/100 м² и 30% из свойства. Уже выбранные
    3 и 10, как и любое другое число, остаются как есть.
    """
    return float(density), float(share)


def plant_slot_count(area_m2: float, per_square_meter: float) -> int:
    """Сколько слотов просит плотность. Половина округляется вверх.

    Ноль остаётся нулём: мелкий газон при редкой плотности остаётся пустым.
    Минимума в одно растение нет.
    """
    if area_m2 <= 0.0 or per_square_meter <= 0.0:
        return 0
    return int(float(area_m2) * float(per_square_meter) + 0.5)


def slot_attempt_families(choose, attempts: int) -> list:
    """Семейство слота выбирается один раз. Каждая попытка несёт тот же выбор.

    choose() вызывается ровно один раз. Повтор двигает точку, а не подменяет дерево кустом.
    """
    if attempts <= 0:
        return []
    family = choose()
    return [family for _ in range(int(attempts))]


def oriented_square(
    center: tuple[float, float],
    side: float,
    yaw: float,
) -> list[tuple[float, float]]:
    """Квадрат плана с центром в точке, стороной side и рысканьем вокруг Z."""
    half = float(side) * 0.5
    origin = (float(center[0]), float(center[1]))
    angle = float(yaw)
    return [
        _rotate(-half, -half, angle, origin),
        _rotate(half, -half, angle, origin),
        _rotate(half, half, angle, origin),
        _rotate(-half, half, angle, origin),
    ]


def plant_footprint(
    center: tuple[float, float],
    plan_side: float,
    yaw: float,
    plant_scale: float,
) -> list[tuple[float, float]]:
    """След модели: сторона сетки после импорта, умноженная на размер посадки."""
    return oriented_square(center, float(plan_side) * float(plant_scale), yaw)


def _as_polygon(shape):
    if hasattr(shape, "geom_type") and hasattr(shape, "area"):
        return shape
    _point, polygon, _union, _prep = _shapely()
    geometry = polygon(list(shape))
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return geometry


def polygon_overlap_area(left, right) -> float:
    """Площадь пересечения двух контуров.

    Меньше FOOTPRINT_SLIVER_M2 (1e-6 м²) считается нулём: общая кромка и
    щепка от округления не должны мигать. Касание ребра с нулевой площадью
    тоже нуль. Наложение карты от порога и выше возвращается как есть.
    """
    left_polygon = _as_polygon(left)
    right_polygon = _as_polygon(right)
    if left_polygon.is_empty or right_polygon.is_empty:
        return 0.0
    if not left_polygon.intersects(right_polygon):
        return 0.0
    shared = float(left_polygon.intersection(right_polygon).area)
    if shared < FOOTPRINT_SLIVER_M2:
        return 0.0
    return shared


def footprint_overlap_fraction(left, right) -> float:
    """Площадь пересечения, делённая на площадь меньшего контура."""
    left_polygon = _as_polygon(left)
    right_polygon = _as_polygon(right)
    smaller = min(float(left_polygon.area), float(right_polygon.area))
    if smaller <= 0.0:
        return 0.0
    return polygon_overlap_area(left_polygon, right_polygon) / smaller


def footprints_overlap_too_much(left, right, limit: float = PLANT_OVERLAP_LIMIT) -> bool:
    """True, если пересечение строго больше limit от меньшего следа."""
    return footprint_overlap_fraction(left, right) > float(limit)


def _unite_rings(rings: list[list[tuple[float, float]]]):
    _point, _polygon, unary_union, _prep = _shapely()
    polygons = []
    for ring in rings:
        if ring is None or len(ring) < 3:
            continue
        geometry = _as_polygon(ring)
        if geometry.is_empty or float(geometry.area) <= 0.0:
            continue
        polygons.append(geometry)
    if not polygons:
        return None
    return unary_union(polygons)


class PlantObstacles:
    """Здания и покрытия, собранные один раз на проход расстановки.

    Здание закрывает и дерево, и куст. Покрытие закрывает только куст,
    пока для дерева не включён запрет заходить на полигоны.
    Полигон, на котором уже стоит дом, закрывает только дерево.
    Порог площади — FOOTPRINT_SLIVER_M2, см. polygon_overlap_area.
    """

    def __init__(
        self,
        buildings: list[list[tuple[float, float]]] | None = None,
        pavements: list[list[tuple[float, float]]] | None = None,
        hosts: list[list[tuple[float, float]]] | None = None,
    ) -> None:
        _point, _polygon, _union, prep = _shapely()
        self.buildings = _unite_rings(list(buildings or []))
        self.pavements = _unite_rings(list(pavements or []))
        self.hosts = _unite_rings(list(hosts or []))
        self._buildings = prep(self.buildings) if self.buildings is not None else None
        self._pavements = prep(self.pavements) if self.pavements is not None else None
        self._hosts = prep(self.hosts) if self.hosts is not None else None

    def _hits(self, geometry, prepared, quad: list[tuple[float, float]]) -> bool:
        if geometry is None or prepared is None:
            return False
        _point, polygon, _union, _prep = _shapely()
        footprint = polygon(quad)
        if not prepared.intersects(footprint):
            return False
        return polygon_overlap_area(geometry, footprint) > 0.0

    def blocks(self, quad: list[tuple[float, float]], family: str, avoid_ground: bool = False) -> bool:
        if self._hits(self.buildings, self._buildings, quad):
            return True
        if family == "tree" and self._hits(self.hosts, self._hosts, quad):
            return True
        if family == "bush" or (family == "tree" and avoid_ground):
            return self._hits(self.pavements, self._pavements, quad)
        return False


def point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    """Точка внутри кольца. Горизонтальное ребро границу не двигает."""
    if len(ring) < 3:
        return False
    x, y = float(point[0]), float(point[1])
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if (y1 > y) != (y2 > y):
            cross = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < cross:
                inside = not inside
        previous = current
    return inside


def host_rings(
    groups: list[list[list[tuple[float, float]]]],
    points: list[tuple[float, float]],
) -> list[list[tuple[float, float]]]:
    """Все кольца объекта, если хотя бы одно содержит точку стоящего дома."""
    kept: list[list[tuple[float, float]]] = []
    for rings in groups:
        if any(point_in_ring(point, ring) for ring in rings for point in points):
            kept.extend(rings)
    return kept


class OccupiedFootprints:
    """Следы уже принятых растений. Один набор на все газоны прохода."""

    def __init__(self) -> None:
        self._items: list[tuple[tuple[float, float, float, float], object]] = []

    def blocks(self, quad: list[tuple[float, float]]) -> bool:
        polygon = _as_polygon(quad)
        if polygon.is_empty or float(polygon.area) <= 0.0:
            return True
        box = polygon.bounds
        for other_box, other in self._items:
            if other_box[2] < box[0] or box[2] < other_box[0] or other_box[3] < box[1] or box[3] < other_box[1]:
                continue
            if footprints_overlap_too_much(polygon, other):
                return True
        return False

    def add(self, quad: list[tuple[float, float]]) -> None:
        polygon = _as_polygon(quad)
        self._items.append((polygon.bounds, polygon))


def _quad_inside(quad: list[tuple[float, float]], ground: _Triangles, blocked: _Triangles | None) -> bool:
    if not ground.covers(quad):
        return False
    return blocked is None or not blocked.intersects(quad)


def _safety_area(asset: dict) -> float:
    front, back, left, right = asset["clearance"]
    return (float(asset["width"]) + left + right) * (float(asset["depth"]) + front + back)


def pack_playground(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    assets: list[dict],
    blocked: tuple[list[tuple[float, float, float]], list[tuple[int, ...]]] | None = None,
    step: float = 0.5,
    limit: int = 12,
    seed: int = 0,
) -> list[dict]:
    """Несколько моделей внутрь полигона. Сид меняет выбор и позиции без ослабления зон безопасности."""
    if not assets or not faces or step <= 0 or limit <= 0:
        return []
    rng = random.Random(int(seed))
    flat = [(vertex[0], vertex[1]) for vertex in vertices]
    rectangle = minimum_rectangle(flat)
    if rectangle is None:
        return []
    width, depth, edge_angle, center = rectangle
    if width < 0.5 or depth < 0.5:
        return []
    ground = _Triangles(vertices, faces)
    obstacles = _Triangles(*blocked) if blocked and blocked[1] else None
    cells = (width / step + 1.0) * (depth / step + 1.0)
    if cells > 8000.0:
        step *= (cells / 8000.0) ** 0.5
    ux, uy = cos(edge_angle), sin(edge_angle)
    vx, vy = -uy, ux
    grid: list[tuple[float, float]] = []
    u = -width * 0.5
    while u <= width * 0.5 + 1e-6:
        v = -depth * 0.5
        while v <= depth * 0.5 + 1e-6:
            point = (center[0] + u * ux + v * vx, center[1] + u * uy + v * vy)
            if ground.contains(point[0], point[1]) and (obstacles is None or not obstacles.contains(point[0], point[1])):
                grid.append(point)
            v += step
        u += step
    if not grid:
        return []
    rng.shuffle(grid)
    ordered = list(assets)
    rng.shuffle(ordered)
    placed: list[dict] = []

    def try_place(asset: dict) -> bool:
        if len(placed) >= limit:
            return False
        for orientation in ("A", "B"):
            angle = orientation_angle(edge_angle, orientation)
            for point in grid:
                _gap, safety = equipment_quads(
                    float(asset["width"]), float(asset["depth"]), asset["clearance"], angle, point,
                )
                gap, _safety = equipment_quads(
                    float(asset["width"]), float(asset["depth"]), asset["clearance"], angle, point, inflate=0.25,
                )
                if not _quad_inside(safety, ground, obstacles):
                    continue
                motion = str(asset.get("motion") or "static")
                conflict = False
                for other in placed:
                    if quads_overlap(gap, other["gap"]):
                        conflict = True
                        break
                    moving = motion in {"swing", "slide"} or other["motion"] in {"swing", "slide"}
                    if moving and quads_overlap(safety, other["safety"]):
                        conflict = True
                        break
                if conflict:
                    continue
                placed.append({
                    "id": asset["id"],
                    "x": point[0],
                    "y": point[1],
                    "angle": angle,
                    "orientation": orientation,
                    "motion": motion,
                    "gap": gap,
                    "safety": safety,
                })
                return True
        return False

    for asset in ordered:
        try_place(asset)
    progressed = True
    while progressed and len(placed) < limit:
        progressed = False
        for asset in ordered:
            if try_place(asset):
                progressed = True
                break
    return [
        {"id": item["id"], "x": item["x"], "y": item["y"], "angle": item["angle"], "orientation": item["orientation"]}
        for item in placed
    ]


def play_group(asset: dict) -> str:
    """Вид оборудования для одного кармана: качели отдельно от качалок и горки."""
    asset_id = str(asset.get("id") or "")
    motion = str(asset.get("motion") or "")
    if asset_id.startswith("swing") or motion == "swing":
        return "swing"
    if asset_id.startswith("rocker"):
        return "rocker"
    if asset_id.startswith("sandbox"):
        return "sandbox"
    if asset_id.startswith("house"):
        return "house"
    if asset_id.startswith("slide") or motion == "slide":
        return "slide"
    if asset_id.startswith("gym"):
        return "gym"
    if asset_id.startswith("bar"):
        return "bar"
    return "complex"


def playground_clearance(asset: dict) -> tuple[float, float, float, float]:
    """Полоса зоны падения по ГОСТ 34614.1. Качели оставляют явную добавку по ходу качания.

    clearance в каталоге — перекрытие, а не источник. Без высоты падения остаётся
    записанная четвёрка, чтобы старые вызовы не потеряли зону.
    """
    motion = str(asset.get("motion") or "static")
    stored = asset.get("clearance")
    has_stored = isinstance(stored, (list, tuple)) and len(stored) >= 4
    if (motion == "swing" or asset.get("clearance_override")) and has_stored:
        return tuple(float(value) for value in stored[:4])
    if "fall_height" in asset and asset.get("fall_height") is not None:
        fall = min(max(float(asset["fall_height"]), 0.0), 3.0)
    elif has_stored:
        return tuple(float(value) for value in stored[:4])
    else:
        footprint = asset.get("footprint") or (0.0, 0.0, 0.0)
        fall = min(max(float(footprint[2]), 0.0), 3.0)
    margin = impact_margin(fall)
    front = back = left = right = margin
    if motion == "slide":
        extra = max(float(asset.get("runout") or 0.0), 0.0)
        side = str(asset.get("runout_side") or "front")
        if side == "back":
            back += extra
        elif side == "left":
            left += extra
        elif side == "right":
            right += extra
        else:
            front += extra
    return (front, back, left, right)


def _geometry_parts(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    kind = geometry.geom_type
    if kind == "Polygon":
        return [geometry]
    if kind in {"MultiPolygon", "GeometryCollection"}:
        found = []
        for part in geometry.geoms:
            found.extend(_geometry_parts(part))
        return found
    return []


def _candidate_points(polygon, limit: int = 8) -> list[tuple[float, float, float]]:
    """Точки внутри полигона, сначала самые дальние от границы."""
    Point, _polygon, _union, _prep = _shapely()
    min_x, min_y, max_x, max_y = polygon.bounds
    span = max(max_x - min_x, max_y - min_y, 1.0)
    step = max(span / 28.0, 0.45)
    boundary = polygon.boundary
    center = polygon.centroid
    found: list[tuple[float, float, float, float]] = []
    y = min_y
    while y <= max_y + 1e-6:
        x = min_x
        while x <= max_x + 1e-6:
            point = Point(x, y)
            if polygon.covers(point):
                found.append((point.distance(boundary), -hypot(x - center.x, y - center.y), x, y))
            x += step
        y += step
    if not found:
        point = polygon.representative_point()
        return [(float(point.x), float(point.y), 0.0)]
    found.sort(reverse=True)
    _distance, _center_bias, best_x, best_y = found[0]
    fine = max(step / 5.0, 0.2)
    for row in range(-4, 5):
        for column in range(-4, 5):
            px = best_x + column * fine
            py = best_y + row * fine
            point = Point(px, py)
            if polygon.covers(point):
                found.append((point.distance(boundary), -hypot(px - center.x, py - center.y), px, py))
    found.sort(reverse=True)
    chosen: list[tuple[float, float, float]] = []
    for distance, _center_bias, x, y in found:
        if any(hypot(x - other_x, y - other_y) < step * 0.75 for _other, other_x, other_y in chosen):
            continue
        chosen.append((x, y, distance))
        if len(chosen) >= limit:
            break
    return chosen or [(best_x, best_y, found[0][0])]


def _compose_spec(asset: dict) -> dict:
    if "width" in asset and "depth" in asset:
        width, depth = float(asset["width"]), float(asset["depth"])
    else:
        footprint = asset["footprint"]
        width, depth = float(footprint[0]), float(footprint[1])
    stored = asset.get("clearance")
    if isinstance(stored, (list, tuple)) and len(stored) >= 4:
        clearance = tuple(float(value) for value in stored[:4])
    else:
        clearance = playground_clearance(asset)
    fall = asset.get("fall_height")
    return {
        "id": str(asset.get("id") or "item"),
        "width": width,
        "depth": depth,
        "clearance": clearance,
        "motion": str(asset.get("motion") or "static"),
        "role": str(asset.get("role") or "activity"),
        "fall_height": float(fall) if fall is not None else 0.0,
        "group": str(asset.get("group") or play_group(asset)),
    }


def _try_sit(spec: dict, point: tuple[float, float], angle: float, orientation: str, usable, placed: list[dict]):
    _point, polygon, _union, _prep = _shapely()
    gap, safety = equipment_quads(
        spec["width"], spec["depth"], spec["clearance"], angle, point,
    )
    if not usable.buffer(1e-3).covers(polygon(safety)):
        return None
    for other in placed:
        if quads_overlap(gap, other["gap"]) or quads_overlap(safety, other["safety"]):
            return None
    return {
        "id": spec["id"],
        "x": float(point[0]),
        "y": float(point[1]),
        "angle": float(angle),
        "orientation": orientation,
        "motion": spec["motion"],
        "role": spec["role"],
        "fall_height": spec["fall_height"],
        "group": spec["group"],
        "gap": gap,
        "safety": safety,
    }


def _seat_angles(edge_angle: float) -> list[tuple[float, str]]:
    """Вдоль длинной стороны, поперёк и разворот каждой на 180°."""
    along = float(edge_angle)
    across = orientation_angle(along, "B")
    return [
        (along, "A"),
        (along + pi, "A"),
        (across, "B"),
        (across + pi, "B"),
    ]


def _snap_axis(angle: float) -> float:
    """Длинная сторона ближе 10° к мировой оси становится 0° или 90°."""
    turned = float(angle) % pi
    limit = pi / 18.0
    if min(turned, pi - turned) <= limit:
        return 0.0
    if abs(turned - pi / 2.0) <= limit:
        return pi / 2.0
    return turned


def _playground_axis(usable) -> float:
    """Угол длинной стороны всего покрытия, не отдельного кармана."""
    parts = _geometry_parts(usable)
    if not parts:
        return 0.0
    part = max(parts, key=lambda item: item.area)
    rectangle = minimum_rectangle([(float(x), float(y)) for x, y in part.exterior.coords])
    if rectangle is None:
        return 0.0
    span_u, span_v, angle, _center = rectangle
    if span_v > span_u:
        angle += pi / 2.0
    return _snap_axis(angle)


def _facing_pair(angle: float, orientation: str) -> list[tuple[float, str]]:
    return [(float(angle), orientation), (float(angle) + pi, orientation)]


def _collect_seats(spec: dict, pocket, usable, placed: list[dict], angles: list[tuple[float, str]]) -> list[tuple[float, dict]]:
    seats: list[tuple[float, dict]] = []
    for x, y, radius in _candidate_points(pocket, limit=12):
        for angle, orientation in angles:
            found = _try_sit(spec, (x, y), angle, orientation, usable, placed)
            if found is not None:
                seats.append((radius, found))
    return seats


def _feasible_seats(spec: dict, pocket, usable, placed: list[dict], axis: float | None = None) -> list[tuple[float, dict]]:
    """Сначала вдоль оси покрытия, затем поперёк. Чужой угол кармана — только если оба не влезли."""
    if axis is not None:
        along = _collect_seats(spec, pocket, usable, placed, _facing_pair(axis, "A"))
        if along:
            return along
        across = _collect_seats(spec, pocket, usable, placed, _facing_pair(orientation_angle(axis, "B"), "B"))
        if across:
            return across
    rectangle = minimum_rectangle([(float(x), float(y)) for x, y in pocket.exterior.coords])
    if rectangle is None:
        return []
    _zone_width, _zone_depth, edge_angle, _center = rectangle
    return _collect_seats(spec, pocket, usable, placed, _seat_angles(edge_angle))


def _pick_seat(seats: list[tuple[float, dict]], rng: random.Random) -> dict | None:
    """Сид берёт позу не хуже 70% лучшего зазора: в широкой части, но не в одной точке."""
    if not seats:
        return None
    best = max(radius for radius, _pose in seats)
    floor = best * 0.7
    pool = [pose for radius, pose in seats if radius >= floor - 1e-6]
    return rng.choice(pool or [pose for _radius, pose in seats])


def _sit_in(spec: dict, pocket, usable, placed: list[dict], rng: random.Random, axis: float | None = None):
    return _pick_seat(_feasible_seats(spec, pocket, usable, placed, axis), rng)


def _pad_kind(spec: dict) -> str | None:
    """Пятно покрытия нужно при падении выше 0.6 м или при принудительном движении."""
    fall = float(spec.get("fall_height") or 0.0)
    motion = str(spec.get("motion") or "static")
    if fall <= 0.6 and motion == "static":
        return None
    if motion in {"swing", "slide"} or spec.get("role") == "hero":
        return "rubber"
    return "sand"


def _satellite_budget(area: float, pockets: int, groups: int) -> int:
    if area < 140.0 or pockets <= 0 or groups <= 0:
        return 0
    return min(4, pockets, groups, max(1, int(area / 300.0)))


def _group_lists(specs: list[dict], rng: random.Random) -> list[list[dict]]:
    ordered = list(specs)
    rng.shuffle(ordered)
    groups: list[list[dict]] = []
    index: dict[str, int] = {}
    for spec in ordered:
        slot = index.get(spec["group"])
        if slot is None:
            index[spec["group"]] = len(groups)
            groups.append([spec])
        else:
            groups[slot].append(spec)
    return groups


def _pockets_after(usable, placed: list[dict], limit: int = 4) -> list:
    """Отдельные широкие места вокруг уже стоящих зон, а не одно кольцо на всех."""
    Point, polygon, union, _prep = _shapely()
    region = usable
    if placed:
        block = union([polygon(item["safety"]).buffer(0.4) for item in placed])
        region = usable.difference(block)
    pockets = []
    for _slot in range(limit):
        parts = [part for part in _geometry_parts(region) if part.area >= 12.0]
        if not parts:
            break
        part = max(parts, key=lambda item: (_candidate_points(item, limit=1)[0][2], item.area))
        x, y, radius = _candidate_points(part, limit=1)[0]
        if radius < 1.2:
            break
        local = part.intersection(Point(x, y).buffer(max(radius * 1.8, 6.0)))
        local_parts = [item for item in _geometry_parts(local) if item.area >= 8.0]
        pockets.append(max(local_parts, key=lambda item: item.area) if local_parts else part)
        region = region.difference(Point(x, y).buffer(max(radius, 4.0)))
    return pockets


def _urn_beside(
    ground,
    blocked: list,
    pose: dict,
    bench_width: float,
    urn_width: float,
    urn_depth: float,
    signs: tuple[float, float] = (1.0, -1.0),
) -> dict | None:
    """Урна у торца лавки, на той же линии. Второй торец, если первый не влез."""
    if urn_width <= 0.0 or urn_depth <= 0.0:
        return None
    angle = float(pose["angle"])
    tangent = (cos(angle), sin(angle))
    shift = bench_width * 0.5 + 0.15 + urn_width * 0.5
    _point, polygon, _union, _prep = _shapely()
    for sign in signs:
        point = (pose["x"] + tangent[0] * shift * sign, pose["y"] + tangent[1] * shift * sign)
        gap, _safety = equipment_quads(urn_width, urn_depth, (0.0, 0.0, 0.0, 0.0), angle, point)
        if not ground.buffer(1e-3).covers(polygon(gap)):
            continue
        if any(quads_overlap(gap, other) for other in blocked):
            continue
        return {"x": float(point[0]), "y": float(point[1]), "angle": angle}
    return None


def _free_midpoint(
    start: tuple[float, float],
    end: tuple[float, float],
    inward: tuple[float, float],
    inset: float,
    width: float,
    depth: float,
    ground,
    blocked: list,
    angle: float,
):
    """Самый длинный отрезок края, куда тело лавки влезает целиком."""
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    length = hypot(dx, dy)
    if length < width:
        return None
    ux, uy = dx / length, dy / length
    margin = width * 0.5
    step = 0.4
    distances: list[float] = []
    cursor = margin
    while cursor <= length - margin + 1e-6:
        distances.append(cursor)
        cursor += step
    if not distances:
        distances = [length * 0.5]
    _point, polygon, _union, _prep = _shapely()
    fitting: list[tuple[int, tuple[float, float], list]] = []
    for index, distance in enumerate(distances):
        point = (
            float(start[0]) + ux * distance + inward[0] * inset,
            float(start[1]) + uy * distance + inward[1] * inset,
        )
        gap, _safety = equipment_quads(width, depth, (0.0, 0.0, 0.0, 0.0), angle, point)
        if not ground.buffer(1e-3).covers(polygon(gap)):
            continue
        if any(quads_overlap(gap, other) for other in blocked):
            continue
        fitting.append((index, point, gap))
    if not fitting:
        return None
    best: list[tuple[int, tuple[float, float], list]] = []
    run: list[tuple[int, tuple[float, float], list]] = []
    previous = -2
    for item in fitting:
        if item[0] != previous + 1:
            if len(run) > len(best):
                best = run
            run = []
        run.append(item)
        previous = item[0]
    if len(run) > len(best):
        best = run
    return [(item[1], item[2]) for item in best]


def _compose_benches(
    ground,
    hero: tuple[float, float] | None,
    placed: list[dict],
    bench: dict,
    rng: random.Random,
    station: float = 0.5,
) -> tuple[list[dict], list[dict]]:
    """Пара на каждом внешнем краю, который смотрит внутрь и длиннее лавки.

    Сид выбирает одну долю края для всех лавок. Сиденье смотрит внутрь.
    """
    del rng
    width = float(bench.get("width") or 1.82)
    depth = float(bench.get("depth") or 0.68)
    urn_width = float(bench.get("urn_width") or 0.0)
    urn_depth = float(bench.get("urn_depth") or 0.0)
    inset = depth * 0.5 + 0.2
    center = ground.centroid
    focus = hero if hero is not None else (float(center.x), float(center.y))
    parts = [part for part in _geometry_parts(ground) if part.area >= 30.0]
    if not parts:
        return [], []
    Point, polygon, _union, _prep = _shapely()
    edges = []
    for part in parts:
        others = [item for item in parts if item is not part]
        ring = list(part.exterior.coords)
        for index, (start, end) in enumerate(zip(ring, ring[1:])):
            dx = float(end[0]) - float(start[0])
            dy = float(end[1]) - float(start[1])
            length = hypot(dx, dy)
            if length < width:
                continue
            mid = ((float(start[0]) + float(end[0])) * 0.5, (float(start[1]) + float(end[1])) * 0.5)
            left = (-dy / length, dx / length)
            probe = Point(mid[0] + left[0] * 0.3, mid[1] + left[1] * 0.3)
            inward = left if part.covers(probe) else (-left[0], -left[1])
            toward = (focus[0] - mid[0], focus[1] - mid[1])
            if toward[0] * inward[0] + toward[1] * inward[1] <= 0.0:
                continue
            outward = (-inward[0], -inward[1])
            if any(
                other.buffer(0.3).covers(Point(mid[0] + outward[0] * distance, mid[1] + outward[1] * distance))
                for other in others
                for distance in (1.2, 2.4, 3.6)
            ):
                continue
            edges.append((length, index, (float(start[0]), float(start[1])), (float(end[0]), float(end[1])), inward))
    edges.sort(key=lambda item: (-item[0], item[1]))
    poses: list[dict] = []
    urns: list[dict] = []
    blocked = [item["safety"] for item in placed]
    for _length, _index, start, end, inward in edges:
        angle = forward_angle(inward[0], inward[1])
        run = _free_midpoint(start, end, inward, inset, width, depth, ground, blocked, angle)
        if not run:
            continue
        slot = int(round((len(run) - 1) * min(max(float(station), 0.0), 1.0)))
        order = sorted(range(len(run)), key=lambda index: (abs(index - slot), index))
        chosen = None
        for index in order:
            point, gap = run[index]
            pose = {"x": float(point[0]), "y": float(point[1]), "angle": float(angle)}
            urn = _urn_beside(ground, blocked + [gap], pose, width, urn_width, urn_depth, (1.0, -1.0))
            if urn is None and urn_width > 0.0:
                continue
            chosen = (pose, gap, urn)
            break
        if chosen is None:
            continue
        pose, gap, urn = chosen
        poses.append(pose)
        blocked.append(gap)
        if urn is None:
            continue
        urns.append(urn)
        urn_gap, _urn_safety = equipment_quads(
            urn_width, urn_depth, (0.0, 0.0, 0.0, 0.0), urn["angle"], (urn["x"], urn["y"]),
        )
        blocked.append(urn_gap)
    return poses, urns


def _safety_area(spec: dict) -> float:
    front, back, left, right = spec["clearance"]
    return (float(spec["width"]) + left + right) * (float(spec["depth"]) + front + back)


def _place_compact(specs: list[dict], usable, placed: list[dict], rng: random.Random, axis: float) -> dict | None:
    """Самая компактная модель, чья зона влезает в оставшийся полигон. Ставится одна."""
    parts = _geometry_parts(usable)
    if not parts:
        return None
    ground = max(parts, key=lambda item: item.area)
    for spec in sorted(specs, key=_safety_area):
        found = _sit_in(spec, ground, usable, placed, rng, axis)
        if found is not None:
            return found
    return None


_MAX_PLAY_ITEMS = 16


def _model_spans(spec: dict, orientation: str) -> tuple[float, float]:
    """Полный габарит зоны вдоль оси покрытия и поперёк неё."""
    front, back, left, right = spec["clearance"]
    span_x = float(spec["width"]) + float(left) + float(right)
    span_y = float(spec["depth"]) + float(front) + float(back)
    if orientation == "B":
        return span_y, span_x
    return span_x, span_y


def _axis_attempts(axis: float, across_first: bool = False) -> tuple[tuple[str, float], ...]:
    along = float(axis)
    side = orientation_angle(axis, "B")
    forward = (("A", along), ("A", along + pi))
    across = (("B", side), ("B", side + pi))
    if across_first:
        return across + forward
    return forward + across


def _axis_frame(part, axis: float):
    ux, uy = cos(axis), sin(axis)
    vx, vy = -uy, ux
    us: list[float] = []
    vs: list[float] = []
    for x, y in part.exterior.coords:
        px, py = float(x), float(y)
        us.append(px * ux + py * uy)
        vs.append(px * vx + py * vy)
    return min(us), max(us), min(vs), max(vs), ux, uy, vx, vy


def _world_on_axis(u: float, v: float, ux: float, uy: float, vx: float, vy: float) -> tuple[float, float]:
    return (u * ux + v * vx, u * uy + v * vy)


def _sit_axis(spec: dict, point: tuple[float, float], usable, placed: list[dict], axis: float, across_first: bool = False):
    """Вдоль оси, разворот на 180°, и только потом поперёк. Принцип 3 начинает поперёк."""
    for orientation, angle in _axis_attempts(axis, across_first):
        found = _try_sit(spec, point, angle, orientation, usable, placed)
        if found is not None:
            return found, _model_spans(spec, orientation)
    return None, (0.0, 0.0)


def _mix_bands(specs: list[dict], rng: random.Random) -> list[dict]:
    """Полосы близкого размера мешает сид, крупные полосы остаются впереди."""
    ordered = sorted(specs, key=_safety_area, reverse=True)
    if len(ordered) <= 1:
        return ordered
    bands: list[list[dict]] = []
    current = [ordered[0]]
    leader = _safety_area(ordered[0])
    for spec in ordered[1:]:
        area = _safety_area(spec)
        if leader > 0.0 and area < leader * 0.65:
            bands.append(current)
            current = [spec]
            leader = area
        else:
            current.append(spec)
    bands.append(current)
    mixed: list[dict] = []
    for band in bands:
        rng.shuffle(band)
        mixed.extend(band)
    return mixed


def _axis_cells(
    min_u: float,
    max_u: float,
    min_v: float,
    max_v: float,
    step: float,
    origin_u: float,
    origin_v: float,
) -> list[tuple[float, float]]:
    """Клетки оси от точки сида, с общим рядом вдоль покрытия."""
    span = max(max_u - min_u, max_v - min_v, step)
    count = int(span / step) + 2
    cells: list[tuple[float, float, float]] = []
    mid_u = (min_u + max_u) * 0.5
    mid_v = (min_v + max_v) * 0.5
    for row in range(-count, count + 1):
        v = mid_v + row * step
        if v < min_v - 0.2 or v > max_v + 0.2:
            continue
        for column in range(-count, count + 1):
            u = mid_u + column * step
            if u < min_u - 0.2 or u > max_u + 0.2:
                continue
            cells.append((u, v, (u - origin_u) ** 2 + (v - origin_v) ** 2))
    cells.sort(key=lambda item: item[2])
    return [(u, v) for u, v, _distance in cells]


def _zones_clear(safety: list, placed: list[dict], gap: float) -> bool:
    if gap <= 0.0 or not placed:
        return True
    _point, polygon, _union, _prep = _shapely()
    zone = polygon(safety)
    limit = float(gap) - 1e-6
    for other in placed:
        if zone.distance(polygon(other["safety"])) < limit:
            return False
    return True


def _lay_rows(specs: list[dict], usable, rng: random.Random, axis: float, gap: float, style: int = 0):
    """Клетки оси от точки принципа. Сначала самая большая подходящая, кроме принципа 2."""
    parts = [part for part in _geometry_parts(usable) if part.area >= 1.0]
    heroes_exist = any(spec["role"] == "hero" for spec in specs)
    if not parts:
        return [], None, heroes_exist
    frames = [_axis_frame(part, axis) for part in parts]
    min_u = min(frame[0] for frame in frames)
    max_u = max(frame[1] for frame in frames)
    min_v = min(frame[2] for frame in frames)
    max_v = max(frame[3] for frame in frames)
    _min_u, _max_u, _min_v, _max_v, ux, uy, vx, vy = frames[0]
    ordered = sorted(specs, key=_safety_area, reverse=True)
    placed: list[dict] = []
    span_u = max_u - min_u
    span_v = max_v - min_v
    mid_u = (min_u + max_u) * 0.5
    mid_v = (min_v + max_v) * 0.5
    if int(style) == 1:
        sign = -1.0 if rng.random() < 0.5 else 1.0
        origin_u = mid_u + sign * span_u * 0.42
        origin_v = mid_v + (rng.random() - 0.5) * span_v * 0.15
    else:
        origin_u = mid_u + (rng.random() - 0.5) * span_u * 0.3
        origin_v = mid_v + (rng.random() - 0.5) * span_v * 0.3
    cells = _axis_cells(min_u, max_u, min_v, max_v, 1.5, origin_u, origin_v)
    across_first = int(style) == 3

    def sit_at(spec: dict, u: float, v: float):
        point = _world_on_axis(u, v, ux, uy, vx, vy)
        for item in placed:
            xs = [corner[0] for corner in item["safety"]]
            ys = [corner[1] for corner in item["safety"]]
            if min(xs) - gap <= point[0] <= max(xs) + gap and min(ys) - gap <= point[1] <= max(ys) + gap:
                return None
        for orientation, angle in _axis_attempts(axis, across_first):
            found = _try_sit(spec, point, angle, orientation, usable, placed)
            if found is None or not _zones_clear(found["safety"], placed, gap):
                continue
            return found
        return None

    def place_once(candidates: list[dict]) -> int:
        added = 0
        for spec in candidates:
            if len(placed) >= _MAX_PLAY_ITEMS:
                break
            for u, v in cells:
                found = sit_at(spec, u, v)
                if found is None:
                    continue
                placed.append(found)
                added += 1
                break
        return added

    if int(style) == 2 and ordered:
        leader = _safety_area(ordered[0])
        top = [spec for spec in ordered if _safety_area(spec) >= leader * 0.65]
        rng.shuffle(top)
        top_ids = {spec["id"] for spec in top}
        first_queue = top + [spec for spec in ordered if spec["id"] not in top_ids]
    else:
        first_queue = ordered
    for spec in first_queue:
        if len(placed) >= _MAX_PLAY_ITEMS:
            break
        for u, v in cells:
            found = sit_at(spec, u, v)
            if found is None:
                continue
            placed.append(found)
            break
        if placed:
            break
    used = {item["id"] for item in placed}
    place_once(_mix_bands([spec for spec in ordered if spec["id"] not in used], rng))
    if len(placed) < _MAX_PLAY_ITEMS and placed and _widest_open(usable, placed) is not None:
        for _round in range(4):
            if len(placed) >= _MAX_PLAY_ITEMS:
                break
            if place_once(ordered) == 0:
                break
    hero_point = None
    hero_missing = heroes_exist
    for item in placed:
        if item.get("role") == "hero":
            hero_point = (item["x"], item["y"])
            hero_missing = False
            break
    return placed, hero_point, hero_missing


def _open_ground(usable, placed: list[dict]):
    """Полигон без уже занятых зон безопасности."""
    if not placed:
        return usable
    _point, polygon, union, _prep = _shapely()
    block = union([polygon(item["safety"]).buffer(0.4) for item in placed])
    return usable.difference(block).buffer(0)


def _widest_open(usable, placed: list[dict]):
    """Самая широкая свободная часть. Узкий остаток моделью не занимается."""
    parts = [part for part in _geometry_parts(_open_ground(usable, placed)) if part.area >= 12.0]
    if not parts:
        return None
    part = max(parts, key=lambda item: (_candidate_points(item, limit=1)[0][2], item.area))
    _x, _y, radius = _candidate_points(part, limit=1)[0]
    if radius < 1.2:
        return None
    return part


def _seat_on_open(specs: list[dict], pocket, usable, placed: list[dict], rng: random.Random, axis: float) -> dict | None:
    for spec in specs:
        found = _sit_in(spec, pocket, usable, placed, rng, axis)
        if found is not None:
            return found
    return None


def _fill_open_pockets(specs: list[dict], usable, placed: list[dict], rng: random.Random, axis: float) -> None:
    """Свободное место занимает ещё не стоявшая модель, иначе самая компактная.

    Поиск идёт по всему оставшемуся полигону. Остановка на восьми объектах
    или когда следующая зона уже не влезает.
    """
    others = [spec for spec in specs if spec["role"] != "hero"]
    while len(placed) < _MAX_PLAY_ITEMS:
        pocket = _widest_open(usable, placed)
        if pocket is None:
            break
        used = {item["id"] for item in placed}
        fresh = sorted((spec for spec in others if spec["id"] not in used), key=_safety_area, reverse=True)
        found = _seat_on_open(fresh, pocket, usable, placed, rng, axis) if fresh else None
        if found is None:
            found = _seat_on_open(sorted(others, key=_safety_area), pocket, usable, placed, rng, axis)
        if found is None:
            break
        placed.append(found)


def cluster_nearby_islands(islands: list[tuple], gap: float = 4.0) -> list[tuple]:
    """Острова ближе gap метров, включая цепочку, становятся одним полигоном."""
    if len(islands) <= 1:
        return list(islands)
    geometries = []
    for vertices, faces, _key in islands:
        geometries.append(_Triangles(vertices, faces).geometry)
    parent = list(range(len(islands)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(islands)):
        shape = geometries[left]
        if shape is None or shape.is_empty:
            continue
        for right in range(left + 1, len(islands)):
            other = geometries[right]
            if other is None or other.is_empty:
                continue
            if float(shape.distance(other)) < float(gap):
                parent[find(right)] = find(left)
    groups: dict[int, list[int]] = {}
    for index in range(len(islands)):
        groups.setdefault(find(index), []).append(index)
    merged = []
    for indexes in groups.values():
        if len(indexes) == 1:
            merged.append(islands[indexes[0]])
            continue
        vertices: list = []
        faces: list = []
        for index in indexes:
            island_vertices, island_faces, _key = islands[index]
            base = len(vertices)
            vertices.extend(island_vertices)
            faces.extend(tuple(int(vertex) + base for vertex in face) for face in island_faces)
        merged.append((vertices, faces, islands[indexes[0]][2]))
    return merged


def bench_island_keys(islands: list[tuple]) -> set[str]:
    """Лавки получает только самый большой кусок каждого объекта."""
    groups: dict[str, list[tuple[float, str]]] = {}
    for vertices, faces, key in islands:
        name = str(key).split(":", 1)[0]
        groups.setdefault(name, []).append((polygon_area_xy(vertices, faces), str(key)))
    return {max(parts, key=lambda item: item[0])[1] for parts in groups.values() if parts}


def _public_item(item: dict) -> dict:
    kind = _pad_kind(item)
    return {
        "id": item["id"],
        "x": item["x"],
        "y": item["y"],
        "angle": item["angle"],
        "orientation": item["orientation"],
        "pad": list(item["safety"]) if kind else None,
        "pad_kind": kind,
    }


def compose_playground(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    assets: list[dict],
    blocked: tuple[list[tuple[float, float, float]], list[tuple[int, ...]]] | None = None,
    seed: int = 0,
    bench: dict | None = None,
    clearance_scale: float = 1.0,
) -> dict:
    """Модели рядами вдоль оси покрытия, затем лавки по краям.

    Сид повторяет набор и места. Повтор модели — только когда других подходящих нет.
    """
    empty = {"items": [], "benches": [], "urns": [], "hero_missing": False}
    if not assets or not faces:
        return empty
    specs = [_compose_spec(asset) for asset in assets]
    zone = min(max(float(clearance_scale), 0.0), 1.0)
    if zone < 1.0:
        for spec in specs:
            spec["clearance"] = scale_clearance(spec["clearance"], zone)
    rng = random.Random(int(seed))
    usable = _Triangles(vertices, faces).geometry
    if blocked and blocked[1]:
        obstacles = _Triangles(*blocked).geometry
        if not obstacles.is_empty:
            usable = usable.difference(obstacles)
    usable = usable.buffer(0)
    if usable.is_empty:
        return empty
    axis = _playground_axis(usable)
    gap = (1.5, 1.0, 0.4)[int(seed) % 3]
    style = int(seed) % 4
    placed, hero_point, hero_missing = _lay_rows(specs, usable, rng, axis, gap, style)
    station = (0.5, 0.25, 0.75)[int(seed) % 3]
    benches, urns = _compose_benches(usable, hero_point, placed, bench, rng, station) if bench else ([], [])
    return {
        "items": [_public_item(item) for item in placed],
        "benches": benches,
        "urns": urns,
        "hero_missing": hero_missing,
    }


def safety_inside(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    width: float,
    depth: float,
    clearance: tuple[float, float, float, float],
    angle: float,
    origin: tuple[float, float],
    blocked: tuple[list[tuple[float, float, float]], list[tuple[int, ...]]] | None = None,
    scale: float = 1.0,
) -> bool:
    ground = _Triangles(vertices, faces)
    obstacles = _Triangles(*blocked) if blocked and blocked[1] else None
    factor = max(float(scale), 0.0)
    scaled_clearance = tuple(float(value) * factor for value in clearance)
    _body, safety = equipment_quads(width * factor, depth * factor, scaled_clearance, angle, origin)
    return _quad_inside(safety, ground, obstacles)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def plan_image(segments: list[tuple[float, float, float, float]], size: int = 160) -> bytes:
    """План слоя: тёмные контуры на светлом поле, PNG."""
    if not segments:
        return b""
    xs = [value for segment in segments for value in (segment[0], segment[2])]
    ys = [value for segment in segments for value in (segment[1], segment[3])]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span = max(max_x - min_x, max_y - min_y, 1e-6)
    side = max(int(size), 16)
    margin = 8
    usable = side - margin * 2
    pixels = bytearray([236, 232, 220] * side * side)

    def plot(x: int, y: int) -> None:
        if x < 0 or y < 0 or x >= side or y >= side:
            return
        offset = (y * side + x) * 3
        pixels[offset:offset + 3] = b"\x1e\x46\x32"

    def line(x0: int, y0: int, x1: int, y1: int) -> None:
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx - dy
        while True:
            plot(x0, y0)
            if x0 == x1 and y0 == y1:
                break
            doubled = err * 2
            if doubled > -dy:
                err -= dy
                x0 += sx
            if doubled < dx:
                err += dx
                y0 += sy

    drawn = segments[:8000]
    for x1, y1, x2, y2 in drawn:
        ax = margin + int(round((x1 - min_x) / span * usable))
        ay = margin + int(round((max_y - y1) / span * usable))
        bx = margin + int(round((x2 - min_x) / span * usable))
        by = margin + int(round((max_y - y2) / span * usable))
        line(ax, ay, bx, by)
    raw = b"".join(b"\x00" + bytes(pixels[row * side * 3:(row + 1) * side * 3]) for row in range(side))
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


def _glb_parts(path: Path) -> tuple[dict, bytes]:
    """JSON и двоичный буфер GLB. Сетка читается без Blender."""
    import json

    data = Path(path).read_bytes()
    magic, _version, _length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError(f"Не GLB: {path}")
    offset = 12
    payload = None
    blob = b""
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8:offset + 8 + chunk_length]
        offset += 8 + chunk_length
        if chunk_type == 0x4E4F534A:
            payload = json.loads(chunk)
        elif chunk_type == 0x004E4942:
            blob = chunk
    if payload is None:
        raise ValueError(f"В {path} нет JSON")
    return payload, blob


def _quat_matrix(qx: float, qy: float, qz: float, qw: float) -> tuple[tuple[float, float, float], ...]:
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    return (
        (1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)),
        (2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)),
        (2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)),
    )


def _trs_matrix(node: dict) -> tuple[tuple[float, float, float, float], ...]:
    if "matrix" in node:
        values = node["matrix"]
        return tuple(tuple(float(values[row + 4 * column]) for column in range(4)) for row in range(4))
    translation = node.get("translation") or [0.0, 0.0, 0.0]
    rotation = node.get("rotation") or [0.0, 0.0, 0.0, 1.0]
    scale = node.get("scale") or [1.0, 1.0, 1.0]
    rows = _quat_matrix(float(rotation[0]), float(rotation[1]), float(rotation[2]), float(rotation[3]))
    sx, sy, sz = float(scale[0]), float(scale[1]), float(scale[2])
    tx, ty, tz = float(translation[0]), float(translation[1]), float(translation[2])
    return (
        (rows[0][0] * sx, rows[0][1] * sy, rows[0][2] * sz, tx),
        (rows[1][0] * sx, rows[1][1] * sy, rows[1][2] * sz, ty),
        (rows[2][0] * sx, rows[2][1] * sy, rows[2][2] * sz, tz),
        (0.0, 0.0, 0.0, 1.0),
    )


def _mul_matrix(left, right):
    return tuple(
        tuple(sum(left[row][index] * right[index][column] for index in range(4)) for column in range(4))
        for row in range(4)
    )


def _apply_matrix(matrix, point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )


def _yup_to_zup(point: tuple[float, float, float]) -> tuple[float, float, float]:
    """glTF смотрит Y вверх, Blender — Z. План после импорта — XY Blender."""
    x, y, z = point
    return (x, -z, y)


def _mesh_positions(gltf: dict, blob: bytes) -> list[list[tuple[float, float, float]]]:
    found: list[list[tuple[float, float, float]]] = []
    for mesh in gltf.get("meshes") or []:
        points: list[tuple[float, float, float]] = []
        for primitive in mesh.get("primitives") or []:
            index = (primitive.get("attributes") or {}).get("POSITION")
            if index is None:
                continue
            accessor = gltf["accessors"][index]
            if int(accessor.get("componentType", 0)) != 5126 or accessor.get("type") != "VEC3":
                raise ValueError("Позиции сетки ожидаются как float VEC3")
            view = gltf["bufferViews"][accessor["bufferView"]]
            start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
            stride = int(view.get("byteStride", 12))
            for item in range(int(accessor["count"])):
                points.append(struct.unpack_from("<fff", blob, start + item * stride))
        found.append(points)
    return found


def _gltf_plan_points(path: Path) -> list[tuple[float, float, float]]:
    """Вершины в системе Blender после узлового TRS. Сдвиг на землю размер не меняет."""
    gltf, blob = _glb_parts(path)
    positions = _mesh_positions(gltf, blob)
    nodes = gltf.get("nodes") or []
    identity = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    points: list[tuple[float, float, float]] = []

    def walk(index: int, parent) -> None:
        node = nodes[index]
        world = _mul_matrix(parent, _trs_matrix(node))
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            for point in positions[int(mesh_index)]:
                points.append(_yup_to_zup(_apply_matrix(world, point)))
        for child in node.get("children") or []:
            walk(int(child), world)

    scene_index = int(gltf.get("scene") or 0)
    roots = (gltf.get("scenes") or [{}])[scene_index].get("nodes") or []
    for root in roots:
        walk(int(root), identity)
    return points


def glb_planted_points(path: Path) -> list[tuple[float, float, float]]:
    """Вершины как после импорта: TRS узла запечён, Z вверх, центр плана и низ в нуле.

    Так же сдвигает `_ground_center` после `transform_apply`. Сдвиг размер рамки не меняет.
    """
    points = _gltf_plan_points(path)
    if not points:
        return []
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    shift = ((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5, min(zs))
    return [(point[0] - shift[0], point[1] - shift[1], point[2] - shift[2]) for point in points]


def glb_plan_side(path: Path) -> float:
    """Сторона квадрата плана после запекания поворота узла.

    Это локальная рамка XY сетки, которую потом крутит рысканье посадки.
    У «Дерева» рамка до запекания рысканья узла около 21.93 м, после — 17.33 м:
    вершины лежат в этой меньшей рамке, длинная хорда карты остаётся внутри неё.
    """
    return mesh_plan_side(glb_planted_points(path))


def glb_node_scale(path: Path) -> float:
    """Масштаб узла с сеткой. У «Дерева» он не единица, у ели и куста — единица."""
    gltf, _blob = _glb_parts(path)
    for node in gltf.get("nodes") or []:
        if "mesh" not in node:
            continue
        scale = node.get("scale")
        if not scale:
            return 1.0
        return float(scale[0])
    return 1.0
