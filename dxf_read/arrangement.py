"""Плоское разбиение отрезков на замкнутые грани.

Так же, как CGAL Arrangement_2: отрезки вставляются в плоскость, режутся
в точках пересечения и стыка, ограниченная грань становится полигоном.
Внешняя бесконечная грань не возвращается.
"""

from __future__ import annotations

from collections import defaultdict
from math import floor, hypot

from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree


def _snapped_endpoints(
    segments: list[tuple[tuple[float, float], tuple[float, float], str]],
    tolerance: float,
) -> list[tuple[tuple[float, float], tuple[float, float], str]]:
    """Притягивает концы к ближайшим исходным точкам без транзитивных цепочек."""
    if tolerance <= 0:
        return list(segments)
    points = [point for start, end, _layer in segments for point in (start, end)]
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    exact_points: dict[tuple[float, float], int] = {}
    anchors: list[int] = []
    assigned: list[int] = []
    for index, point in enumerate(points):
        exact = exact_points.get(point)
        if exact is not None:
            assigned.append(exact)
            continue
        cell = (floor(point[0] / tolerance), floor(point[1] / tolerance))
        best: tuple[float, int] | None = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for anchor in cells.get((cell[0] + dx, cell[1] + dy), ()):
                    distance = hypot(point[0] - points[anchor][0], point[1] - points[anchor][1])
                    if distance <= tolerance and (best is None or (distance, anchor) < best):
                        best = (distance, anchor)
        if best is None:
            anchor = index
            anchors.append(anchor)
            cells[cell].append(anchor)
            exact_points[point] = anchor
        else:
            anchor = best[1]
        assigned.append(anchor)

    snapped = []
    for index, (start, end, layer) in enumerate(segments):
        a = points[assigned[index * 2]]
        b = points[assigned[index * 2 + 1]]
        # Не удаляем реальный короткий сегмент, даже если оба конца выбрали один узел.
        if start != end and a == b:
            a, b = start, end
        snapped.append((a, b, layer))
    return snapped


def bounded_faces(
    segments: list[tuple[tuple[float, float], tuple[float, float], str]],
    tolerance: float,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]], dict[str, float]]]:
    """Грани разбиения: внешнее кольцо, дырки и длина внешнего кольца по слоям."""
    snapped: list[tuple[tuple[float, float], tuple[float, float], str]] = []
    for a, b, layer in _snapped_endpoints(segments, tolerance):
        if a == b or not layer:
            continue
        snapped.append((a, b, layer))
    if not snapped:
        return []

    lines = [LineString([start, end]) for start, end, _layer in snapped]
    noded = unary_union(lines)
    faces = [
        face for face in polygonize(noded)
        if face.geom_type == "Polygon" and not face.is_empty and face.area > 0
    ]
    if not faces:
        return []

    tree = STRtree(lines)
    reach = max(tolerance * 4, 1e-6)
    result: list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]], dict[str, float]]] = []
    for face in faces:
        lengths = _ring_layers(list(face.exterior.coords), lines, snapped, tree, reach)
        holes = [_open_ring(list(hole.coords)) for hole in face.interiors]
        holes = [ring for ring in holes if len(ring) >= 3]
        for hole in face.interiors:
            for layer, length in _ring_layers(list(hole.coords), lines, snapped, tree, reach).items():
                lengths[layer] = lengths.get(layer, 0.0) + length
        if not lengths:
            continue
        result.append((_open_ring(list(face.exterior.coords)), holes, lengths))
    return result


def _open_ring(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ring = [(float(point[0]), float(point[1])) for point in coords]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def _ring_layers(coords, lines, snapped, tree, reach: float) -> dict[str, float]:
    lengths: dict[str, float] = defaultdict(float)
    for start, end in zip(coords, coords[1:]):
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        length = (dx * dx + dy * dy) ** 0.5
        if length <= 0:
            continue
        midpoint = Point((float(start[0]) + float(end[0])) * 0.5, (float(start[1]) + float(end[1])) * 0.5)
        for layer in _nearest_layers(midpoint, lines, snapped, tree, reach):
            lengths[layer] += length
    return dict(lengths)


def _nearest_layers(midpoint: Point, lines, snapped, tree, reach: float) -> set[str]:
    try:
        hits = tree.query(midpoint.buffer(reach))
    except Exception:
        hits = range(len(lines))
    best_layers: set[str] = set()
    best_distance = reach
    epsilon = max(reach * 1e-9, 1e-12)
    for index in hits:
        distance = lines[int(index)].distance(midpoint)
        layer = snapped[int(index)][2]
        if distance < best_distance - epsilon:
            best_distance = distance
            best_layers = {layer}
        elif abs(distance - best_distance) <= epsilon:
            best_layers.add(layer)
    return best_layers
