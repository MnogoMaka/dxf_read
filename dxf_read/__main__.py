from __future__ import annotations

import sys
from pathlib import Path

from .config import DEFAULT_SOURCE, resolve_path
from .dxf import run_pipeline


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    source = Path(args[0]) if args else resolve_path(DEFAULT_SOURCE)
    result = run_pipeline(source)
    print("Готово:")
    print(f"  DXF: {result['dxf']}")
    print(f"  JSON: {result['underlay'].get('json')}")
    print(f"  BLEND: {result['underlay'].get('blend')}")


if __name__ == "__main__":
    main()
