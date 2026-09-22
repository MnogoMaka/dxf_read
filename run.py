from pathlib import Path

from dxf_read.config import PROJECT_ROOT, resolve_path
from dxf_read.dxf import run_pipeline

MODELS_DIR = resolve_path("output/_dxf")


def main() -> None:
    drawings = sorted(MODELS_DIR.glob("*.dxf"))
    if not drawings:
        raise SystemExit(f"В {MODELS_DIR} нет .dxf")

    print(f"Найдено DWG: {len(drawings)}")
    failed: list[str] = []

    for index, dwg in enumerate(drawings, start=1):
        # if 'СПОЗУ Рычагова 22_дом_17_10_24' not in dwg.name:
        #     continue
        print(f"\n[{index}/{len(drawings)}] {dwg.name}")
        try:
            result = run_pipeline(dwg)
            print("  BLEND:", result["underlay"].get("blend"))
        except Exception as exc:
            failed.append(f"{dwg.name}: {exc}")
            print("  Ошибка:", exc)

    print(f"\nГотово. Успешно: {len(drawings) - len(failed)}, ошибки: {len(failed)}")
    for item in failed:
        print(" ", item)


if __name__ == "__main__":
    main()