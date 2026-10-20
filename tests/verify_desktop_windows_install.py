"""Install and open the actual NSIS artifact on a disposable Windows CI runner.

Run: python -m tests.verify_desktop_windows_install DIST_DIRECTORY_OR_EXE REPORT.json
This is deliberately forbidden on personal and self-hosted machines: NSIS writes
per-user uninstall registration and shortcuts even with a temporary /D target.
It checks a fresh install, not the separate production OTA transaction.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


def require_disposable_runner() -> Path:
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true" or \
            os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise RuntimeError("Installer smoke is allowed only on a disposable GitHub-hosted Windows runner")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", ""))
    if not runner_temp.is_absolute() or not runner_temp.is_dir() or runner_temp.is_symlink():
        raise RuntimeError("A real absolute RUNNER_TEMP directory is required")
    return runner_temp.resolve()


def default_profiles() -> tuple[Path, ...]:
    appdata = Path(os.environ["APPDATA"])
    return (appdata / "Violet", appdata / "violet-desktop")


def refuse_running_violet() -> None:
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            if entry.name.casefold() in {"violet.exe", "violet-backend.exe"}:
                raise RuntimeError("Refusing to run alongside an existing Violet process")
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)


def refuse_existing_installation() -> None:
    import winreg

    refuse_running_violet()
    if any(path.exists() or path.is_symlink() for path in default_profiles()):
        raise RuntimeError("Refusing to run with an existing Violet user profile")
    # The exact NSIS GUID is generated from appId by electron-builder. Check
    # both the install and uninstall keys, including a partially removed app.
    guid = str(uuid.uuid5(uuid.UUID("50e065bc-3134-11e6-9bab-38c9862bdaf3"), "com.violet.interior-rd"))
    keys = (rf"Software\{guid}", rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{guid}")
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            for key in keys:
                try:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view):
                        raise RuntimeError("Refusing to replace an existing Violet installation")
                except FileNotFoundError:
                    pass
    for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(name)
        if base and any((Path(base) / suffix).exists() for suffix in ("Violet", "Programs/Violet")):
            raise RuntimeError("An existing Violet installation directory was found")


def clean_child_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME",
                           "VIOLET_DESKTOP_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"}
            and not key.startswith(("CSC_", "APPLE_", "AWS_"))}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_installer(path: Path) -> tuple[Path, str]:
    package = Path(__file__).resolve().parents[1] / "desktop/package.json"
    version = json.loads(package.read_text(encoding="utf-8"))["version"]
    if not isinstance(version, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
            r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
            r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version):
        raise RuntimeError("desktop/package.json has an invalid release version")
    filename = f"Violet-{version}-win-x64.exe"
    installer = path / filename if path.is_dir() else path
    if installer.name != filename or not installer.is_file() or installer.is_symlink():
        raise RuntimeError(f"Expected the actual NSIS artifact for this source version: {filename}")
    return installer, version


def terminate_owned_tree(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        # Never select by image name: only the process this script created.
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        process.wait(timeout=20)


def wait_started(process: subprocess.Popen, profile: Path) -> dict:
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Installed app exited during startup: {process.returncode}")
        logs = list((profile / "logs").glob("desktop-*.log"))
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)
        api_ready = bool(re.search(r'"GET /api/v1/projects HTTP/1\.[01]" 200\b', text))
        database = profile / "workspace/projects.sqlite3"
        if "Редактор загружен." in text and api_ready and database.is_file():
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("Installed app created an invalid workspace database")
                count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            finally:
                connection.close()
            if count != 0:
                raise RuntimeError("Installed app workspace is not empty")
            return {"editor_loaded": True, "frontend_projects_http_status": 200,
                    "database_quick_check": "ok", "project_count": count}
        time.sleep(0.25)
    raise RuntimeError("Installed app did not load its editor and project API within 150 seconds")


def verify(installer: Path, output: Path) -> dict:
    runner_temp = require_disposable_runner()
    refuse_existing_installation()
    installer, version = select_installer(installer)
    temporary = Path(tempfile.mkdtemp(prefix="violet installed smoke ", dir=runner_temp))
    target, profile = temporary / "Installed Violet", temporary / "Isolated Profile"
    report = {"status": "running", "kind": "fresh-nsis-install-and-startup",
              "version": version, "installer_sha256": sha256(installer),
              "temporary_root": str(temporary), "installed_target": str(target),
              "user_data": str(profile)}
    installer_process = app_process = None
    start = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        # NSIS parses /D as the unquoted remainder of the command line. Keep it
        # last, preserve spaces, and never pass --force-run (which bypasses our
        # explicitly isolated app launch). No shell evaluates this string.
        command = subprocess.list2cmdline([str(installer), "/S", "/currentuser"]) + " /D=" + str(target)
        with (temporary / "installer-output.log").open("wb") as installer_log:
            installer_process = subprocess.Popen(command, executable=str(installer),
                                                 env=clean_child_environment(), cwd=temporary,
                                                 stdout=installer_log, stderr=subprocess.STDOUT)
            if installer_process.wait(timeout=600) != 0:
                raise RuntimeError(f"NSIS installation failed: exit {installer_process.returncode}")
        executable = target / "Violet.exe"
        for path in (executable, target / "resources/app.asar",
                     target / "resources/backend/violet-backend.exe",
                     target / "resources/release-config.json",
                     target / "resources/notices/inventory.json"):
            if not path.is_file():
                raise RuntimeError(f"Installed package is incomplete: {path.relative_to(target)}")
        if any(path.exists() for path in default_profiles()):
            raise RuntimeError("Installer unexpectedly started Violet in its default profile")
        with (temporary / "app-output.log").open("wb") as app_log:
            app_process = subprocess.Popen(
                [str(executable), f"--user-data-dir={profile}"],
                env=clean_child_environment(), cwd=target, stdout=app_log, stderr=subprocess.STDOUT)
            report["startup"] = wait_started(app_process, profile)
            report["pid"] = app_process.pid
            if app_process.poll() is not None:
                raise RuntimeError("Installed app exited immediately after startup")
            if any(path.exists() for path in default_profiles()):
                raise RuntimeError("Installed app created data outside the requested isolated profile")
            terminate_owned_tree(app_process)
        report.update(status="passed", cleanup="forced termination of only the created app process tree",
                      graceful_close_tested=False,
                      default_profile_untouched=True,
                      note="Fresh NSIS installation, editor load and API startup; not UI interaction or an OTA upgrade test.")
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        try:
            terminate_owned_tree(app_process)
            terminate_owned_tree(installer_process)
        finally:
            report["elapsed_seconds"] = round(time.monotonic() - start, 3)
            if report["status"] != "passed":
                # Only our child output and our isolated desktop log, never
                # environment variables, credentials or a default user profile.
                report["diagnostics"] = {}
                for log in [temporary / "app-output.log", temporary / "installer-output.log",
                            *list((profile / "logs").glob("desktop-*.log"))[:2]]:
                    if log.is_file():
                        with log.open("rb") as source:
                            source.seek(max(0, log.stat().st_size - 8000))
                            report["diagnostics"][str(log.relative_to(temporary))] = \
                                source.read(8000).decode("utf-8", errors="replace")
            # Keep the unique runner-only directory for failure inspection.
            # GitHub discards it with the hosted VM; no real profile is deleted.
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=lambda value: Path(value).absolute())
    parser.add_argument("report", type=lambda value: Path(value).absolute())
    arguments = parser.parse_args()
    try:
        print(json.dumps(verify(arguments.installer, arguments.report), ensure_ascii=True, indent=2))
    except Exception as error:
        message = f"Windows installer smoke failed: {error}".encode("ascii", errors="backslashreplace").decode("ascii")
        print(message, file=sys.stderr)
        sys.exit(1)
