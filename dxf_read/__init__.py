"""Чтение DWG/DXF и сборка плоской подложки площадки."""

from .dxf import (
    HatchPiece,
    LayerSelection,
    LayerStat,
    Shape,
    convert_dwg_to_dxf,
    draw_classified_hatches,
    export_blender_underlay,
    extract_classified_hatches,
    find_oda_converter,
    infer_layer_category,
    list_layers,
    open_drawing,
    read_layer_geometry,
    refine_layer_groups,
    run_pipeline,
    select_target_layers,
)

__all__ = [
    "HatchPiece",
    "LayerSelection",
    "LayerStat",
    "Shape",
    "convert_dwg_to_dxf",
    "draw_classified_hatches",
    "export_blender_underlay",
    "extract_classified_hatches",
    "find_oda_converter",
    "infer_layer_category",
    "list_layers",
    "open_drawing",
    "read_layer_geometry",
    "refine_layer_groups",
    "run_pipeline",
    "select_target_layers",
]
