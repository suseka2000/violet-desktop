"""Portable library regression coverage, including an emulated Win32 boundary.

The fake adapter runs the Windows routing on the source platform; native Win32
ABI and filesystem semantics are additionally covered on the Windows CI runner.
It is not evidence of a Windows executable having been built/tested on macOS.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import library_io as io
from backend.model_library import ModelLibrary
from backend.vendor_library import VendorLibrary, _safe_file


class FakeWindowsAPI:
    def __init__(self):
        self.closed = []

    def open(self, path, *, directory=False, create=False):
        path = Path(path)
        if create:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        info = path.lstat()
        if io.is_link(info) or stat.S_ISDIR(info.st_mode) != directory or (not directory and info.st_nlink != 1):
            raise OSError("refuse reparse/link")
        return os.open(path, os.O_RDONLY)

    def close(self, handle):
        self.closed.append(handle)
        os.close(handle)

    def descriptor(self, handle, *, writable=False):
        return handle


@contextmanager
def windows_routing():
    """Exercise actual traversal/read/cache code through WindowsDirectory."""
    api = FakeWindowsAPI()

    def root(path):
        path = Path(path)
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise OSError("no linked ancestor")
        return io.WindowsDirectory(path, [api.open(path, directory=True)])

    with patch.object(io, "_windows_api", return_value=api), \
            patch("backend.vendor_library._root_fd", side_effect=root), \
            patch("backend.model_library._root_fd", side_effect=root):
        yield api


@unittest.skipIf(os.name == "nt", "POSIX-host Windows-routing emulation; native checks below run on Windows")
class DesktopLibraryIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "library"
        self.root.mkdir()
        (self.root / "models").mkdir()
        self.content = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;"
        (self.root / "models" / "Профиль.step").write_bytes(self.content)

    def tearDown(self):
        self.temporary.cleanup()

    def test_windows_directory_routing_lists_and_reads_both_libraries(self):
        with windows_routing() as api:
            vendor = VendorLibrary(self.root).inventory()
            models = ModelLibrary(self.root).inventory()
            self.assertTrue(vendor["available"])
            self.assertTrue(models["complete"])
            self.assertEqual(1, len(vendor["assets"]))
            self.assertEqual(1, len(models["assets"]))
            self.assertEqual(hashlib.sha256(self.content).hexdigest(), models["assets"][0]["id"])
            self.assertEqual(self.content, _safe_file(self.root, "models/Профиль.step")[0])
            self.assertGreater(len(api.closed), 5)

    def test_windows_directory_routing_cache_is_atomic_and_readable(self):
        cache = self.base / "cache"
        library = ModelLibrary(self.root, cache_dir=cache)
        with windows_routing():
            library._cache_write("entry.json", b'{"first":1}')
            self.assertEqual(b'{"first":1}', library._cache_read("entry.json", 100))
            library._cache_write("entry.json", b'{"second":2}')
            self.assertEqual(b'{"second":2}', library._cache_read("entry.json", 100))
            self.assertIsNone(library._cache_read("entry.json", 1))
            self.assertEqual(["entry.json"], [p.name for p in cache.iterdir()])

    def test_windows_routing_cannot_read_or_write_linked_root(self):
        cache = self.base / "linked"
        try:
            cache.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Platform cannot create test symlink: {exc}")
        with windows_routing():
            self.assertFalse(VendorLibrary(cache).inventory()["available"])
            self.assertFalse(ModelLibrary(cache).inventory()["available"])
            library = ModelLibrary(self.root, cache_dir=cache)
            self.assertIsNone(library._cache_read("entry.json", 100))
            with self.assertRaises(OSError):
                library._cache_write("entry.json", b"outside")
        self.assertFalse((self.root / "entry.json").exists())

    def test_windows_routing_rejects_file_replaced_with_link(self):
        target = self.base / "private.step"
        target.write_bytes(b"private")
        selected = self.root / "models" / "Профиль.step"
        selected.unlink()
        try:
            selected.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"Platform cannot create test symlink: {exc}")
        with windows_routing():
            self.assertEqual([], ModelLibrary(self.root).inventory()["assets"])
            with self.assertRaises(OSError):
                _safe_file(self.root, "models/Профиль.step")

    def test_junctions_are_links_even_when_st_mode_says_directory(self):
        self.assertTrue(io.is_link(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x410)))
        self.assertTrue(io.is_link(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)))
        self.assertFalse(io.is_link(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x20)))

    def test_windows_path_normalization_handles_drive_case_and_extended_prefix(self):
        self.assertEqual(io._normal_windows_path(r"\\?\C:\Violet\part.step"),
                         io._normal_windows_path(r"c:\violet\part.step"))
        self.assertEqual(io._normal_windows_path(r"\\?\UNC\server\share\file"),
                         io._normal_windows_path(r"\\server\share\file"))

    def test_windows_components_reject_streams_devices_and_ambiguous_names(self):
        with patch.object(io, "WINDOWS", True):
            for name in ("../x", "C:x", "part.step:secret", "NUL", "con.txt", "LPT¹.txt",
                         "COM9", "file.", "file ", "a\\b", "", ".."):
                with self.subTest(name=name), self.assertRaises(OSError):
                    io._component(name)
            self.assertEqual("Профиль 12.step", io._component("Профиль 12.step"))

    def test_windows_root_keeps_every_ancestor_open_and_closes_on_failure(self):
        api = SimpleNamespace(open=lambda path, **kw: str(path), close=lambda value: closed.append(value))
        closed = []
        with patch.object(io, "WINDOWS", True), patch.object(io, "Path", PureWindowsPath), \
                patch.object(io, "_windows_api", return_value=api):
            directory = io.root_directory(r"C:\Users\Person\library")
            self.assertEqual(4, len(directory.handles))
            self.assertEqual([], closed)
            io.close_directory(directory)
            self.assertEqual([r"C:\Users\Person\library", r"C:\Users\Person", r"C:\Users", "C:\\"], closed)
            with self.assertRaises(OSError):
                io.root_directory(r"\\server\share\library")
            with self.assertRaises(OSError):
                io.root_directory(r"C:relative")


@unittest.skipUnless(os.name == "nt", "Actual Win32 handle checks run on Windows CI")
class NativeWindowsLibraryIOTests(unittest.TestCase):
    def test_native_files_are_read_and_cache_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.step").write_bytes(b"ISO-10303-21;fixture")
            library = ModelLibrary(root, cache_dir=root / "derived")
            self.assertEqual(1, len(library.inventory()["assets"]))
            self.assertEqual(1, len(VendorLibrary(root).inventory()["assets"]))
            library._cache_write("entry.json", b"fixture")
            self.assertEqual(b"fixture", ModelLibrary(root, cache_dir=root / "derived")._cache_read("entry.json", 99))
            library._cache_write("entry.json", b"changed")
            self.assertEqual(b"changed", library._cache_read("entry.json", 99))

    def test_native_directory_handle_blocks_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "library"
            directory.mkdir()
            handle = io.root_directory(directory)
            try:
                with self.assertRaises(OSError):
                    directory.rename(root / "replaced")
            finally:
                io.close_directory(handle)
            directory.rename(root / "replaced")

    def test_native_junction_and_hardlink_are_not_library_sources(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, outside = base / "library", base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "private.step").write_bytes(b"ISO-10303-21;private")
            os.link(outside / "private.step", root / "linked.step")
            junction = root / "junction"
            completed = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                       capture_output=True, check=False)
            self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
            try:
                self.assertEqual([], ModelLibrary(root).inventory()["assets"])
                self.assertFalse(VendorLibrary(junction).inventory()["available"])
                with self.assertRaises(OSError):
                    _safe_file(root, "junction/private.step")
                with self.assertRaises(OSError):
                    _safe_file(root, "linked.step")
            finally:
                # Remove the junction itself, not its target.
                os.rmdir(junction)

    def test_native_stream_and_device_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.step").write_bytes(b"ordinary")
            for name in ("source.step:other", "NUL", "CON.txt", "COM1", "trailing."):
                with self.subTest(name=name), self.assertRaises(OSError):
                    _safe_file(root, name)


if __name__ == "__main__":
    unittest.main()
