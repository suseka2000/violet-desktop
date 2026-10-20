from __future__ import annotations
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bundle_policy", ROOT / "desktop/build/bundle_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class DesktopPackagingTests(unittest.TestCase):
    def test_default_allowlist_has_no_customer_projects(self):
        files = policy.application_assets(ROOT)
        paths = {relative for _, relative in files}
        self.assertIn("frontend/index.html", paths)
        self.assertIn("backend/documents/fonts/NotoSans-Regular.ttf", paths)
        self.assertIn("backend/assembly_recipes.py", paths)
        for path in paths:
            self.assertFalse(any(part in policy.FORBIDDEN_PARTS for part in Path(path).parts), path)
            self.assertNotIn("for_interior-rd-generator", path)
            self.assertNotEqual(Path(path).suffix.lower(), ".dxf")
            self.assertFalse(path.endswith(".test.js"), path)
        self.assertNotIn("resources/vendor/reference-drawings.json", paths)

    def test_private_library_needs_confirmation_and_exact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "for_interior-rd-generator" / "all_models" / "profile.step"
            source.parent.mkdir(parents=True)
            source.write_text("ISO-10303-21; test")
            manifest = root / "approved.json"
            data = {"schema_version": "violet-private-library/1", "redistribution_confirmed": False,
                    "files": [{"path": source.relative_to(root).as_posix(), "sha256": policy.digest(source)}]}
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                policy.library_assets(root, manifest)
            data["redistribution_confirmed"] = True
            manifest.write_text(json.dumps(data))
            self.assertEqual(len(policy.library_assets(root, manifest)), 1)
            source.write_text("modified")
            with self.assertRaises(ValueError):
                policy.library_assets(root, manifest)

    def test_no_directory_glob_or_original_drawing_can_sneak_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "approved.json"
            for path in ("../data/project.sqlite3", "/etc/passwd", "for_interior-rd-generator/Draw1.DXF",
                         "for_interior-rd-generator/all_models/*", "for_interior-rd-generator\\file.step"):
                manifest.write_text(json.dumps({"schema_version": "violet-private-library/1",
                    "redistribution_confirmed": True, "files": [{"path": path, "sha256": "0" * 64}]}))
                with self.assertRaises((ValueError, FileNotFoundError)):
                    policy.library_assets(root, manifest)

    def test_approved_library_is_step_and_supplier_manuals_only(self):
        files = policy.library_assets(ROOT, ROOT / "desktop/build/approved-step-library.json")
        self.assertEqual(len(files), 88)
        models = [(source, relative) for source, relative in files if "/all_models/" in relative]
        manuals = [(source, relative) for source, relative in files if "/manuals/" in relative]
        self.assertEqual(len(models), 80)
        self.assertEqual(len(manuals), 8)
        self.assertTrue(all(source.suffix.lower() in {".step", ".stp"} for source, _ in models))
        self.assertTrue(all(source.suffix.lower() in {".pdf", ".jpg", ".jpeg"} for source, _ in manuals))

    def test_production_configuration_never_embeds_signing_secrets(self):
        entry = importlib.util.spec_from_file_location("prepare_release", ROOT / "desktop/scripts/prepare_release.py")
        release = importlib.util.module_from_spec(entry)
        entry.loader.exec_module(release)
        env = {"VIOLET_GITHUB_OWNER": "example-owner", "VIOLET_GITHUB_REPOSITORY": "private-releases",
               "CSC_LINK": "private-certificate", "CSC_KEY_PASSWORD": "private-password",
               "VIOLET_WINDOWS_PUBLISHER_NAME": "Example Inc"}
        config = release.release_configuration(env, "Windows", "AMD64")
        self.assertEqual(config["provider"], "github-public")
        self.assertNotIn("private-password", json.dumps(config))
        self.assertNotIn("CSC_LINK", config)
        self.assertEqual(config["updateProtocol"], "violet-ed25519-v1")
        self.assertFalse(config["osSigned"])
        private = release.release_configuration({**env, "VIOLET_UPDATE_PROVIDER": "github-private"},
                                                 "Windows", "AMD64")
        self.assertEqual(private["provider"], "github-private")
        with self.assertRaises(ValueError):
            release.release_configuration({**env, "VIOLET_UPDATE_PROVIDER": "unsigned"},
                                          "Windows", "AMD64")
        with self.assertRaises(ValueError):
            release.release_configuration({"VIOLET_UPDATE_PUBLIC_KEY": "not a key"}, "Windows", "AMD64")
        with self.assertRaises(ValueError):
            release.release_configuration({"VIOLET_OS_SIGNING": "1"}, "Windows", "AMD64")

    def test_native_inventory_keeps_unknown_components_unreviewed(self):
        from types import SimpleNamespace
        entry = importlib.util.spec_from_file_location("collect_notices", ROOT / "desktop/scripts/collect_notices.py")
        notices = importlib.util.module_from_spec(entry)
        entry.loader.exec_module(notices)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unknown.dll").write_bytes(b"unmapped binary input")
            distribution = SimpleNamespace(metadata={"Name": "cadquery-ocp-novtk"}, version="7.9.3.0",
                files=[Path("unknown.dll")], locate_file=lambda p: root / p)
            report = notices.native_inventory({"cadquery-ocp-novtk": distribution}, {"component_map": []})
            self.assertEqual(report[0]["sha256"], policy.digest(root / "unknown.dll"))
            self.assertIsNone(report[0]["source_version"])
            self.assertEqual(report[0]["license_text_status"], "unmapped")

    def test_release_policy_reads_unicode_artifacts_with_windows_legacy_locale(self):
        entry = importlib.util.spec_from_file_location("prepare_release", ROOT / "desktop/scripts/prepare_release.py")
        release = importlib.util.module_from_spec(entry)
        entry.loader.exec_module(release)
        public_key = json.loads((ROOT / "desktop/build/update-public-key.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = {
                "desktop/build/update-public-key.json": {**public_key, "note": "сечение профиля"},
                "desktop/stage/notices/inventory.json": {"release_ready": True, "note": "сечение профиля"},
                "desktop/stage/backend/bundle-inventory.json": {
                    "platform": "win32", "architecture": "AMD64",
                    "files": [{"path": "Библиотека/сечение профиля.step"}]},
            }
            for relative, value in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            original_read_text, reads = Path.read_text, []

            def legacy_windows_read(path, encoding=None, errors=None):
                reads.append((path.relative_to(root).as_posix(), encoding))
                # Deliberately reproduce Windows' non-UTF-8 default even on a
                # developer's Mac. An implicit read would fail on Cyrillic.
                return original_read_text(path, encoding=encoding or "cp1252", errors=errors)

            with patch.object(release, "ROOT", root), patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, "argv", ["prepare_release.py", "--release"]), \
                    patch.object(sys, "platform", "win32"), \
                    patch.object(release.platform, "system", return_value="Windows"), \
                    patch.object(release.platform, "machine", return_value="AMD64"), \
                    patch.object(Path, "read_text", new=legacy_windows_read), \
                    contextlib.redirect_stdout(io.StringIO()):
                release.main()
            self.assertEqual(set(fixtures), {relative for relative, _ in reads})
            self.assertTrue(all(encoding == "utf-8" for _, encoding in reads))
            config = json.loads((root / "desktop/stage/release-config.json").read_text(encoding="utf-8"))
            self.assertEqual("github-public", config["provider"])
            self.assertEqual(public_key["publicKeyPem"], config["publicKeyPem"])

    def test_build_scripts_never_use_locale_dependent_text_artifacts(self):
        paths = list((ROOT / "desktop/scripts").glob("*.py")) + \
            list((ROOT / "tests").glob("verify_desktop*.py"))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                        node.func.attr in {"read_text", "write_text"}:
                    with self.subTest(path=path.name, line=node.lineno):
                        encoding = next((item.value for item in node.keywords if item.arg == "encoding"), None)
                        self.assertIsInstance(encoding, ast.Constant)
                        self.assertEqual("utf-8", encoding.value)


if __name__ == "__main__":
    unittest.main()
