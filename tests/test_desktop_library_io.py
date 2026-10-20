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
from backend.importing.source_store import SourceStore
from backend.model_library import ModelLibrary
from backend.vendor_library import VendorLibrary, _safe_file


class DesktopSourceStoreBinaryTests(unittest.TestCase):
    def test_source_roundtrip_preserves_crlf_ctrl_z_and_all_bytes_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"ISO-10303-21;\r\nHEADER;\r\nENDSEC;\r\n\x1a" + bytes(range(256)) + b"\r\nEND"
            store = SourceStore(root)
            record = store.put("Профиль.step", payload)
            self.assertEqual(len(payload), record.byte_length)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), record.source_id)
            self.assertEqual(payload, store.get_bytes(record.source_id))
            restarted = SourceStore(root)
            self.assertEqual(record, restarted.get_record(record.source_id))
            self.assertEqual(payload, restarted.get_bytes(record.source_id))
            self.assertEqual(record, restarted.put("same-source.step", payload))

    def test_raw_descriptor_read_explicitly_requests_binary_mode(self):
        # The actual round-trip above runs on Windows too. This assertion also
        # catches removal of O_BINARY while working on a POSIX development host.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source"
            path.write_bytes(b"a\r\nb\x1ac")
            marker = getattr(os, "O_BINARY", 1 << 29)
            original_open, flags_seen = os.open, []

            def checked_open(filename, flags):
                flags_seen.append(flags)
                actual_flags = flags if os.name == "nt" else flags & ~marker
                return original_open(filename, actual_flags)

            with patch.object(os, "O_BINARY", marker, create=True), \
                    patch("backend.importing.source_store.os.open", side_effect=checked_open):
                self.assertEqual(b"a\r\nb\x1ac", SourceStore._read_regular_file(path, max_bytes=20))
            self.assertEqual(1, len(flags_seen))
            self.assertTrue(flags_seen[0] & marker)


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

    def rename_file(self, parent, source, destination):
        os.replace(parent / source, parent / destination)


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

    def test_windows_enumeration_zero_identity_is_replaced_by_checked_handle_metadata(self):
        source = self.root / "models" / "Профиль.step"
        zeros = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=0, st_ino=0,
                                st_dev=0, st_size=len(self.content))
        entry = SimpleNamespace(name=source.name, stat=lambda **_: zeros)
        with windows_routing():
            directory = io.WindowsDirectory(source.parent, [])
            info = io.entry_stat(directory, entry)
            actual = source.stat()
            self.assertEqual(1, info.st_nlink)
            self.assertEqual((actual.st_ino, actual.st_dev), (info.st_ino, info.st_dev))
            self.assertEqual(actual.st_size, info.st_size)

    def test_windows_entry_metadata_does_not_accept_unknown_link_count(self):
        source = self.root / "models" / "Профиль.step"
        os.link(source, self.root / "second.step")
        zeros = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=0, st_ino=0,
                                st_dev=0, st_size=len(self.content))
        entry = SimpleNamespace(name=source.name, stat=lambda **_: zeros)
        with windows_routing():
            with self.assertRaises(OSError):
                io.entry_stat(io.WindowsDirectory(source.parent, []), entry)

    def test_windows_entry_metadata_checks_current_file_not_stale_enumeration(self):
        source = self.root / "models" / "Профиль.step"
        stale = source.stat()
        source.unlink()
        target = self.base / "private.step"
        target.write_bytes(b"private")
        source.symlink_to(target)
        entry = SimpleNamespace(name=source.name, stat=lambda **_: stale)
        with windows_routing():
            with self.assertRaises(OSError):
                io.entry_stat(io.WindowsDirectory(source.parent, []), entry)

    def test_windows_replace_uses_checked_parent_and_only_validated_leaf_names(self):
        directory = io.WindowsDirectory(self.root, [123])
        calls = []
        api = SimpleNamespace(rename_file=lambda *args: calls.append(args))
        with patch.object(io, "_windows_api", return_value=api):
            io.replace_file(directory, ".random.tmp", "разрез.json")
            self.assertEqual([(self.root, ".random.tmp", "разрез.json")], calls)
            self.assertEqual([123], directory.handles)
            for source, destination in (("../outside", "entry.json"), ("temp.tmp", "../outside"),
                                        ("temp.tmp", "entry.json:stream")):
                with self.subTest(source=source, destination=destination), self.assertRaises(OSError):
                    io.replace_file(directory, source, destination)
            self.assertEqual(1, len(calls))

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

    def test_windows_short_name_alias_matches_only_expanded_identical_path(self):
        requested = r"C:\Users\RUNNER~1\AppData\Local\Temp\LIBRAR~1\PROFILE~1.STP"
        expanded = r"C:\Users\runneradmin\AppData\Local\Temp\Library With Long Name\Profile Long.stp"
        calls = []
        def expand(value):
            calls.append(value)
            return expanded
        self.assertTrue(io._same_windows_path("\\\\?\\" + expanded, requested, expand))
        self.assertEqual([requested], calls)
        for changed in (r"C:\Users\attacker\Profile Long.stp",
                        r"D:\Users\runneradmin\AppData\Local\Temp\Library With Long Name\Profile Long.stp",
                        expanded + ".other"):
            self.assertFalse(io._same_windows_path(changed, requested, expand))
        with self.assertRaises(OSError):
            io._same_windows_path(expanded, requested,
                                  lambda _: (_ for _ in ()).throw(OSError("cannot expand")))

    def test_windows_matching_full_path_needs_no_name_lookup(self):
        def forbidden(_):
            self.fail("Already identical paths need no extra filesystem operation")
        self.assertTrue(io._same_windows_path(r"\\?\C:\Users\Person\Profile.step",
                                             r"c:\users\person\profile.step", forbidden))

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
    def test_native_short_name_ancestor_and_leaf_are_read_without_resolving_links(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        kernel.GetShortPathNameW.restype = wintypes.DWORD
        def short_name(value):
            size = kernel.GetShortPathNameW(str(value), None, 0)
            if not size:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(size + 1)
            count = kernel.GetShortPathNameW(str(value), buffer, len(buffer))
            self.assertTrue(0 < count < len(buffer))
            return Path(buffer.value)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Library With Long Name"
            root.mkdir()
            original = root / "Profile With Long Name.step"
            content = b"ISO-10303-21;short-name-fixture"
            original.write_bytes(content)
            alias_root, alias_file = short_name(root), short_name(original)
            # Even on volumes with 8.3 creation disabled, existing TEMP/user
            # ancestors may use a short alias. Both spellings must be usable.
            opened = io.root_directory(alias_root)
            try:
                descriptor = io.open_file(opened, alias_file.name)
                with os.fdopen(descriptor, "rb") as stream:
                    self.assertEqual(content, stream.read())
                with self.assertRaises(OSError):
                    root.rename(root.with_name("Renamed Library"))
            finally:
                io.close_directory(opened)
            self.assertEqual(1, len(ModelLibrary(alias_root).inventory()["assets"]))
            self.assertEqual(1, len(VendorLibrary(alias_root).inventory()["assets"]))

    def test_native_files_are_read_and_cache_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = b"ISO-10303-21;\r\nfixture\x1a" + bytes(range(256))
            source = root / "source.step"
            source.write_bytes(content)
            library = ModelLibrary(root, cache_dir=root / "derived")
            inventory = library.inventory()
            self.assertEqual(1, len(inventory["assets"]), inventory["warnings"])
            self.assertTrue(inventory["complete"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), inventory["assets"][0]["id"])
            vendor = VendorLibrary(root).inventory()
            self.assertEqual(1, len(vendor["assets"]), vendor["warnings"])
            self.assertEqual(content, _safe_file(root, "source.step")[0])
            parent = io.root_directory(root)
            try:
                with os.scandir(parent) as entries:
                    entry = next(item for item in entries if item.name == "source.step")
                    actual = io.entry_stat(parent, entry)
                self.assertEqual(1, actual.st_nlink)
                self.assertEqual(source.stat().st_ino, actual.st_ino)
            finally:
                io.close_directory(parent)
            library._cache_write("entry.json", b"fixture")
            restarted = ModelLibrary(root, cache_dir=root / "derived")
            self.assertEqual(b"fixture", restarted._cache_read("entry.json", 99))
            restarted_inventory = restarted.inventory()
            self.assertEqual(1, len(restarted_inventory["assets"]), restarted_inventory["warnings"])
            self.assertEqual(inventory["assets"][0]["id"], restarted_inventory["assets"][0]["id"])
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

    def test_native_ancestor_handle_blocks_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ancestor = root / "ancestor"
            directory = ancestor / "library"
            directory.mkdir(parents=True)
            handle = io.root_directory(directory)
            try:
                with self.assertRaises(OSError):
                    ancestor.rename(root / "replaced")
            finally:
                io.close_directory(handle)
            ancestor.rename(root / "replaced")

    def test_native_cache_replace_preserves_directory_lock_and_replaces_unicode_leaf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "derived"
            library = ModelLibrary(root, cache_dir=cache)
            replace = io.replace_file
            seen = []

            def checked_replace(directory, source, destination):
                with self.assertRaises(OSError):
                    cache.rename(root / "moved-cache")
                replace(directory, source, destination)
                with self.assertRaises(OSError):
                    cache.rename(root / "moved-cache")
                seen.append(destination)

            with patch("backend.model_library.replace_file", side_effect=checked_replace):
                library._cache_write("Разрез профиля.json", b"first")
                library._cache_write("Разрез профиля.json", b"second")
            self.assertEqual(["Разрез профиля.json"] * 2, seen)
            self.assertEqual(b"second", library._cache_read("Разрез профиля.json", 99))
            self.assertEqual(["Разрез профиля.json"], [item.name for item in cache.iterdir()])
            cache.rename(root / "moved-cache")

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
