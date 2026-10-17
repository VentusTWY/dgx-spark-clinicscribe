"""async_stt gateway + ClinicScribe STT_API path, against a simulated async_stt pipeline.

Celery and async_stt's common.job_store are stubbed: send_task starts a thread that writes the same
job-file shape the real pipeline writes (diarization task, one ASR task per cut, final result).
"""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from tests.test_agent import FakeVLLM
from tests.test_server import free_port, multipart

ROOT = Path(__file__).resolve().parent.parent

try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None

JOBS = {}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def create_job(job_id, *, pipeline="inference", input_data=None):
    JOBS[job_id] = {"job_id": job_id, "pipeline": pipeline, "status": "queued", "input_data": input_data or {},
                    "tasks": {}, "result": None, "error": None, "created_at": _now(), "updated_at": _now()}
    return JOBS[job_id]


def get_job(job_id):
    return json.loads(json.dumps(JOBS[job_id])) if job_id in JOBS else None


def simulate_pipeline(payload):
    job = JOBS[payload["job_id"]]
    job["status"] = "processing"
    segs = json.loads((ROOT / "samples" / "diarized_urti.json").read_text())["segments"]
    time.sleep(0.1)
    job["tasks"]["d"] = {"task_type": "diarization", "status": "completed", "created_at": job["created_at"],
                         "updated_at": _now(), "duration": 100.1,
                         "result": {"segments": [{"speaker": str(s["speaker"]), "start": s["start"], "end": s["end"]} for s in segs]}}
    for i, s in enumerate(segs):
        job["tasks"][f"a{i}"] = {"task_type": "asr", "status": "processing", "audio_path": f"/x/{i:05d}_u.wav"}
    for i, s in enumerate(segs):
        time.sleep(0.02)
        job["tasks"][f"a{i}"].update(status="completed", result={"text": s["text"]})
    job["result"] = {"segments": [{**s, "speaker": str(s["speaker"])} for s in segs]}
    job["updated_at"] = _now()
    job["status"] = "completed"


class FakeCelery:
    def __init__(self, *a, **k):
        pass

    def send_task(self, name, args, queue, task_id):
        assert (name, queue) == ("run_pipeline", "pipeline_queue")
        threading.Thread(target=simulate_pipeline, args=(args[0],), daemon=True).start()


@unittest.skipIf(uvicorn is None, "fastapi/uvicorn not installed")
class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("celery", types.SimpleNamespace(Celery=FakeCelery))
        sys.modules["common"] = types.ModuleType("common")
        sys.modules["common.job_store"] = types.SimpleNamespace(create_job=create_job, get_job=get_job)
        cls.tmp = tempfile.mkdtemp()
        os.environ["UPLOAD_DIR"] = cls.tmp
        sys.path.insert(0, str(ROOT / "stt_gateway"))
        import api
        cls.port = free_port()
        cls.srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=cls.port, log_level="error"))
        threading.Thread(target=cls.srv.run, daemon=True).start()
        while not cls.srv.started:
            time.sleep(0.05)
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.should_exit = True

    def call(self, path, body=None, ctype=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     headers={"Content-Type": ctype} if ctype else {})
        with self.opener.open(req, timeout=10) as r:
            return json.loads(r.read())

    def test_transcribe_returns_first_layer(self):
        out = self.call("/transcribe", *multipart("consult.wav", b"RIFF"))
        self.assertEqual(out["total_duration"], 100.1)
        self.assertEqual(len(out["segments"]), 17)
        self.assertTrue(out["segments"][0]["text"].startswith("Good morning"))

    def test_job_progress(self):
        job_id = self.call("/jobs", *multipart("consult.wav", b"RIFF"))["job_id"]
        seen = set()
        for _ in range(200):
            st = self.call(f"/jobs/{job_id}")
            seen.add(st["status"])
            if st["status"] == "done":
                break
            time.sleep(0.02)
        self.assertIn("transcribing", seen)
        self.assertEqual(st["turns_done"], st["turns_total"])
        self.assertEqual(st["result"]["wav"], f"{job_id}.wav")

    def test_clinicscribe_via_stt_api(self):
        llm = HTTPServer(("127.0.0.1", 0), FakeVLLM)
        threading.Thread(target=llm.serve_forever, daemon=True).start()
        FakeVLLM.step = 0
        script = (f"import os,sys; sys.path.insert(0, {str(ROOT)!r}); "
                  "from server.app import app; import uvicorn; "
                  "uvicorn.run(app, host='127.0.0.1', port=int(os.environ['PORT']), log_level='error')")
        port = free_port()
        import subprocess
        env = {**os.environ, "STT_API": f"http://127.0.0.1:{self.port}", "PORT": str(port), "OUT_DIR": self.tmp,
               "LLM_BASE_URL": f"http://127.0.0.1:{llm.server_port}/v1", "AGENT_WORKERS": "1", "STT_POLL_S": "0.05"}
        proc = subprocess.Popen([sys.executable, "-c", script], env=env, cwd=ROOT)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    self.opener.open(base + "/health", timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            body, ctype = multipart("hero.wav", b"RIFF")
            req = urllib.request.Request(base + "/jobs", data=body, headers={"Content-Type": ctype})
            job_id = json.loads(self.opener.open(req, timeout=10).read())["job_id"]
            statuses = set()
            for _ in range(300):
                job = json.loads(self.opener.open(f"{base}/jobs/{job_id}", timeout=5).read())
                statuses.add(job["status"])
                if job["status"] in ("done", "failed"):
                    break
                time.sleep(0.03)
            self.assertEqual(job["status"], "done", job["error"])
            self.assertIn("transcribing", statuses)
            self.assertEqual(job["turns"][0]["speaker"], "Doctor")
            self.assertEqual(job["result"]["timings"]["audio_s"], 100.1)
        finally:
            proc.terminate()
            llm.shutdown()


if __name__ == "__main__":
    unittest.main()
