from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stage_sources", ROOT / "desktop/scripts/stage_sources.py")
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


class DesktopSourceStagingTests(unittest.TestCase):
    def test_reviewed_allowlist_contains_build_sources_and_only_isolated_tests(self):
        names = staging.source_names(ROOT)
        self.assertIn("desktop/build/violet-backend.spec", names)
        self.assertIn("desktop/pnpm-lock.yaml", names)
        self.assertIn("tests/test_desktop_runtime.py", names)
        self.assertEqual({name for name in names if name.startswith("tests/")}, staging.TEST_FILES)
        for name in names:
            self.assertTrue(staging.source_path_allowed(staging.checked_relative(name)), name)
            self.assertFalse(name.startswith("for_interior-rd-generator/"), name)
        self.assertNotIn("resources/vendor/reference-drawings.json", names)

    def test_rejects_generated_private_and_traversal_inputs(self):
        for name in ("../private", "/Users/person/key", "backend/../data/test.py",
                     "frontend/.env", "desktop/stage/secrets.json", "backend/data/test.py",
                     "desktop/node_modules/test.js", "original.DWG", "original.sldprt",
                     "frontend/test.py:secret", "frontend//app.js", ".ssh/id_rsa"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                staging.checked_relative(name)
        for name in ("tests/test_saved_customer_project.py", "docs/private-notes.md",
                     "resources/vendor/reference-drawings.json", "for_interior-rd-generator/all_models/a.step"):
            self.assertFalse(staging.source_path_allowed(staging.checked_relative(name)), name)

    def test_source_reader_rejects_links_and_secret_without_printing_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "frontend").mkdir()
            original = root / "frontend/app.js"
            original.write_text("safe")
            os.link(original, root / "frontend/hard.js")
            with self.assertRaises((ValueError, OSError)):
                staging.read_checked(root, "frontend/hard.js")
            (root / "frontend/hard.js").unlink()
            token = "ghp_" + "A" * 40
            original.write_text(token)
            with self.assertRaises(ValueError) as caught:
                staging.read_checked(root, "frontend/app.js")
            self.assertNotIn(token, str(caught.exception))
            self.assertIn("value withheld", str(caught.exception))
            if os.name != "nt":
                original.write_text("safe")
                (root / "frontend/link.js").symlink_to(original)
                with self.assertRaises(OSError):
                    staging.read_checked(root, "frontend/link.js")

    def test_staging_copies_only_plan_and_never_overwrites_existing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "source"
            (root / "frontend").mkdir(parents=True)
            (root / "frontend/app.js").write_bytes(b"safe")
            (root / "private.dwg").write_bytes(b"never copy")
            (root / staging.LIBRARY_MANIFEST).parent.mkdir(parents=True)
            (root / staging.LIBRARY_MANIFEST).write_text(json.dumps({"redistribution_scope": "public"}))
            record = {"path": "frontend/app.js", "bytes": 4,
                      "sha256": hashlib.sha256(b"safe").hexdigest(), "kind": "source"}
            target = base / "clean"
            with patch.object(staging, "build_plan", return_value=[record]):
                report = staging.stage(root, target)
                with self.assertRaises(FileExistsError):
                    staging.stage(root, target)
                with self.assertRaises(ValueError):
                    staging.stage(root, root / "nested")
            self.assertEqual((target / "frontend/app.js").read_bytes(), b"safe")
            self.assertFalse((target / "private.dwg").exists())
            self.assertFalse((target / ".git").exists())
            self.assertFalse(report["uploaded"])
            self.assertFalse(report["private_distribution_only"])
            self.assertNotIn(str(root), json.dumps(report))
            self.assertEqual(json.loads((target / staging.REPORT).read_text())["file_count"], 1)

    def test_gitignore_is_deny_by_default_even_in_allowlisted_directories(self):
        text = staging.gitignore_for(["backend/api/server.py", "desktop/build/violet-backend.spec",
                                     "for_interior-rd-generator/all_models/part [1].step"])
        for rule in ("/*", "!/backend/", "/backend/*", "!/backend/api/",
                     "/backend/api/*", "!/backend/api/server.py", "/desktop/build/*"):
            self.assertIn(rule, text.splitlines())
        self.assertIn("!/desktop/build/violet-backend.spec", text)
        self.assertIn("part\\ \\[1\\].step", text)
        self.assertNotIn("!*.py", text)


if __name__ == "__main__":
    unittest.main()
