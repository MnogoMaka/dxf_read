"""Подбор габарита и точки лавок. Без Blender, чтобы это можно было проверить отдельно."""

from __future__ import annotations

import random
from math import atan2, cos, hypot, pi, sin


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
    return _sub_meshes(vertices, faces, list(clustered.values()))


def _sub_meshes(
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    face_sets: list[list[int]],
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]]:
    """Сетки по наборам граней, слева направо (min x, затем min y)."""
    parts: list[tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]] = []
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
        parts.append((part_vertices, part_faces))
    parts.sort(key=lambda item: (min(vertex[0] for vertex in item[0]), min(vertex[1] for vertex in item[0])))
    return parts


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
    counts = [int(count) for count in (polygon_faces or []) if int(count) > 0]
    if counts and sum(counts) == len(faces):
        face_sets: list[list[int]] = []
        start = 0
        for count in counts:
            face_sets.append(list(range(start, start + count)))
            start += count
    else:
        face_sets = face_groups(faces)
    return _sub_meshes(vertices, faces, face_sets)


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

    limit = max(gap, 0.0) ** 2
    for left in range(count):
        ax0, ay0, ax1, ay1 = boxes[left]
        for right in range(left + 1, count):
            bx0, by0, bx1, by1 = boxes[right]
            dx = max(0.0, ax0 - bx1, bx0 - ax1)
            dy = max(0.0, ay0 - by1, by0 - ay1)
            if dx * dx + dy * dy <= limit:
                unite(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)

    def origin(members: list[int]) -> tuple[float, float]:
        return (
            min(boxes[index][0] for index in members),
            min(boxes[index][1] for index in members),
        )

    return [members for _root, members in sorted(groups.items(), key=lambda item: origin(item[1]))]
