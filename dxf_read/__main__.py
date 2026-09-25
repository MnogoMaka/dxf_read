from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_SOURCE, PipelineSettings, resolve_path, use_settings
from .dxf import run_pipeline


def _settings_from_file(path: Path) -> PipelineSettings:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("fill_polyline_tokens", "junk_layer_tokens"):
        if key in data and not isinstance(data[key], tuple):
            data[key] = tuple(data[key])
    return PipelineSettings(**data)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dxf_read")
    parser.add_argument("source", nargs="?", default=None)
    parser.add_argument(
        "--no-blender",
        action="store_true",
        help="Только JSON подложки, без запуска Blender",
    )
    parser.add_argument(
        "--settings",
        default=None,
        help="JSON с PipelineSettings: радиус, пороги, модель, ODA",
    )
    parsed = parser.parse_args(sys.argv[1:] if argv is None else argv)
    source = Path(parsed.source) if parsed.source else resolve_path(DEFAULT_SOURCE)
    if parsed.settings:
        with use_settings(_settings_from_file(Path(parsed.settings))):
            result = run_pipeline(source, run_blender=not parsed.no_blender)
    else:
        result = run_pipeline(source, run_blender=not parsed.no_blender)
    json_path = result["underlay"].get("json")
    print("Готово:")
    print(f"  DXF: {result['dxf']}")
    print(f"  JSON: {json_path}")
    print(f"  BLEND: {result['underlay'].get('blend')}")
    if parsed.no_blender and not json_path:
        raise SystemExit("Подложка не собрана: в выбранных слоях нет заливок.")
    if json_path:
        print(f"UNDERLAY_JSON={json_path}")


if __name__ == "__main__":
    main()
