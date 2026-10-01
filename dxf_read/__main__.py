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
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Только разобрать слои и записать JSON, без подложки",
    )
    parser.add_argument(
        "--groups",
        default=None,
        help="JSON с подтверждённым by_category. Модель повторно не вызывается",
    )
    parsed = parser.parse_args(sys.argv[1:] if argv is None else argv)
    source = Path(parsed.source) if parsed.source else resolve_path(DEFAULT_SOURCE)
    confirmed = None
    if parsed.groups:
        payload = json.loads(Path(parsed.groups).read_text(encoding="utf-8"))
        confirmed = payload.get("by_category", payload)
    pipeline_kwargs = {
        "run_blender": not parsed.no_blender and not parsed.classify_only,
        "classify_only": parsed.classify_only,
        "by_category": confirmed,
        "lock_groups": confirmed is not None,
    }
    if parsed.settings:
        with use_settings(_settings_from_file(Path(parsed.settings))):
            result = run_pipeline(source, **pipeline_kwargs)
    else:
        result = run_pipeline(source, **pipeline_kwargs)
    if parsed.classify_only:
        print(f"LAYERS_JSON={result.get('layers_json') or ''}")
        return
    json_path = result["underlay"].get("json")
    print("Готово:")
    print(f"  DXF: {result['dxf']}")
    print(f"  JSON: {json_path}")
    print(f"  BLEND: {result['underlay'].get('blend')}")
    if parsed.no_blender and not json_path:
        groups = result.get("by_category") or {}
        listed = [
            f"{category}: {', '.join(names)}"
            for category, names in groups.items()
            if names
        ]
        detail = "; ".join(listed) if listed else "ни один слой не отнесён к площадке"
        print(f"UNDERLAY_EMPTY=В выбранных слоях нет заливок. Отобрано: {detail}")
        return
    if json_path:
        print(f"UNDERLAY_JSON={json_path}")


if __name__ == "__main__":
    main()
