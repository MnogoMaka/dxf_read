from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_addon" / "dwgreader_underlay"))

from install_support import (
    API_MISSING,
    CHATGPT_MODEL_MISSING,
    CHATGPT_TOKEN_MISSING,
    PIP_PACKAGES,
    VENDOR_NAMES,
    classify_block_reason,
    compose_mcp_addon,
    deps_ready,
    is_windows_arm,
    looks_like_msi,
    mcp_vendor_prefix,
    oda_present,
    oda_search_roots,
    parse_oda_msi_url,
    python_search_folders,
)


class PackageTests(unittest.TestCase):
    def test_pip_includes_matplotlib_and_requests(self) -> None:
        self.assertIn("matplotlib", PIP_PACKAGES)
        self.assertIn("requests", PIP_PACKAGES)
        self.assertIn("matplotlib", VENDOR_NAMES)
        self.assertIn("requests", VENDOR_NAMES)

    def test_deps_ready_needs_every_vendor_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vendor = Path(raw)
            self.assertFalse(deps_ready(vendor))
            for name in VENDOR_NAMES:
                (vendor / name).mkdir()
            self.assertTrue(deps_ready(vendor))


class ClassifyTests(unittest.TestCase):
    def test_sharing_with_model_and_token_is_open(self) -> None:
        self.assertEqual(
            classify_block_reason(sharing=True, slug="gpt-5", has_access=True),
            "",
        )

    def test_sharing_waits_for_model_list(self) -> None:
        self.assertEqual(
            classify_block_reason(sharing=True, slug="none", has_access=False),
            CHATGPT_MODEL_MISSING,
        )

    def test_sharing_without_token_stays_blocked(self) -> None:
        self.assertEqual(
            classify_block_reason(
                sharing=True,
                slug="gpt-5",
                has_access=False,
                access_notice="нет токена",
                api_url="https://example",
                api_model="local",
            ),
            "нет токена",
        )
        self.assertEqual(
            classify_block_reason(sharing=True, slug="gpt-5", has_access=False),
            CHATGPT_TOKEN_MISSING,
        )

    def test_no_plan_needs_api(self) -> None:
        self.assertEqual(
            classify_block_reason(sharing=False, slug="", has_access=False),
            API_MISSING,
        )
        self.assertEqual(
            classify_block_reason(
                sharing=False,
                slug="",
                has_access=False,
                api_url="https://example",
                api_model="local",
            ),
            "",
        )

    def test_force_api_ignores_chatgpt_and_needs_fields(self) -> None:
        self.assertEqual(
            classify_block_reason(
                sharing=True,
                slug="gpt-5",
                has_access=True,
                force_api=True,
            ),
            API_MISSING,
        )
        self.assertEqual(
            classify_block_reason(
                sharing=True,
                slug="gpt-5",
                has_access=True,
                api_url="https://example",
                api_model="local",
                force_api=True,
            ),
            "",
        )


class McpTests(unittest.TestCase):
    def test_prefix_adds_vendor_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vendor = Path(raw) / "vendor"
            vendor.mkdir()
            source = "import requests\nREQ = 1\n"
            first = compose_mcp_addon(source, vendor)
            self.assertIn("_DWGREADER_VENDOR", first)
            self.assertIn(repr(str(vendor.resolve())), first)
            self.assertIn("import requests", first)
            second = compose_mcp_addon(first, vendor)
            self.assertEqual(second, first)
            namespace: dict = {}
            exec(mcp_vendor_prefix(vendor), namespace)
            self.assertIn(str(vendor.resolve()), namespace["sys"].path)


class SearchTests(unittest.TestCase):
    def test_python_folders_include_scripts(self) -> None:
        prefix = Path(r"C:\Blender\4.2\python")
        binary = Path(r"C:\Blender\blender.exe")
        folders = python_search_folders(prefix, binary)
        self.assertIn(prefix / "Scripts", folders)
        self.assertIn(prefix / "bin", folders)

    def test_oda_roots_keep_program_files_and_add_local(self) -> None:
        roots = oda_search_roots("win32", Path("C:/Users/me"), r"C:\Users\me\AppData\Local")
        self.assertIn(Path(r"C:\Program Files\ODA"), roots)
        self.assertIn(Path(r"C:\Users\me\AppData\Local") / "Programs", roots)
        self.assertIn(Path("C:/Users/me"), roots)

    def test_oda_present_accepts_which(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            exe = Path(raw) / "ODAFileConverter.exe"
            exe.write_text("", encoding="utf-8")
            self.assertTrue(oda_present([], str(exe)))
            self.assertFalse(oda_present([Path(raw) / "missing"]))

    def test_windows_arm(self) -> None:
        self.assertTrue(is_windows_arm("ARM64"))
        self.assertFalse(is_windows_arm("AMD64"))

    def test_parse_oda_msi_url_from_guestfiles_page(self) -> None:
        html = (
            '<a href="/guestfiles/get?filename=ODAFileConverter_QT6_vc16_amd64dll_27.9.msi">'
            "Download</a>"
        )
        url = parse_oda_msi_url(html)
        self.assertTrue(url.endswith("ODAFileConverter_QT6_vc16_amd64dll_27.9.msi"))
        self.assertIn("guestfiles/get?filename=", url)

    def test_html_is_not_an_msi(self) -> None:
        self.assertFalse(looks_like_msi(b"<!DOCTYPE html><html>403 Forbidden</html>"))
        self.assertTrue(looks_like_msi(b"\xd0\xcf\x11\xe0" + b"\x00" * 8))


class BuilderTests(unittest.TestCase):
    def test_pack_uses_current_sources_and_reuses_only_building_from_zip(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import builder

        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            addon = project / "blender_addon" / "dwgreader_underlay"
            (addon / "icons").mkdir(parents=True)
            (addon / "textures").mkdir()
            (addon / "mcp_for_blender").mkdir()
            (addon / "__init__.py").write_text("bl_info = {'version': (1, 5, 0)}\n", encoding="utf-8")
            (addon / "placement.py").write_text("feature = 'new'\n", encoding="utf-8")
            (addon / "mcp_for_blender" / "addon.py").write_text("mcp = 1\n", encoding="utf-8")
            (addon / "icons" / "tile.png").write_bytes(b"png")
            for name in builder.SURFACE.values():
                (addon / "textures" / name).write_bytes(b"tex")
            dxf = project / "dxf_read"
            dxf.mkdir()
            (dxf / "__init__.py").write_text("", encoding="utf-8")
            library = project / "maf_blender_models"
            library.mkdir()
            (library / "catalog.json").write_text('[{"id": "new", "file": "new.glb"}]', encoding="utf-8")
            (library / "new.glb").write_bytes(b"new model")
            dest = project / "blender_addon" / "dwgreader_underlay.zip"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("dwgreader_underlay/placement.py", "feature = 'old'\n")
                zf.writestr("dwgreader_underlay/library/catalog.json", "[]")
                zf.writestr("dwgreader_underlay/library/old.glb", b"old model")
                zf.writestr("dwgreader_underlay/building/Building 2.0.blend", b"blend")
                zf.writestr("dwgreader_underlay/building/textures/wall.jpg", b"wall")
            built = builder.build_zip(project, dest)
            with zipfile.ZipFile(built) as zf:
                names = set(zf.namelist())
                placement = zf.read("dwgreader_underlay/placement.py")
                catalog = zf.read("dwgreader_underlay/library/catalog.json")
            self.assertIn("dwgreader_underlay/__init__.py", names)
            self.assertIn("dwgreader_underlay/mcp_for_blender/addon.py", names)
            self.assertIn("dwgreader_underlay/building/Building 2.0.blend", names)
            self.assertIn("dwgreader_underlay/building/textures/wall.jpg", names)
            self.assertIn("dwgreader_underlay/textures/grass.jpg", names)
            self.assertIn("dwgreader_underlay/library/new.glb", names)
            self.assertNotIn("dwgreader_underlay/library/old.glb", names)
            self.assertEqual(placement, (addon / "placement.py").read_bytes())
            self.assertEqual(catalog, (library / "catalog.json").read_bytes())
            self.assertTrue(all("vendor" not in name for name in names))
            self.assertEqual(builder.addon_version(project), (1, 5, 0))


if __name__ == "__main__":
    unittest.main()
