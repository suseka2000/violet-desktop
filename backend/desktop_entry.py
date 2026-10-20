"""Source and PyInstaller entry for a private, self-contained desktop session."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import runpy
import signal
import sqlite3
import sys
import tempfile
import threading

from backend.desktop_runtime import WORKER_MODULES, asset_root, stop_workers, watch_parent


VERSION_MARKER = "desktop-data-version.json"
VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def application_version(value: str) -> str:
    if not isinstance(value, str) or (value != "development" and (len(value) > 100 or not VERSION_PATTERN.fullmatch(value))):
        raise ValueError("Некорректная версия настольного приложения")
    return value


def prepare_data_upgrade(directory: Path, version: str) -> dict:
    """Snapshot projects/history before a new release can run migrations.

    Source files stay in their unchanged content-addressed store. Derived CAD
    queues/caches are disposable, so the canonical project DB is the backup
    boundary. No existing backup is replaced or automatically deleted.
    """
    version = application_version(version)
    marker = directory / VERSION_MARKER
    previous = None
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16 * 1024:
            raise ValueError("Повреждён маркер версии пользовательских данных")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != "violet-desktop-data/1":
                raise ValueError("Неизвестный формат маркера версии")
            previous = application_version(value["application_version"])
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("Не удалось прочитать версию пользовательских данных") from error
    result = {"version": version, "previous_version": previous, "backup": None}
    database = directory / "projects.sqlite3"
    if database.is_symlink():
        raise ValueError("База проектов должна быть обычным файлом")
    if previous == version or not database.exists():
        return result
    if database.is_symlink() or not database.is_file():
        raise ValueError("База проектов должна быть обычным файлом")
    backups = directory / "backups"
    if backups.is_symlink():
        raise ValueError("Каталог резервных копий не должен быть символической ссылкой")
    backups.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(tempfile.mkdtemp(prefix=f"pre-{version}-{stamp}-", dir=backups))
    pending = destination / "projects.sqlite3.partial"
    snapshot = destination / "projects.sqlite3"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
            with closing(sqlite3.connect(pending)) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("Проверка резервной копии SQLite не пройдена")
        pending.replace(snapshot)
        sha = hashlib.sha256()
        with snapshot.open("rb") as stream:
            for block in iter(lambda: stream.read(1024**2), b""):
                sha.update(block)
        manifest = {"schema": "violet-desktop-pre-update-backup/1",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "from_version": previous, "to_version": version,
                    "database": {"filename": snapshot.name, "sha256": sha.hexdigest(),
                                 "bytes": snapshot.stat().st_size},
                    "includes": ["projects", "project_history"],
                    "source_files": "Preserved unchanged in the sibling sources directory; not copied.",
                    "derived_caches": "Not backed up; can be recalculated."}
        with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, sqlite3.Error, ValueError) as error:
        raise ValueError("Не удалось создать проверенную резервную копию перед обновлением. "
                         "Исходная база не изменена; запуск остановлен.") from error
    result["backup"] = str(destination)
    return result


def commit_data_version(directory: Path, upgrade: dict):
    """Publish only after Repository initialization/migrations succeeded."""
    if upgrade["version"] == upgrade["previous_version"]:
        return
    payload = {"schema": "violet-desktop-data/1", "application_version": upgrade["version"],
               "updated_at": datetime.now(timezone.utc).isoformat(),
               "pre_update_backup": upgrade["backup"]}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                     prefix=".desktop-version-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(directory / VERSION_MARKER)
    finally:
        temporary.unlink(missing_ok=True)


def dispatch_worker(argv: list[str]) -> int:
    if not argv or argv[0] not in WORKER_MODULES:
        raise ValueError("Неподдерживаемый обработчик приложения")
    module, *arguments = argv
    parent = os.environ.get("VIOLET_WORKER_PARENT_PID")
    stopped = threading.Event()
    if parent:
        watch_parent(int(parent), stopped, lambda: os._exit(1))
    # Native workers use os.dup(1) and binary STEP/PNG streams on Windows.
    if os.name == "nt":
        import msvcrt
        for descriptor in (0, 1):
            msvcrt.setmode(descriptor, os.O_BINARY)
    previous = sys.argv
    try:
        sys.argv = [module, *arguments]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    finally:
        sys.argv = previous
        stopped.set()
    return 0


class DataDirectoryLock:
    """OS-released advisory lock; a crash never leaves a permanent stale lock."""
    def __init__(self, directory: Path):
        self.path = directory / ".desktop.lock"
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0, 2)
                if not self.stream.tell():
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise ValueError("Данные уже открыты другим экземпляром приложения") from error
        return self

    def __exit__(self, *_):
        if self.stream:
            self.stream.close()


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--worker"]:
        return dispatch_worker(arguments[1:])
    parser = argparse.ArgumentParser(description="Локальный сервер настольного Violet")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--assets-root", type=Path, default=asset_root())
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(arguments)
    token = os.environ.get("VIOLET_DESKTOP_TOKEN", "")
    if not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Не задан безопасный ключ локального сеанса")
    if not args.data_dir.is_absolute() or not args.assets_root.is_absolute():
        raise ValueError("Нужны абсолютные пути приложения и пользовательских данных")
    if args.parent_pid is not None and args.parent_pid <= 0:
        raise ValueError("Некорректный родительский процесс")
    assets = args.assets_root.resolve(strict=True)
    if not (assets / "frontend" / "index.html").is_file():
        raise ValueError("В сборке отсутствует интерфейс приложения")
    data = args.data_dir.resolve()
    # Installed resources are replaced by updates; user data must not live there.
    if data == assets or data.is_relative_to(assets):
        raise ValueError("Пользовательские данные должны находиться вне установленного приложения")
    data.mkdir(parents=True, exist_ok=True)
    version = application_version(os.environ.get("VIOLET_DESKTOP_VERSION", "development"))
    os.environ["VIOLET_DESKTOP_PROCESS"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    from backend.api.server import build_server
    stopped = threading.Event()
    with DataDirectoryLock(data):
        upgrade = prepare_data_upgrade(data, version)
        server = build_server(data / "projects.sqlite3", host="127.0.0.1", port=0,
                              assets_root=assets, desktop_token=token)
        try:
            commit_data_version(data, upgrade)
        except BaseException:
            server.server_close()
            raise
        def shutdown(*_):
            server.request_desktop_shutdown()
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), shutdown)
        if args.parent_pid is not None:
            watch_parent(args.parent_pid, stopped, shutdown)
        print(json.dumps({"type": "ready", "protocol": 1, "port": server.server_address[1],
                          "pid": os.getpid()}, separators=(",", ":")), flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            stopped.set()
            server.server_close()
            stop_workers()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({"type": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr, flush=True)
        raise SystemExit(2)
