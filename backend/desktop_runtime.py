"""Portable process boundary for the bundled desktop backend.

Only application-owned modules may be dispatched by the frozen executable.
The renderer cannot choose a command, module, path, or process environment.
"""
from __future__ import annotations

import atexit
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import weakref

WORKER_MODULES = frozenset({
    "backend.assembly_worker", "backend.building_assembly_worker",
    "backend.dxf_workspace_worker", "backend.room_assembly_worker",
    "backend.source_preview", "backend.source_worker", "backend.step_worker",
    "backend.vendor_library", "backend.wall_assembly_worker",
})
_workers = weakref.WeakSet()
_worker_lock = threading.RLock()
_closing = False


def asset_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()


def worker_command(module: str, *arguments: str) -> list[str]:
    if module not in WORKER_MODULES:
        raise ValueError("Неподдерживаемый обработчик приложения")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", module, *arguments]
    if os.environ.get("VIOLET_DESKTOP_PROCESS") == "1":
        return [sys.executable, "-m", "backend.desktop_entry", "--worker", module, *arguments]
    return [sys.executable, "-m", module, *arguments]


def worker_launch_options() -> dict:
    """Give children a lifetime owner, but never the HTTP session secret."""
    env = os.environ.copy()
    env.pop("VIOLET_DESKTOP_TOKEN", None)
    if os.environ.get("VIOLET_DESKTOP_PROCESS") == "1":
        env["VIOLET_WORKER_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if not getattr(sys, "frozen", False):
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    options = {"env": env}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def track_worker(process):
    """Register long native jobs for prompt cancellation when the app exits."""
    if os.environ.get("VIOLET_DESKTOP_PROCESS") != "1":
        return process
    with _worker_lock:
        if _closing and process.poll() is None:
            process.terminate()
        _workers.add(process)
    return process


def active_worker_count() -> int:
    with _worker_lock:
        return sum(process.poll() is None for process in _workers)


def stop_workers(timeout: float = 3.0):
    global _closing
    with _worker_lock:
        _closing = True
        pending = [process for process in _workers if process.poll() is None]
    for process in pending:
        try:
            process.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    for process in pending:
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a portable liveness check on Windows.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            # Access denied proves existence; a stale PID gives invalid parameter.
            return ctypes.get_last_error() == 5
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def windows_resident_bytes(pid: int) -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return None
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        return int(value.WorkingSetSize) if psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb) else None
    finally:
        kernel.CloseHandle(handle)


def physical_memory_bytes() -> int:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        *[(name, ctypes.c_ulonglong) for name in (
                            "total_physical", "available_physical", "total_page",
                            "available_page", "total_virtual", "available_virtual",
                            "available_extended")]]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        if kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 16 * 1024**3


def watch_parent(pid: int, stopped: threading.Event, on_exit):
    """Watch only a known parent; never enumerate or kill unrelated processes."""
    def run():
        while not stopped.wait(0.5):
            if not process_alive(pid):
                on_exit()
                return
    thread = threading.Thread(target=run, name="violet-parent-lifetime", daemon=True)
    thread.start()
    return thread


atexit.register(stop_workers)
