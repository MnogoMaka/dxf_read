"""Плоское разбиение отрезков на замкнутые грани.

Так же, как CGAL Arrangement_2: отрезки вставляются в плоскость, режутся
в точках пересечения и стыка, ограниченная грань становится полигоном.
Внешняя бесконечная грань не возвращается.
"""

from __future__ import annotations

from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree


def snap_value(value: float, tolerance: float) -> float:
    if tolerance <= 0:
        return value
    return round(value / tolerance) * tolerance


def bounded_faces(
    segments: list[tuple[tuple[float, float], tuple[float, float], str]],
    tolerance: float,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]], dict[str, float]]]:
    """Грани разбиения: внешнее кольцо, дырки и длина внешнего кольца по слоям."""
    snapped: list[tuple[tuple[float, float], tuple[float, float], str]] = []
    for start, end, layer in segments:
        a = (snap_value(start[0], tolerance), snap_value(start[1], tolerance))
        b = (snap_value(end[0], tolerance), snap_value(end[1], tolerance))
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
        if not lengths:
            continue
        holes = [_open_ring(list(hole.coords)) for hole in face.interiors]
        holes = [ring for ring in holes if len(ring) >= 3]
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
        layer = _nearest_layer(midpoint, lines, snapped, tree, reach)
        if layer:
            lengths[layer] += length
    return dict(lengths)


def _nearest_layer(midpoint: Point, lines, snapped, tree, reach: float) -> str:
    try:
        hits = tree.query(midpoint.buffer(reach))
    except Exception:
        hits = range(len(lines))
    best_layer = ""
    best_distance = reach
    for index in hits:
        distance = lines[int(index)].distance(midpoint)
        if distance < best_distance:
            best_distance = distance
            best_layer = snapped[int(index)][2]
    return best_layer
