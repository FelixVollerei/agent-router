from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from model_router.credentials import save_secret, load_secret
from model_router.models import Event
from model_router.orchestrator import Orchestrator as RealOrchestrator
from model_router.webapp import App, make_server, ROOT
from tests.test_policy import easy_task


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "variables.json").write_text('{"old": 1}')
        self.app = App(ROOT / "router.example.toml", self.root / "ui")
        self.app.config.auto_usage = False
        self.app.config.commands = {"check": [sys.executable, "-B", "-c", "import json; from pathlib import Path; assert json.loads(Path('variables.json').read_text()) == {'new': 1}"]}
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, auth=True, origin=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        headers = {"X-Router-Token": self.app.token} if auth else {}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection.request("POST" if data is not None else "GET", path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        body = response.read()
        status, content_type = response.status, response.getheader("Content-Type")
        connection.close()
        return status, json.loads(body) if content_type.startswith("application/json") else body.decode()

    def payload(self):
        from dataclasses import asdict
        task = easy_task()
        task.force = "sol"
        task.expected_files = ["variables.json"]
        return {"repo": str(self.repo), "task": asdict(task)}

    def test_assets_and_authenticated_bootstrap(self):
        for path in ("/", "/app.js", "/style.css", "/favicon.svg"):
            self.assertEqual(self.request(path, auth=False)[0], 200)
        self.assertEqual(self.request("/api/bootstrap", auth=False)[0], 403)
        status, data = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("default_profile", data)
        self.assertNotIn("token", data)

    def test_csrf_and_dns_rebinding_rejected(self):
        self.assertEqual(self.request("/api/settings", {}, origin="https://malicious.example")[0], 403)
        self.assertEqual(self.request("/api/bootstrap", host="malicious.example")[0], 403)
        self.assertEqual(self.request("/api/run", self.payload(), auth=False)[0], 403)

    def test_plan_and_example_do_not_execute(self):
        status, data = self.request("/api/plan", self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(data["route"]["executor"], "sol_medium")
        self.assertIn("Execution Budget", data["prompt"])
        self.assertIsNone(self.app.active)
        self.assertEqual(self.request("/api/example")[0], 200)

    def test_conversation_http_flow_and_auth(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':''}):
            self.assertEqual(self.request('/api/message', {'message':'需求'}, auth=False)[0],403)
            status,result=self.request('/api/message',{'message':'只给需求也能生成方案'})
            self.assertEqual(status,202)
            deadline=time.monotonic()+3
            while self.app.busy and time.monotonic()<deadline:
                time.sleep(.02)
            status,c=self.request('/api/conversations/'+result['conversation_id'])
            self.assertEqual(status,200)
            self.assertEqual(c['status'],'ready')
            self.assertEqual(c['repo'],'')
            status,saved=self.request('/api/proposal',{'conversation_id':c['id'],'revision':1,'edits':{'execution_prompt':'编辑后的真实执行正文'}})
            self.assertEqual(status,200)
            self.assertIn('编辑后的真实执行正文',saved['full_prompt'])
            self.assertEqual(self.request('/api/conversations',auth=False)[0],403)
            self.assertEqual(len(self.request('/api/conversations')[1]['conversations']),1)

    def test_settings_validation_and_no_plaintext_secret(self):
        with patch.dict(os.environ, {}, clear=False):
            status, result = self.request("/api/settings", {"api_key": "fixture-secret-only", "remember_key": False, "max_minutes": 12})
            self.assertEqual(status, 200)
            self.assertTrue(result["key_present"])
            saved = (self.app.state / "settings.json").read_text()
            self.assertNotIn("fixture-secret-only", saved)
            # v0.4 persists credential environment names, never inline key material.
            self.assertNotIn("api_key", json.loads(saved))
            self.assertEqual(self.request("/api/settings", {"commands": {"oops": "python check.py"}})[0], 400)
            self.assertEqual(self.request("/api/settings", {"max_minutes": -1})[0], 400)

    def test_artifact_traversal_rejected(self):
        self.assertEqual(self.request("/api/runs/../../outside")[0], 400)
        self.assertEqual(self.request("/api/artifact?run=../../&name=settings.json")[0], 400)

    def wait_job(self, job_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, job = self.request("/api/jobs/" + job_id)
            self.assertEqual(status, 200)
            if job["status"] != "running":
                return job
            time.sleep(.03)
        self.fail("job did not finish")

    def test_http_run_progress_history_and_promote(self):
        class Fixture:
            def run(self, **kw):
                (kw["work"] / "variables.json").write_text('{"new": 1}')
                kw["emit"](Event("summary", {"text": "fixture done"}))
        def factory(cfg, **kwargs):
            return RealOrchestrator(cfg, provider_factory=lambda *_: Fixture(), **kwargs)
        with patch("model_router.webapp.Orchestrator", side_effect=factory):
            status, data = self.request("/api/run", self.payload())
            self.assertEqual(status, 202)
            job = self.wait_job(data["job_id"])
        self.assertEqual(job["status"], "verified")
        self.assertTrue(job["events"])
        self.assertNotIn("cancel", job)
        self.assertEqual(json.loads((self.repo / "variables.json").read_text()), {"old": 1})
        status, detail = self.request("/api/runs/" + job["run_id"])
        self.assertIn("changes.diff", detail["files"])
        self.assertEqual(self.request("/api/artifact?run=" + job["run_id"] + "&name=../settings.json")[0], 400)
        self.assertEqual(self.request("/api/history")[1]["runs"][0]["status"], "verified")
        self.assertEqual(self.request("/api/promote", {"run_id": job["run_id"]})[0], 200)
        self.assertEqual(json.loads((self.repo / "variables.json").read_text()), {"new": 1})

    def test_cancel_and_single_active_run(self):
        class Fixture:
            def run(self, **kw):
                while True:
                    kw["monitor"].check()
                    time.sleep(.02)
        def factory(cfg, **kwargs):
            return RealOrchestrator(cfg, provider_factory=lambda *_: Fixture(), **kwargs)
        with patch("model_router.webapp.Orchestrator", side_effect=factory):
            _, data = self.request("/api/run", self.payload())
            self.assertEqual(self.request("/api/run", self.payload())[0], 400)
            self.assertEqual(self.request("/api/settings", {"max_minutes": 10})[0], 400)
            self.assertEqual(self.request("/api/cancel", {"job_id": data["job_id"]})[0], 200)
            job = self.wait_job(data["job_id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(self.request("/api/promote", {"run_id": job["run_id"]})[0], 400)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_dpapi_roundtrip_is_not_plaintext(self):
        path = self.root / "secret.dpapi"
        save_secret(path, "fixture-local-secret")
        self.assertNotIn(b"fixture-local-secret", path.read_bytes())
        self.assertEqual(load_secret(path), "fixture-local-secret")
