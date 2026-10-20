"""End-to-end desktop binary acceptance on an isolated, disposable workspace.

The test driver uses only stdlib and HTTP; the server cannot import this repo.
Run: python -m tests.verify_desktop_frozen /absolute/violet-backend /tmp/report.json
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import queue
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time


class Session:
    def __init__(self, executable, directory, version):
        self.token = secrets.token_hex(32)
        self.errors = tempfile.TemporaryFile("w+b")
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME", "VIOLET_DESKTOP_PROCESS", "VIOLET_WORKER_PARENT_PID"}}
        env.update(VIOLET_DESKTOP_TOKEN=self.token, VIOLET_DESKTOP_VERSION=version)
        self.process = subprocess.Popen([str(executable), "--data-dir", str(directory),
                                         "--parent-pid", str(os.getpid())],
                                        env=env, cwd=directory, stdout=subprocess.PIPE,
                                        stderr=self.errors, text=True)
        messages = queue.Queue()
        threading.Thread(target=lambda: messages.put(self.process.stdout.readline()), daemon=True).start()
        try:
            self.ready = json.loads(messages.get(timeout=45))
            assert self.ready["type"] == "ready" and self.ready["protocol"] == 1, self.ready
            self.port = self.ready["port"]
        except BaseException:
            self.close()
            raise

    def request(self, method, path, value=None, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=180)
        headers = {"X-Violet-Desktop-Token": self.token} if authorized else {}
        if value is not None:
            value = json.dumps(value).encode()
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, value, headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, (json.loads(data) if "application/json" in
                                     response.getheader("Content-Type", "") else data)
        finally:
            connection.close()

    def ok(self, method, path, value=None):
        status, result = self.request(method, path, value)
        assert 200 <= status < 300, (status, result)
        return result

    def close(self):
        if self.process.poll() is None and hasattr(self, "port"):
            try:
                self.ok("POST", "/api/v1/desktop/shutdown", {"require_idle": True})
            except Exception:
                pass
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.errors.seek(0)
        stderr = self.errors.read().decode(errors="replace")
        self.errors.close()
        if self.process.returncode != 0:
            raise AssertionError(f"Desktop backend exit {self.process.returncode}: {stderr[-12000:]}")


def verify(executable):
    start = time.monotonic()
    report = {"platform": platform.platform(), "architecture": platform.machine(),
              "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix="violet-frozen-acceptance-") as temporary:
        directory = Path(temporary)
        session = Session(executable, directory, "0.4.0")
        try:
            for path in ("/", "/app.js", "/api/v1/desktop/health", "/api/v1/projects"):
                assert session.request("GET", path, authorized=False)[0] == 401
            report["auth"] = "all tested unauthenticated routes rejected"
            assert b"<!doctype html>" in session.ok("GET", "/").lower()
            health = session.ok("GET", "/api/v1/desktop/health")
            assert health["active_jobs"] == 0 and not health["closing"], health
            assert session.ok("GET", "/api/v1/projects")["projects"] == []
            library = session.ok("GET", "/api/v1/vendor-library")
            report["supplier_assets"] = len(library["assets"])
            snapshot = session.ok("GET", "/api/v1/vendor-library/snapshot")
            report["manuals"] = len(snapshot["documents"])
            report["verified_manuals"] = sum(item["source_status"] == "verified" for item in snapshot["documents"])
            report["verified_rules"] = len(snapshot["rule_ids"])
            assert report["supplier_assets"] >= 88, report
            assert report["verified_manuals"] >= 8 and report["verified_rules"] > 0, report
            manual = next(asset for asset in library["assets"] if asset["format"] == "pdf")
            preview = session.ok("GET", f"/api/v1/vendor-library/{manual['id']}/preview.png?page=1")
            assert preview.startswith(b"\x89PNG\r\n\x1a\n"), preview[:64]
            report["pdf_preview_bytes"] = len(preview)
            models = session.ok("GET", "/api/v1/model-library")
            report["distinct_step_models"] = len(models["assets"])
            model = next((item for item in models["assets"] if "V-WT" in item["filename"].upper()), models["assets"][0])
            opened = session.ok("POST", f"/api/v1/model-library/{model['id']}/open", {})
            inspection = opened["inspection"]
            bounds = inspection["bounds_mm"]
            axis_index = max(range(3), key=lambda i: bounds["max"][i] - bounds["min"][i])
            parameters = {"axis": "xyz"[axis_index],
                          "offset_mm": (bounds["max"][axis_index] + bounds["min"][axis_index]) / 2}
            route = f"/api/v1/cad-assets/{opened['source_id']}"
            section = session.ok("POST", route + "/section", parameters)
            assert section["point_count"] > 8, section
            report["native_step"] = {"filename": model["filename"], "bodies": inspection["component_count"],
                                     "section_points": section["point_count"], "axis": parameters["axis"]}
            dxf = session.ok("POST", route + "/section.dxf", parameters)
            assert b"SECTION" in dxf and b"EOF" in dxf
            report["editable_dxf_bytes"] = len(dxf)
            # Hold a real authenticated save inside body parsing. This makes
            # the in-flight request deterministic without patching the binary.
            body = json.dumps({"name": "Frozen acceptance only"}).encode()
            writer = http.client.HTTPConnection("127.0.0.1", session.port, timeout=10)
            try:
                writer.putrequest("POST", "/api/v1/projects")
                writer.putheader("X-Violet-Desktop-Token", session.token)
                writer.putheader("Content-Type", "application/json")
                writer.putheader("Content-Length", str(len(body)))
                writer.endheaders()
                deadline = time.monotonic() + 3
                while not session.ok("GET", "/api/v1/desktop/health")["active_jobs"]:
                    assert time.monotonic() < deadline, "Save was not admitted"
                    time.sleep(0.01)
                status, busy = session.request("POST", "/api/v1/desktop/shutdown", {"require_idle": True})
                assert status == 409 and busy["error"]["code"] == "desktop_busy", (status, busy)
                assert not session.ok("GET", "/api/v1/desktop/health")["closing"]
                writer.send(body)
                response = writer.getresponse()
                result = json.loads(response.read())
                assert response.status == 201, result
                project_id = result["project"]["id"]
                report["idle_update_guard"] = "busy save rejected shutdown; project saved successfully"
            finally:
                writer.close()
            project_path = f"/api/v1/projects/{project_id}"
            built = session.ok("POST", project_path + "/buildings",
                               {"expected_revision": 1, "name": "Acceptance building"})["project"]
            building_id = built["buildings"][0]["id"]
            built = session.ok("POST", project_path + "/floors",
                               {"expected_revision": 2, "building_id": building_id, "name": "Этаж 1"})["project"]
            floor_id = built["buildings"][0]["floors"][0]["id"]
            session.ok("POST", project_path + "/rooms",
                       {"expected_revision": 3, "floor_id": floor_id, "code": "001", "name": "Комната",
                        "width_mm": 3000, "depth_mm": 2500, "height_mm": 2800})
            deadline = time.monotonic() + 90
            while session.ok("GET", "/api/v1/desktop/health")["active_jobs"]:
                assert time.monotonic() < deadline, "Room background preparation did not finish"
                time.sleep(0.1)
            pdf = session.ok("GET", project_path + "/documents.pdf?format=ALBUM")
            assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf and b"/FontFile2" in pdf
            album_dxf = session.ok("GET", project_path + "/documents.dxf?format=ALBUM")
            assert b"SECTION" in album_dxf and b"EOF" in album_dxf
            report["room_album"] = {"pdf_bytes": len(pdf), "embedded_font": True,
                                    "editable_dxf_bytes": len(album_dxf)}
        finally:
            session.close()
        # The same binary, labelled as a newer release, exercises pre-migration
        # backup with an actual persisted project, not a mocked backup function.
        updated = Session(executable, directory, "0.4.1")
        try:
            projects = updated.ok("GET", "/api/v1/projects")["projects"]
            assert [item["id"] for item in projects] == [project_id], projects
            preserved_source = updated.ok("GET", f"/api/v1/cad-assets/{opened['source_id']}/original")
            assert hashlib.sha256(preserved_source).hexdigest() == opened["source_id"]
            backups = list((directory / "backups").glob("*/projects.sqlite3"))
            assert len(backups) == 1
            connection = sqlite3.connect(backups[0])
            try:
                assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            finally:
                connection.close()
            report["pre_update_backup"] = {"database_count": len(backups), "quick_check": "ok",
                                           "project_preserved": True,
                                           "sources_preserved": True}
        finally:
            updated.close()
        repeated = Session(executable, directory, "0.4.1")
        try:
            assert len(list((directory / "backups").glob("*/projects.sqlite3"))) == 1
        finally:
            repeated.close()
    report.update(status="passed", elapsed_seconds=round(time.monotonic() - start, 3))
    return report


if __name__ == "__main__":
    executable, output = map(lambda item: Path(item).resolve(), sys.argv[1:])
    result = verify(executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
