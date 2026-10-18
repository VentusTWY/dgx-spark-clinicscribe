"""Backend test: upload first-layer JSON and audio, poll until done. Uses the fake vLLM from test_agent.

Run: python3 -m unittest discover tests   (needs fastapi + uvicorn)
"""

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import HTTPServer
from pathlib import Path

from tests.test_agent import FakeVLLM

ROOT = Path(__file__).resolve().parent.parent

try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def multipart(filename, content):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


@unittest.skipIf(uvicorn is None, "fastapi/uvicorn not installed")
class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm = HTTPServer(("127.0.0.1", 0), FakeVLLM)
        threading.Thread(target=cls.llm.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.terms_file = Path(cls.tmp) / "clinic_terms.json"
        shutil.copy(ROOT / "data" / "clinic_terms.json", cls.terms_file)
        os.environ["CLINIC_TERMS_FILE"] = str(cls.terms_file)
        os.environ.update(LLM_BASE_URL=f"http://127.0.0.1:{cls.llm.server_port}/v1", OUT_DIR=cls.tmp,
                          AGENT_WORKERS="1", SAMPLES_DIR=str(ROOT / "samples"))
        from server.app import app
        cls.port = free_port()
        cls.srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="error"))
        threading.Thread(target=cls.srv.run, daemon=True).start()
        while not cls.srv.started:
            time.sleep(0.05)
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.should_exit = True
        cls.llm.shutdown()

    def call(self, path, body=None, ctype=None, method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     headers={"Content-Type": ctype} if ctype else {},
                                     method=method or ("POST" if body is not None else "GET"))
        with self.opener.open(req, timeout=10) as r:
            return json.loads(r.read())

    def wait_done(self, job_id):
        for _ in range(200):
            job = self.call(f"/jobs/{job_id}")
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def upload(self, filename, content):
        FakeVLLM.step = 0
        return self.call("/jobs", *multipart(filename, content))["job_id"]

    def check_result(self, job):
        self.assertEqual(job["status"], "done", job["error"])
        self.assertEqual(job["turns"][0]["speaker"], "Doctor")
        r = job["result"]
        self.assertEqual(r["panel"]["id"], "PANEL-AIA")
        self.assertIn("exceeds AIA Corporate Panel limit", " ".join(f["message"] for f in r["flags"]))
        self.assertEqual(r["timings"]["audio_s"], 100.1)
        self.assertIn("total_s", r["timings"])

    def test_first_layer_json_upload(self):
        job = self.wait_done(self.upload("consult.json", (ROOT / "samples" / "diarized_urti.json").read_bytes()))
        self.check_result(job)
        listed = self.call("/jobs")
        self.assertTrue(any(j["job_id"] == job["job_id"] and j["result"] for j in listed))
        self.assertEqual(self.call(f"/jobs/{job['job_id']}/approve", b""), {"ok": True})
        self.assertTrue(self.call(f"/jobs/{job['job_id']}")["approved"])

    def test_audio_upload_uses_cached_first_layer(self):
        job = self.wait_done(self.upload("urti.wav", b"RIFF fake audio"))
        self.check_result(job)

    def test_audio_without_first_layer_fails_clearly(self):
        job = self.wait_done(self.upload("unknown.wav", b"RIFF"))
        self.assertEqual(job["status"], "failed")
        self.assertIn("STT_URL", job["error"])

    def test_terms_can_be_read_and_edited(self):
        terms = self.call("/terms")["terms"]
        self.assertTrue(any(t["term"] == "Bisolvon" for t in terms))
        new = terms + [{"term": "Difflam", "kind": "drug", "meaning": "Benzydamine spray", "mishearings": "diff lamb"}]
        saved = self.call("/terms", json.dumps({"terms": new}).encode(), "application/json", "PUT")["terms"]
        self.assertEqual(saved[-1]["mishearings"], ["diff lamb"])
        self.assertEqual(json.loads(self.terms_file.read_text())["terms"], saved)
        dup = json.dumps({"terms": [{"term": "MC"}, {"term": "MC"}]}).encode()
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.call("/terms", dup, "application/json", "PUT")
        self.assertEqual(err.exception.code, 400)

    def test_ui_is_served(self):
        with self.opener.open(f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            self.assertIn(b"ClinicScribe", r.read())


if __name__ == "__main__":
    unittest.main()
