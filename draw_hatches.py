"""Отрисовка всех HATCH на одном слое.

Типы DXF рисуются по-разному: линия — штрих, INSERT — точка/штамп,
текст — подпись, HATCH — залитый контур с отверстиями. Эта функция
делает только заливки.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.colors import aci2rgb
from ezdxf.entities import DXFEntity, Insert
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
import matplotlib.pyplot as plt

from dxf import (
    FILL_TYPES,
    _hatch_rings,
    _iter_block_content,
    _unit_scale,
    normalize_name,
)

BYLAYER = 256
BYBLOCK = 0


def draw_layer_hatches(
    layer_name: str,
    dxf_path: str | Path,
    *,
    include_blocks: bool = True,
    show: bool = True,
    save_path: str | Path | None = None,
) -> int:
    """Находит все HATCH слоя и рисует их в плане (вид сверху).

    Учитываются и заливки внутри INSERT, если их слой — этот же
    (в том числе слой ``0`` внутри блока, который наследует слой вставки).

    Возвращает число нарисованных полигонов.
    """
    document = ezdxf.readfile(str(dxf_path))
    scale = _unit_scale(document)
    wanted = normalize_name(layer_name)

    polygons: list[tuple[list[tuple[float, float, float]], list, tuple[float, float, float]]] = []

    for entity, _layer in _iter_hatches(document, wanted, include_blocks):
        color = _hatch_color(entity, document)
        for outer, holes in _hatch_rings(entity, scale):
            polygons.append((outer, holes, color))
    print(polygons)
    if not polygons:
        print(f"На слое {layer_name!r} нет HATCH.")
        return 0

    figure, axes = plt.subplots(figsize=(10, 10))
    for outer, holes, color in polygons:
        axes.add_patch(_hatch_patch(outer, holes, color))

    axes.set_aspect("equal", adjustable="box")
    _focus_on_site(axes, polygons)
    axes.set_title(f"HATCH: {layer_name}  ({len(polygons)})")
    axes.set_xlabel("x, м")
    axes.set_ylabel("y, м")
    figure.tight_layout()

    if save_path is not None:
        output = Path(save_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=150)
        print(f"Сохранено: {output}")

    if show:
        plt.show()
    else:
        plt.close(figure)

    print(f"Нарисовано полигонов: {len(polygons)}")
    return len(polygons)


def _ring_area(ring: list[tuple[float, float, float]]) -> float:
    area = 0.0
    for (x1, y1, _), (x2, y2, _) in zip(ring, ring[1:] + ring[:1]):
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def _focus_on_site(axes: Any, polygons: list) -> None:
    """Кадр вокруг самой большой заливки: образцы легенды на листе отбрасываются."""
    areas = [_ring_area(outer) for outer, _, _ in polygons]
    seed = polygons[areas.index(max(areas))][0]
    kept = [seed]
    changed = True
    while changed:
        changed = False
        for outer, _, _ in polygons:
            if outer in kept:
                continue
            if _near_cluster(outer, kept, radius_m=400.0):
                kept.append(outer)
                changed = True

    xs = [point[0] for ring in kept for point in ring]
    ys = [point[1] for ring in kept for point in ring]
    pad_x = (max(xs) - min(xs)) * 0.08 or 1.0
    pad_y = (max(ys) - min(ys)) * 0.08 or 1.0
    axes.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    axes.set_ylim(min(ys) - pad_y, max(ys) + pad_y)


def _near_cluster(
    ring: list[tuple[float, float, float]],
    cluster: list[list[tuple[float, float, float]]],
    radius_m: float,
) -> bool:
    cx, cy = _centroid(ring)
    for other in cluster:
        ox, oy = _centroid(other)
        if (cx - ox) ** 2 + (cy - oy) ** 2 <= radius_m ** 2:
            return True
    return False


def _centroid(ring: list[tuple[float, float, float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in ring) / len(ring),
        sum(point[1] for point in ring) / len(ring),
    )


def _iter_hatches(
    document: Any, wanted: str, include_blocks: bool,
) -> list[tuple[DXFEntity, str]]:
    found: list[tuple[DXFEntity, str]] = []
    modelspace = document.modelspace()

    for entity in modelspace:
        layer = str(entity.dxf.layer)
        if entity.dxftype() in FILL_TYPES and normalize_name(layer) == wanted:
            found.append((entity, layer))

        if not include_blocks or not isinstance(entity, Insert):
            continue
        for child, child_layer, _ in _iter_block_content(entity):
            if child.dxftype() in FILL_TYPES and normalize_name(child_layer) == wanted:
                found.append((child, child_layer))

    return found


def _hatch_color(entity: DXFEntity, document: Any) -> tuple[float, float, float]:
    aci = int(entity.dxf.color)
    if aci == BYLAYER:
        layer = document.layers.get(str(entity.dxf.layer))
        aci = abs(int(layer.get_color())) if layer is not None else 7
    if aci == BYBLOCK or aci < 1:
        aci = 7
    try:
        return aci2rgb(aci).to_floats()
    except (IndexError, KeyError):
        return (0.45, 0.45, 0.45)


def _hatch_patch(
    outer: list[tuple[float, float, float]],
    holes: list[list[tuple[float, float, float]]],
    color: tuple[float, float, float],
) -> PathPatch:
    vertices: list[tuple[float, float]] = []
    codes: list[int] = []

    def add_ring(ring: list[tuple[float, float, float]]) -> None:
        if len(ring) < 3:
            return
        start = (ring[0][0], ring[0][1])
        vertices.append(start)
        codes.append(MplPath.MOVETO)
        for point in ring[1:]:
            vertices.append((point[0], point[1]))
            codes.append(MplPath.LINETO)
        vertices.append(start)
        codes.append(MplPath.CLOSEPOLY)

    add_ring(outer)
    for hole in holes:
        add_ring(hole)

    path = MplPath(vertices, codes)
    return PathPatch(
        path,
        facecolor=(*color, 0.75),
        edgecolor=color,
        linewidth=0.4,
        joinstyle="miter",
    )

if __name__ == "__main__":
    draw_layer_hatches(
        "1 ГП_Штриховки_асфальт_проезд",
        "output_dxf/ПЗУ Касимовская 33 (1).dxf",
        save_path="output/hatches_asphalt.png",
    )
