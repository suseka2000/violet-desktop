"""No-follow library handles on POSIX and Windows.

Windows does not implement Python's ``dir_fd``/``O_NOFOLLOW`` interface.
Use non-inheritable native handles there, reject *all* reparse points (including
junctions), and hold every directory ancestor open without write/delete sharing
for the lifetime of the operation. A final path check is made against the opened
handle, not just against a path checked before opening. No untrusted library
file can turn into an alternate data stream or a DOS device.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
from functools import lru_cache
import ntpath
import os
from pathlib import Path, PureWindowsPath
import stat

WINDOWS = os.name == "nt"
REPARSE_POINT = 0x400


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _component(name):
    if not isinstance(name, str) or not name or name in {".", ".."} or any(c in name for c in "/\\:\0"):
        raise OSError(errno.EINVAL, "Invalid library path component")
    if WINDOWS and (name.endswith((".", " ")) or name.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        *(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"),
    }):
        raise OSError(errno.EINVAL, "Invalid Windows library filename")
    return name


def _normal_windows_path(path):
    # GetFinalPathNameByHandle returns the extended path spelling.
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD),
                    ("creation", wintypes.FILETIME), ("access", wintypes.FILETIME),
                    ("write", wintypes.FILETIME), ("volume", wintypes.DWORD),
                    ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                    ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                    ("index_low", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.GetFileType.argtypes = (wintypes.HANDLE,)
    kernel.GetFileType.restype = wintypes.DWORD

    class API:
        @staticmethod
        def close(handle):
            kernel.CloseHandle(handle)

        @staticmethod
        def open(path, *, directory=False, create=False):
            flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
            access = 0x80 if directory else (0x40000000 if create else 0x80000000)
            # In particular omit FILE_SHARE_DELETE: ancestors cannot be swapped.
            # Omit FILE_SHARE_WRITE to prevent reparse conversion while held.
            handle = kernel.CreateFileW(str(path), access, 1 if not create else 0,
                                        None, 1 if create else 3, flags, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                info = FileInformation()
                if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if (info.attributes & REPARSE_POINT or bool(info.attributes & 0x10) != directory
                        or kernel.GetFileType(handle) != 1 or (not directory and info.links != 1)):
                    raise OSError(errno.ELOOP, "Library links, reparse points and non-regular files are not allowed")
                size = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
                if not size:
                    raise ctypes.WinError(ctypes.get_last_error())
                buffer = ctypes.create_unicode_buffer(size + 1)
                count = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
                if not count or count >= len(buffer):
                    raise OSError(errno.EIO, "Could not validate opened library path")
                if _normal_windows_path(buffer.value) != _normal_windows_path(str(path)):
                    raise OSError(errno.ELOOP, "Opened library path changed")
                return handle
            except BaseException:
                kernel.CloseHandle(handle)
                raise

        @staticmethod
        def descriptor(handle, *, writable=False):
            import msvcrt
            try:
                return msvcrt.open_osfhandle(handle, (os.O_WRONLY if writable else os.O_RDONLY) | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(handle)
                raise

    return API


@dataclass
class WindowsDirectory:
    path: Path
    handles: list[int]

    def __fspath__(self):
        return str(self.path)


def root_directory(path):
    path = Path(path)
    if WINDOWS:
        # App/library roots are local paths, not remote devices or UNC shares.
        # Relative and drive-relative paths must never acquire ambient meaning.
        windows_path = PureWindowsPath(str(path))
        if not windows_path.is_absolute() or not windows_path.drive or windows_path.drive.startswith("\\"):
            raise OSError(errno.EINVAL, "A local absolute Windows library path is required")
        handles = []
        cursor = Path(windows_path.anchor)
        try:
            handles.append(_windows_api().open(cursor, directory=True))
            for part in windows_path.parts[1:]:
                cursor = cursor / _component(part)
                handles.append(_windows_api().open(cursor, directory=True))
            return WindowsDirectory(cursor, handles)
        except BaseException:
            for handle in reversed(handles):
                _windows_api().close(handle)
            raise
    fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            replacement = child_directory(fd, part)
            os.close(fd)
            fd = replacement
        return fd
    except BaseException:
        os.close(fd)
        raise


def child_directory(parent, name):
    name = _component(name)
    if isinstance(parent, WindowsDirectory):
        path = parent.path / name
        return WindowsDirectory(path, [_windows_api().open(path, directory=True)])
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def close_directory(directory):
    if isinstance(directory, WindowsDirectory):
        for handle in reversed(directory.handles):
            _windows_api().close(handle)
        directory.handles.clear()
    else:
        os.close(directory)


def open_file(parent, name, *, create=False):
    name = _component(name)
    if isinstance(parent, WindowsDirectory):
        handle = _windows_api().open(parent.path / name, create=create)
        return _windows_api().descriptor(handle, writable=create)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY | os.O_NONBLOCK
    return os.open(name, flags | os.O_NOFOLLOW, mode=0o600, dir_fd=parent)


def make_directory(parent, name):
    name = _component(name)
    if isinstance(parent, WindowsDirectory):
        os.mkdir(parent.path / name, mode=0o700)
    else:
        os.mkdir(name, mode=0o700, dir_fd=parent)


def replace_file(parent, source, destination):
    _component(source)
    _component(destination)
    if isinstance(parent, WindowsDirectory):
        os.replace(parent.path / source, parent.path / destination)
    else:
        os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)


def unlink_file(parent, name):
    name = _component(name)
    if isinstance(parent, WindowsDirectory):
        os.unlink(parent.path / name)
    else:
        os.unlink(name, dir_fd=parent)
