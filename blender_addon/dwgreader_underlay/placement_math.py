"""Подбор габарита и точки лавок. Без Blender, чтобы это можно было проверить отдельно."""

from __future__ import annotations

import random
from math import atan2, cos, hypot, sin


FIT_SLACK_M = 0.05


def polygon_area_xy(vertices: list[tuple[float, float, float]], faces: list[tuple[int, ...]]) -> float:
    area = 0.0
    for face in faces:
        if len(face) < 3:
            continue
        origin = vertices[face[0]]
        for index in range(1, len(face) - 1):
            ax = vertices[face[index]][0] - origin[0]
            ay = vertices[face[index]][1] - origin[1]
            bx = vertices[face[index + 1]][0] - origin[0]
            by = vertices[face[index + 1]][1] - origin[1]
            area += abs(ax * by - ay * bx) * 0.5
    return area


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
    for face in faces:
        if len(face) < 3:
            continue
        origin = vertices[face[0]]
        for index in range(1, len(face) - 1):
            a = vertices[face[index]]
            b = vertices[face[index + 1]]
            signed = (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])
            weight = abs(signed) * 0.5
            if weight < 1e-12:
                continue
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
    triangles: list[tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]] = []
    weights: list[float] = []
    for face in faces:
        if len(face) < 3:
            continue
        origin = vertices[face[0]]
        for index in range(1, len(face) - 1):
            a = vertices[face[index]]
            b = vertices[face[index + 1]]
            weight = abs((a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])) * 0.5
            if weight < 1e-12:
                continue
            triangles.append((origin, a, b))
            weights.append(weight)
    if not triangles or count <= 0:
        return []
    total = sum(weights)
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight
        cumulative.append(running)
    rng = random.Random(seed)
    points: list[tuple[float, float, float]] = []
    for _ in range(count):
        mark = rng.random() * total
        chosen = triangles[-1]
        for triangle, edge in zip(triangles, cumulative):
            if mark <= edge:
                chosen = triangle
                break
        origin, a, b = chosen
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
    if len(faces) <= 1:
        return [(vertices, faces)] if faces else []
    groups = face_groups(faces)
    if len(groups) <= 1:
        return [(vertices, faces)]
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

    for left in range(len(groups)):
        for right in range(left + 1, len(groups)):
            if _box_gap(boxes[left], boxes[right]) <= gap_m:
                union(left, right)
    clustered: dict[int, list[int]] = {}
    for index, group in enumerate(groups):
        clustered.setdefault(find(index), []).extend(group)
    parts: list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]] = []
    for face_indices in clustered.values():
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
        parts.append((part_vertices, part_faces))
    parts.sort(key=lambda item: (min(vertex[0] for vertex in item[0]), min(vertex[1] for vertex in item[0])))
    return parts


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
