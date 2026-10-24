import base64
import json
import time
import http.client
from pathlib import Path
import tempfile
import threading
import unittest

from backend.api.server import build_server
from tests.test_agent import answer, call, op, response


class AgentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = build_server(Path(cls.directory.name) / "projects.sqlite3", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def request(self, method, path, data=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        headers.update(extra_headers or {})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        result = json.loads(content) if "application/json" in response.getheader("Content-Type", "") else content
        status = response.status
        connection.close()
        return status, result

    def test_static_files_and_safe_navigation_without_api_key(self):
        for path in ("/agent-panel.js", "/agent-panel.css"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/v1/agent/help")
        self.assertEqual(status, 200)
        self.assertIn("mount", [x["target"] for x in result["destinations"]])

    def test_native_acceptance_customer_album_fixture_has_mounted_panels(self):
        from tests.test_agent import fixture
        from tests.verify_desktop_frozen import verify_customer_presentation
        from types import SimpleNamespace
        def ok(method, path, value=None):
            status, result = self.request(method, path, value)
            self.assertTrue(200 <= status < 300, (status, result))
            return result
        project = ok("POST", "/api/v1/import", {"project": fixture().model_dump(mode="json")})["project"]
        report = verify_customer_presentation(SimpleNamespace(ok=ok), f'/api/v1/projects/{project["id"]}')
        self.assertGreater(report["panels"], 0)
        self.assertGreater(report["panel_album_bytes"], 1000)

    def test_draft_api_never_saves_and_refuses_conflicting_apply(self):
        _, created = self.request("POST", "/api/v1/projects", {"name": "API agent test"})
        project = created["project"]
        responses = [response([call("change_project", {"reason": "Название", "operations": [op("/metadata/name", "Предложение")]})]), answer()]
        self.server.agent_runs.transport = lambda *args: responses.pop(0)
        secret = "sk-local-test-12345678901234567890"
        status, settings = self.request("PUT", "/api/v1/agent/settings", {"api_key": secret})
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(settings))
        status, _ = self.request("POST", "/api/v1/agent/runs", {"project": project, "message": "Название", "network_consent": False})
        self.assertEqual(status, 422)
        status, run = self.request("POST", "/api/v1/agent/runs", {"project": project, "message": "Название", "network_consent": True})
        self.assertEqual(status, 202, run)
        path = f'/api/v1/agent/runs/{run["id"]}'
        for _ in range(100):
            _, run = self.request("GET", path)
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed", run)
        _, stored = self.request("GET", f'/api/v1/projects/{project["id"]}')
        self.assertEqual(stored["project"]["metadata"]["name"], "API agent test")
        status, result = self.request("POST", path+"/proposal", {"project": project})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["project"]["metadata"]["name"], "Предложение")
        project["metadata"]["name"] = "Изменили руками"
        status, result = self.request("POST", path+"/proposal", {"project": project})
        self.assertEqual(status, 422)
        self.assertIn("изменился", result["error"]["message"])

    def test_local_upload_and_cross_origin_protection(self):
        status, result = self.request("POST", "/api/v1/agent/files", {"filename": "note.txt", "content_base64": base64.b64encode(b"data only").decode()})
        self.assertEqual(status, 201)
        self.assertFalse(result["uploaded_to_openai"])
        status, _ = self.request("PUT", "/api/v1/agent/settings", {"clear_key": True}, {"Origin": "https://untrusted.example"})
        self.assertEqual(status, 403)
