"""Desktop boundary tests without touching the developer's saved projects."""
from __future__ import annotations

from io import BytesIO
import http.client
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.api.server import build_server
from backend.desktop_entry import (DataDirectoryLock, VERSION_MARKER, application_version,
                                   commit_data_version, dispatch_worker, main, prepare_data_upgrade)
from backend.desktop_runtime import (WORKER_MODULES, asset_root, physical_memory_bytes,
                                     process_alive, worker_command, worker_launch_options)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "desktop-test-" + "a" * 48


def startup_line(process, errors, *, timeout=60):
    """Bound readiness even when a child stays alive without writing stdout.

    A daemon reader works for Windows pipes as well as POSIX. The caller owns
    child cleanup in its finally block; killing the child releases the reader.
    """
    result = queue.Queue(maxsize=1)

    def read():
        try:
            result.put((process.stdout.readline(), None))
        except Exception as error:
            result.put((None, error))

    threading.Thread(target=read, daemon=True, name="desktop-test-readiness").start()

    def diagnostic(reason):
        errors.seek(0, os.SEEK_END)
        end = errors.tell()
        errors.seek(max(0, end - 8192))
        tail = errors.read().decode("utf-8", errors="replace")
        return AssertionError(
            f"{reason}; pid={process.pid}; exit={process.poll()}; stderr tail:\n{tail}"
        )

    try:
        line, error = result.get(timeout=timeout)
    except queue.Empty:
        raise diagnostic(f"Desktop readiness timed out after {timeout:g}s") from None
    if error is not None:
        raise diagnostic(f"Desktop readiness pipe failed: {type(error).__name__}") from error
    if not line:
        raise diagnostic("Desktop exited before readiness")
    return line


def request(port, method, path, body=None, *, token=TOKEN, extra=None):
    headers = {} if token is None else {"X-Violet-Desktop-Token": token}
    if body is not None:
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    headers.update(extra or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        content = response.read()
        if "application/json" in response.getheader("Content-Type", ""):
            content = json.loads(content)
        return response.status, content
    finally:
        connection.close()


class DesktopCommandTests(unittest.TestCase):
    def test_source_and_frozen_dispatch_are_allowlisted(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(worker_command("backend.step_worker", "inspect"),
                             [sys.executable, "-m", "backend.step_worker", "inspect"])
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(worker_command("backend.step_worker", "inspect"),
                             [sys.executable, "--worker", "backend.step_worker", "inspect"])
        with patch.dict(os.environ, {"VIOLET_DESKTOP_PROCESS": "1"}):
            self.assertEqual(worker_command("backend.step_worker")[1:],
                             ["-m", "backend.desktop_entry", "--worker", "backend.step_worker"])
        for module in ("os", "backend.__main__", "backend.desktop_entry", "../anything"):
            with self.assertRaises(ValueError):
                worker_command(module)
            with self.assertRaises(ValueError):
                dispatch_worker([module])
        self.assertEqual(len(WORKER_MODULES), 10)
        self.assertIn("backend.agent.file_reader", WORKER_MODULES)

    def test_worker_environment_removes_secret_and_sets_owner(self):
        with patch.dict(os.environ, {"VIOLET_DESKTOP_TOKEN": TOKEN, "VIOLET_DESKTOP_PROCESS": "1", "OPENAI_API_KEY": "test-only"}):
            options = worker_launch_options()
        self.assertNotIn("VIOLET_DESKTOP_TOKEN", options["env"])
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertEqual(options["env"]["VIOLET_WORKER_PARENT_PID"], str(os.getpid()))
        self.assertEqual(options["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertIn(str(ROOT), options["env"]["PYTHONPATH"])

    def test_resources_and_liveness(self):
        self.assertTrue((asset_root() / "frontend" / "index.html").is_file())
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(-1))
        self.assertGreater(physical_memory_bytes(), 0)

    def test_data_directory_lock_released_after_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            with DataDirectoryLock(Path(temporary)):
                with self.assertRaises(ValueError):
                    with DataDirectoryLock(Path(temporary)):
                        pass
            with DataDirectoryLock(Path(temporary)):
                pass

    def test_desktop_worker_transports_binary_png(self):
        from PIL import Image
        content = BytesIO()
        Image.new("RGB", (8, 6), color="blue").save(content, "PNG")
        with patch.dict(os.environ, {"VIOLET_DESKTOP_PROCESS": "1", "VIOLET_DESKTOP_TOKEN": TOKEN}):
            from backend.source_preview import preview_bytes
            result = preview_bytes(content.getvalue())
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(Image.open(BytesIO(result)).size, (8, 6))


class DesktopHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = build_server(Path(self.directory.name) / "projects.sqlite3",
                                   port=0, desktop_token=TOKEN, assets_root=ROOT)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.directory.cleanup()

    def test_all_routes_require_token_including_static_and_downloads(self):
        for path in ("/", "/app.js", "/api/v1/projects", "/api/v1/health",
                     "/api/v1/desktop/health", "/api/v1/projects/not-real/export"):
            for token in (None, "wrong"):
                with self.subTest(path=path, token=token):
                    self.assertEqual(request(self.port, "GET", path, token=token)[0], 401)
        self.assertEqual(request(self.port, "GET", "/")[0], 200)
        self.assertEqual(request(self.port, "GET", "/api/v1/projects")[1], {"projects": []})

    def test_host_and_origin_are_checked_even_with_token(self):
        self.assertEqual(request(self.port, "GET", "/api/v1/desktop/health",
                                 extra={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(request(self.port, "GET", "/api/v1/desktop/health",
                                 extra={"Host": "evil.invalid"})[0], 403)

    def test_health_reports_queued_work_and_requests(self):
        status, value = request(self.port, "GET", "/api/v1/desktop/health")
        self.assertEqual(status, 200)
        self.assertEqual(value, {"status": "ok", "protocol": 1, "active_jobs": 0, "closing": False})
        with self.server.assembly_jobs._connection() as connection:
            connection.execute("INSERT INTO assembly_jobs VALUES (?,?,?,?,?,?)",
                               ("desktop-test", "token", "signature", 1, None, '{"status":"queued"}'))
        self.assertEqual(request(self.port, "GET", "/api/v1/desktop/health")[1]["active_jobs"], 1)
        self.assertEqual(self.server.desktop_requests, 0)

    def test_shutdown_requires_empty_json_and_authentication(self):
        self.assertEqual(request(self.port, "POST", "/api/v1/desktop/shutdown", {}, token=None)[0], 401)
        self.assertEqual(request(self.port, "POST", "/api/v1/desktop/shutdown", {"force": True})[0], 422)
        self.assertEqual(request(self.port, "POST", "/api/v1/desktop/shutdown", {})[0], 200)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.server.desktop_closing.is_set())

    def test_idle_shutdown_refuses_an_inflight_save_without_stopping_server(self):
        from backend.service import ProjectService
        original = ProjectService.create
        entered, release = threading.Event(), threading.Event()
        saved = []
        def delayed(service, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test save was not released")
            return original(service, *args, **kwargs)
        with patch.object(ProjectService, "create", delayed):
            writer = threading.Thread(target=lambda: saved.append(
                request(self.port, "POST", "/api/v1/projects", {"name": "Save during update"})))
            writer.start()
            try:
                self.assertTrue(entered.wait(3))
                status, result = request(self.port, "POST", "/api/v1/desktop/shutdown", {"require_idle": True})
                self.assertEqual(status, 409, result)
                self.assertEqual(result["error"]["code"], "desktop_busy")
                self.assertFalse(self.server.desktop_closing.is_set())
                self.assertTrue(self.thread.is_alive())
            finally:
                release.set()
                writer.join(5)
        self.assertEqual(saved[0][0], 201, saved)
        self.assertEqual(request(self.port, "GET", "/api/v1/projects")[0], 200)

    def test_accepted_idle_shutdown_blocks_new_work_atomically(self):
        # Keep the listener alive just long enough to exercise admission after
        # quiescing; production shutdown stops it immediately afterwards.
        with patch.object(self.server, "shutdown"):
            status, result = request(self.port, "POST", "/api/v1/desktop/shutdown", {"require_idle": True})
            self.assertEqual(status, 200, result)
            self.assertTrue(self.server.desktop_closing.is_set())
            status, result = request(self.port, "POST", "/api/v1/projects", {"name": "Too late"})
            self.assertEqual(status, 503, result)
            self.assertEqual(result["error"]["code"], "desktop_closing")
            self.assertEqual(self.server.desktop_requests, 0)

    def test_idle_shutdown_refuses_queued_geometry(self):
        with self.server.assembly_jobs._connection() as connection:
            connection.execute("INSERT INTO assembly_jobs VALUES (?,?,?,?,?,?)",
                               ("queued-job", "token", "signature", 1, None, '{"status":"queued"}'))
        self.assertEqual(request(self.port, "POST", "/api/v1/desktop/shutdown", {"require_idle": True})[0], 409)
        self.assertFalse(self.server.desktop_closing.is_set())


class DesktopProcessTests(unittest.TestCase):
    def test_startup_line_times_out_when_live_child_never_writes(self):
        release, finished = threading.Event(), threading.Event()

        class SilentPipe:
            def readline(self):
                try:
                    release.wait()
                    return ""
                finally:
                    finished.set()

        class LiveChild:
            pid = 12345
            stdout = SilentPipe()

            @staticmethod
            def poll():
                return None

        try:
            with self.assertRaisesRegex(AssertionError, "readiness timed out") as failure:
                startup_line(LiveChild(), BytesIO(b"native import stalled\n"), timeout=0.01)
            self.assertIn("pid=12345; exit=None", str(failure.exception))
            self.assertIn("native import stalled", str(failure.exception))
        finally:
            release.set()
            self.assertTrue(finished.wait(2), "test readiness reader was not released")

    def test_lost_parent_closes_backend_without_leaving_listener(self):
        with tempfile.TemporaryDirectory(prefix="violet-orphan-test-") as temporary:
            owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
            owner.wait(timeout=5)
            process = subprocess.run(
                [sys.executable, "-m", "backend.desktop_entry", "--data-dir", temporary,
                 "--assets-root", str(ROOT), "--parent-pid", str(owner.pid)],
                cwd=ROOT, capture_output=True, timeout=10,
                env={**os.environ, "VIOLET_DESKTOP_TOKEN": TOKEN})
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout, b"")
            self.assertFalse((Path(temporary) / "projects.sqlite3").exists())

    def test_live_backend_stops_after_its_owner_exits(self):
        with tempfile.TemporaryDirectory(prefix="violet-parent-exit-test-") as temporary:
            owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            with tempfile.TemporaryFile("w+b") as errors:
                process = subprocess.Popen(
                    [sys.executable, "-m", "backend.desktop_entry", "--data-dir", temporary,
                     "--assets-root", str(ROOT), "--parent-pid", str(owner.pid)],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=errors, text=True,
                    env={**os.environ, "VIOLET_DESKTOP_TOKEN": TOKEN})
                try:
                    line = startup_line(process, errors)
                    ready = json.loads(line)
                    self.assertEqual(ready["type"], "ready")
                    owner.terminate()
                    owner.wait(timeout=5)
                    process.wait(timeout=10)
                    self.assertEqual(process.returncode, 0)
                    with self.assertRaises(OSError):
                        request(ready["port"], "GET", "/api/v1/desktop/health")
                finally:
                    for child in (owner, process):
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=5)
                    process.stdout.close()

    def test_orphan_worker_stops_even_when_waiting_for_input(self):
        owner = subprocess.Popen([sys.executable, "-c", "pass"])
        owner.wait(timeout=5)
        process = subprocess.Popen(
            [sys.executable, "-m", "backend.desktop_entry", "--worker", "backend.source_worker"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "VIOLET_WORKER_PARENT_PID": str(owner.pid)})
        try:
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 1)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_ready_protocol_and_graceful_exit(self):
        with tempfile.TemporaryDirectory(prefix="violet-desktop-test-") as temporary:
            with tempfile.TemporaryFile("w+b") as errors:
                process = subprocess.Popen(
                    [sys.executable, "-m", "backend.desktop_entry", "--data-dir", temporary,
                     "--assets-root", str(ROOT), "--parent-pid", str(os.getpid())],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=errors, text=True,
                    env={**os.environ, "VIOLET_DESKTOP_TOKEN": TOKEN})
                try:
                    line = startup_line(process, errors)
                    ready = json.loads(line)
                    self.assertEqual(ready["type"], "ready")
                    self.assertEqual(ready["protocol"], 1)
                    self.assertEqual(ready["pid"], process.pid)
                    self.assertNotIn(TOKEN, json.dumps(ready))
                    self.assertEqual(request(ready["port"], "GET", "/api/v1/desktop/health")[0], 200)
                    self.assertTrue((Path(temporary) / "projects.sqlite3").is_file())
                    request(ready["port"], "POST", "/api/v1/desktop/shutdown", {})
                    stdout, _ = process.communicate(timeout=10)
                    self.assertEqual(stdout, "")
                    self.assertEqual(process.returncode, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    process.stdout.close()

    def test_missing_session_secret_never_starts_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ)
            env.pop("VIOLET_DESKTOP_TOKEN", None)
            process = subprocess.run([sys.executable, "-m", "backend.desktop_entry",
                                      "--data-dir", temporary], env=env, cwd=ROOT,
                                     capture_output=True, timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(process.stdout, b"")
            self.assertFalse((Path(temporary) / "projects.sqlite3").exists())


class DesktopDataUpgradeTests(unittest.TestCase):
    @staticmethod
    def create_database(directory):
        connection = sqlite3.connect(directory / "projects.sqlite3")
        try:
            connection.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE project_history (revision INTEGER, payload TEXT);
                INSERT INTO projects VALUES ('project-1','current');
                INSERT INTO project_history VALUES (1,'previous');
                INSERT INTO project_history VALUES (2,'current');
            """)
            connection.commit()
        finally:
            connection.close()

    def test_backup_contains_history_does_not_copy_sources_or_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.create_database(directory)
            sources = directory / "sources"
            sources.mkdir()
            (sources / "drawing.step").write_bytes(b"source-preserved")
            original = (directory / "projects.sqlite3").read_bytes()
            upgrade = prepare_data_upgrade(directory, "0.4.0")
            backup = Path(upgrade["backup"])
            self.assertFalse((directory / VERSION_MARKER).exists())
            connection = sqlite3.connect(backup / "projects.sqlite3")
            try:
                self.assertEqual(connection.execute("SELECT * FROM project_history ORDER BY revision").fetchall(),
                                 [(1, "previous"), (2, "current")])
            finally:
                connection.close()
            self.assertEqual((directory / "projects.sqlite3").read_bytes(), original)
            self.assertFalse((backup / "sources").exists())
            self.assertEqual((sources / "drawing.step").read_bytes(), b"source-preserved")
            manifest = json.loads((backup / "manifest.json").read_text())
            self.assertEqual(manifest["to_version"], "0.4.0")
            self.assertEqual(manifest["database"]["bytes"], (backup / "projects.sqlite3").stat().st_size)
            commit_data_version(directory, upgrade)
            self.assertIsNone(prepare_data_upgrade(directory, "0.4.0")["backup"])
            self.assertEqual(len(list((directory / "backups").iterdir())), 1)
            next_upgrade = prepare_data_upgrade(directory, "0.5.0")
            self.assertNotEqual(next_upgrade["backup"], upgrade["backup"])
            self.assertEqual(next_upgrade["previous_version"], "0.4.0")
            self.assertEqual(len(list((directory / "backups").iterdir())), 2)
            self.assertEqual(json.loads((backup / "manifest.json").read_text()), manifest)

    def test_fresh_install_only_records_version_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            upgrade = prepare_data_upgrade(directory, "0.4.0")
            self.assertIsNone(upgrade["backup"])
            self.assertFalse((directory / "backups").exists())
            self.assertFalse((directory / VERSION_MARKER).exists())
            commit_data_version(directory, upgrade)
            self.assertEqual(json.loads((directory / VERSION_MARKER).read_text())["application_version"], "0.4.0")

    def test_backup_includes_committed_wal_not_only_main_database_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            writer = sqlite3.connect(directory / "projects.sqlite3")
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE history (revision INTEGER)")
                writer.execute("INSERT INTO history VALUES (37)")
                writer.commit()
                self.assertTrue((directory / "projects.sqlite3-wal").exists())
                backup = Path(prepare_data_upgrade(directory, "0.4.0")["backup"])
                reader = sqlite3.connect(backup / "projects.sqlite3")
                try:
                    self.assertEqual(reader.execute("SELECT revision FROM history").fetchall(), [(37,)])
                finally:
                    reader.close()
            finally:
                writer.close()

    def test_failed_backup_prevents_repository_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "projects.sqlite3"
            database.write_bytes(b"damaged database")
            with patch.dict(os.environ, {"VIOLET_DESKTOP_TOKEN": TOKEN, "VIOLET_DESKTOP_VERSION": "0.4.0"}):
                with patch("backend.api.server.build_server") as create_server:
                    with self.assertRaisesRegex(ValueError, "резервную копию"):
                        main(["--data-dir", temporary, "--assets-root", str(ROOT)])
                    create_server.assert_not_called()
            self.assertEqual(database.read_bytes(), b"damaged database")
            self.assertFalse((directory / VERSION_MARKER).exists())

    def test_failed_migration_does_not_advance_version_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.create_database(directory)
            commit_data_version(directory, {"version": "0.3.0", "previous_version": None, "backup": None})
            marker = (directory / VERSION_MARKER).read_bytes()
            with patch.dict(os.environ, {"VIOLET_DESKTOP_TOKEN": TOKEN, "VIOLET_DESKTOP_VERSION": "0.4.0"}):
                with patch("backend.api.server.build_server", side_effect=ValueError("migration failed")):
                    with self.assertRaisesRegex(ValueError, "migration failed"):
                        main(["--data-dir", temporary, "--assets-root", str(ROOT)])
            self.assertEqual((directory / VERSION_MARKER).read_bytes(), marker)
            self.assertEqual(len(list((directory / "backups").glob("*/projects.sqlite3"))), 1)

    def test_version_cannot_inject_backup_paths_and_bad_marker_is_rejected(self):
        for value in ("../elsewhere", "/tmp/bad", "1.0.0/bad", "1.0.0:bad", "", "1" * 101, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                application_version(value)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / VERSION_MARKER).write_text("[]")
            with self.assertRaises(ValueError):
                prepare_data_upgrade(directory, "0.4.0")


if __name__ == "__main__":
    unittest.main()
