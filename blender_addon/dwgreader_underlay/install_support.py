"""Проверки установки без bpy: библиотеки, Python Blender, ODA, MCP, классификация."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


PIP_PACKAGES = ("ezdxf", "shapely", "numpy", "mapbox-earcut", "PyJWT[crypto]", "matplotlib", "requests")
VENDOR_NAMES = ("ezdxf", "shapely", "mapbox_earcut", "numpy", "jwt", "cryptography", "matplotlib", "requests")
WINGET_ODA_ID = "ODA.ODAFileConverter"
ODA_DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"
ODA_MSI_FALLBACK = (
    "https://www.opendesign.com/guestfiles/get?filename="
    "ODAFileConverter_QT6_vc16_amd64dll_27.9.msi"
)
ODA_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": ODA_DOWNLOAD_URL,
}
ODA_MSI_MAGIC = b"\xd0\xcf\x11\xe0"
ODA_INSTALL_OK = 0
ODA_INSTALL_NEED_UI = 3
ODA_INSTALL_DOWNLOAD = 2
ODA_PATTERNS = (
    "ODAFileConverter*/ODAFileConverter.exe",
    "ODAFileConverter.exe",
    "ODAFileConverter*.app/Contents/MacOS/ODAFileConverter",
    "ODAFileConverter",
)
API_MISSING = "Войдите в ChatGPT или укажите адрес и модель API"
CHATGPT_MODEL_MISSING = (
    "Модель ChatGPT не выбрана. Список моделей ещё не загрузился — повторите через несколько секунд."
)
CHATGPT_TOKEN_MISSING = "Сессия ChatGPT не дала токен доступа"


def deps_ready(vendor: Path) -> bool:
    return all((vendor / name).exists() for name in VENDOR_NAMES)


def python_search_folders(prefix: Path, binary: Path | None) -> list[Path]:
    folders = [prefix / "bin", prefix / "Scripts", prefix]
    if not binary:
        return folders
    resources = binary.parent.parent / "Resources"
    if resources.is_dir():
        folders.extend(resources.glob("*/python/bin"))
        folders.extend(resources.glob("*/python/Scripts"))
    folders.extend(binary.parent.glob("*/python/bin"))
    folders.extend(binary.parent.glob("*/python/Scripts"))
    return folders


def is_python_binary(path: Path) -> bool:
    name = path.name.lower()
    if not path.is_file() or "config" in name:
        return False
    return name.startswith("python") and not name.endswith((".dll", ".dylib", ".so"))


def classify_block_reason(
    *,
    sharing: bool,
    slug: str,
    has_access: bool,
    access_notice: str = "",
    api_url: str = "",
    api_model: str = "",
    force_api: bool = False,
) -> str:
    """Пустая строка — разбор можно запускать."""
    if not force_api and sharing:
        if not slug or slug == "none":
            return CHATGPT_MODEL_MISSING
        if not has_access:
            return access_notice or CHATGPT_TOKEN_MISSING
        return ""
    if api_url.strip() and api_model.strip():
        return ""
    return API_MISSING


def oda_search_roots(
    platform: str,
    home: Path,
    local_app_data: str = "",
    extra: list[Path] | None = None,
) -> list[Path]:
    roots: list[Path] = []
    if platform == "darwin":
        roots.extend((Path("/Applications"), home / "Applications", home))
    elif platform.startswith("linux"):
        roots.extend((Path("/usr/bin"), Path("/usr/local/bin"), home))
    else:
        roots.extend((
            Path(r"C:\Program Files\ODA"),
            Path(r"C:\Program Files (x86)\ODA"),
            Path(r"C:\Program Files"),
        ))
        if local_app_data:
            roots.append(Path(local_app_data) / "Programs")
        roots.append(home)
    if extra:
        roots.extend(extra)
    return roots


def oda_binaries(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for pattern in ODA_PATTERNS:
        found.extend(path for path in root.glob(pattern) if path.is_file())
    return found


def oda_present(roots: list[Path], which_path: str = "") -> bool:
    if which_path and Path(which_path).is_file():
        return True
    return any(oda_binaries(root) for root in roots)


def is_windows_arm(machine: str) -> bool:
    return machine.lower() in {"arm64", "aarch64"}


def parse_oda_msi_url(html: str) -> str:
    names = re.findall(r"ODAFileConverter[^\"'\s<>]*\.msi", html, re.I)
    if not names:
        raise FileNotFoundError("На странице ODA нет ссылки на установщик")
    return "https://www.opendesign.com/guestfiles/get?filename=" + names[-1]


def looks_like_msi(data: bytes) -> bool:
    if data.startswith(ODA_MSI_MAGIC):
        return True
    head = data.lstrip()[:32].lower()
    return bool(data) and not head.startswith(b"<") and not head.startswith(b"{") and len(data) > 1_000_000


def _urlopen(url: str, timeout: float):
    request = urllib.request.Request(url, headers=ODA_BROWSER_HEADERS)
    return urllib.request.urlopen(request, timeout=timeout)


def _download_curl(url: str, dest: Path) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        return b""
    completed = subprocess.run(
        [
            curl, "-L", "-sS", "--fail",
            "-A", ODA_BROWSER_HEADERS["User-Agent"],
            "-e", ODA_DOWNLOAD_URL,
            "-o", str(dest),
            url,
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0 or not dest.is_file():
        return b""
    return dest.read_bytes()


def download_oda_msi(dest_dir: Path) -> Path:
    """Скачивает MSI браузерным заголовком. winget получает 403 на том же URL."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    html = ""
    try:
        with _urlopen(ODA_DOWNLOAD_URL, timeout=30) as response:
            html = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        html = ""
    try:
        url = parse_oda_msi_url(html) if html else ODA_MSI_FALLBACK
    except FileNotFoundError:
        url = ODA_MSI_FALLBACK
    name = re.search(r"filename=([^&]+)", url, re.I)
    dest = dest_dir / (name.group(1) if name else "ODAFileConverter.msi")
    data = b""
    try:
        with _urlopen(url, timeout=180) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        data = b""
    if not looks_like_msi(data):
        data = _download_curl(url, dest)
    if not looks_like_msi(data):
        raise RuntimeError("Сайт ODA не отдал установщик (403). Скачайте его вручную")
    dest.write_bytes(data)
    return dest


def install_oda_msi(msi: Path) -> int:
    msiexec = shutil.which("msiexec") or "msiexec"
    silent = subprocess.run(
        [msiexec, "/i", str(msi), "/qn", "/norestart"],
        capture_output=True,
        timeout=600,
        check=False,
    )
    if silent.returncode in {0, 3010}:
        return ODA_INSTALL_OK
    try:
        os.startfile(str(msi))  # type: ignore[attr-defined]
        return ODA_INSTALL_NEED_UI
    except OSError:
        return ODA_INSTALL_DOWNLOAD


def run_oda_install(dest_dir: Path) -> int:
    print("STATUS=Скачивание ODA File Converter…", flush=True)
    try:
        msi = download_oda_msi(Path(dest_dir))
    except Exception as exc:
        print(f"STATUS={exc}", flush=True)
        return ODA_INSTALL_DOWNLOAD
    print(f"STATUS=Установщик скачан: {msi.name}", flush=True)
    print("STATUS=Установка ODA File Converter…", flush=True)
    code = install_oda_msi(msi)
    if code == ODA_INSTALL_OK:
        print("STATUS=ODA File Converter установлен", flush=True)
    elif code == ODA_INSTALL_NEED_UI:
        print("STATUS=Подтвердите установщик ODA, если появилось окно", flush=True)
    else:
        print("STATUS=Не удалось запустить установщик ODA", flush=True)
    return code


def mcp_vendor_prefix(vendor: Path) -> str:
    folder = str(vendor.resolve())
    return (
        "import sys\n"
        f"_DWGREADER_VENDOR = {folder!r}\n"
        "if _DWGREADER_VENDOR not in sys.path:\n"
        "    sys.path.insert(0, _DWGREADER_VENDOR)\n"
    )


def compose_mcp_addon(source_text: str, vendor: Path) -> str:
    prefix = mcp_vendor_prefix(vendor)
    head = source_text.lstrip("\ufeff")
    if head.startswith("import sys") and "_DWGREADER_VENDOR" in head[:400]:
        return source_text
    return prefix + "\n" + source_text


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "oda-install":
        raise SystemExit(run_oda_install(Path(sys.argv[2])))
    raise SystemExit("usage: install_support.py oda-install DIR")
