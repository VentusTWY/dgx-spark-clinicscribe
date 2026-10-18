"""ClinicScribe backend: the /jobs API the UI polls, plus the UI itself, on one port.

    python3 -m server            # http://localhost:8080 (PORT to change)

Pipeline (one process, asyncio queues, see CLAUDE.md):

    POST /jobs ─▶ first_layer_queue ─▶ first layer (diarization + ASR)  ─▶ agent_queue ─▶ Lightning agent ─▶ done
                  (skipped when the upload is already first-layer JSON)

Uploads:
  - first-layer JSON (wav, total_duration, segments [{speaker, start, end, text}]) goes straight to the agent.
  - audio goes to the first layer, configured with one of:
      STT_API=http://127.0.0.1:8100        the async_stt gateway (stt_gateway/api.py); turns light up live
      STT_URL=http://127.0.0.1:PORT/path   POST multipart field "file", JSON response
      STT_CMD='python3 ~/Documents/async_stt/run.py {audio}'   JSON on stdout, or written to {out}
    With neither set, a cached first-layer JSON with the same name (<stem>.json in SAMPLES_DIR) is used.

Patient details (panel, allergies) come from the JSON upload's "patient", an optional "patient" form
field, or samples/patients.json (filename glob -> patient).

Clinic terms (data/clinic_terms.json, see agent/terms.py) are read at the start of each agent run, so edits
made at /terms.html (GET/PUT /terms) apply to the next consult without a restart.

If the agent fails and out/<stem>.result.json exists (from a pre-run), that result is served instead
and marked meta.cached = true, so the demo survives Lightning going down.
"""

import asyncio
import fnmatch
import json
import os
import shlex
import subprocess
import tempfile
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent.llm import LLMClient
from agent.segments import is_segment_input, load_segments, run_agent_on_segments
from agent.terms import load_terms, save_terms

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
OUT_DIR = Path(os.environ.get("OUT_DIR", ROOT / "out"))
UPLOAD_DIR = OUT_DIR / "uploads"
SAMPLES_DIR = Path(os.path.expanduser(os.environ.get("SAMPLES_DIR", str(ROOT / "samples"))))
PATIENTS_FILE = Path(os.environ.get("PATIENTS_FILE", ROOT / "samples" / "patients.json"))
STT_API = os.environ.get("STT_API", "").rstrip("/")
STT_URL = os.environ.get("STT_URL", "")
STT_CMD = os.environ.get("STT_CMD", "")
# async_stt queues and scales its own GPU work, so many jobs can be in flight there at once.
STT_WORKERS = int(os.environ.get("STT_WORKERS", "8" if STT_API else "1"))
AGENT_WORKERS = int(os.environ.get("AGENT_WORKERS", "4"))
STT_POLL_S = float(os.environ.get("STT_POLL_S", "1"))
STT_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "1800"))
MAX_TRIES = 2

jobs = {}  # job_id -> job dict (the UI's Job shape plus private "_" keys)
first_layer_queue = asyncio.Queue()
agent_queue = asyncio.Queue()


# --- helpers ---------------------------------------------------------------

def public(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def parse_multipart(content_type, body):
    """Return {field: (filename, bytes)} using the stdlib, so python-multipart is not needed."""
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return fields


def patient_for(filename):
    try:
        table = json.loads(PATIENTS_FILE.read_text())
    except (OSError, ValueError):
        return {}, None
    for pattern, entry in table.items():
        if not pattern.startswith("_") and fnmatch.fnmatch(filename.lower(), pattern.lower()):
            return entry.get("patient", entry), entry.get("visit_date")
    return {}, None


def cached_first_layer(stem):
    for path in (SAMPLES_DIR / f"{stem}.json", SAMPLES_DIR / f"diarized_{stem}.json"):
        if path.exists():
            data = json.loads(path.read_text())
            if is_segment_input(data):
                return data
    return None


def _multipart_file(path):
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _local_json(url, body=None, ctype=None, timeout=60):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype} if ctype else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local service, never a proxy
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


async def run_stt_api(job):
    """Submit to the async_stt gateway and mirror its per-turn progress onto the job, so chips light up."""
    body, ctype = _multipart_file(job["_path"])
    stt_id = (await asyncio.to_thread(_local_json, f"{STT_API}/jobs", body, ctype))["job_id"]
    started = time.time()
    while time.time() - started < STT_TIMEOUT:
        st = await asyncio.to_thread(_local_json, f"{STT_API}/jobs/{stt_id}")
        if st["status"] == "failed":
            raise RuntimeError(st.get("error") or "async_stt job failed")
        if st.get("total_duration"):
            job["audio_s"] = st["total_duration"]
        if st["turns"]:
            job["turns"] = [{**t, "speaker": f"Speaker {t['speaker']}"} for t in st["turns"]]
            job["status"] = "transcribing"
        if st["status"] == "done":
            return st["result"]
        await asyncio.sleep(STT_POLL_S)
    raise TimeoutError(f"async_stt job {stt_id} still running after {STT_TIMEOUT}s")


def run_first_layer(audio_path):
    """Call the team's diarization + ASR first layer. Blocking; runs in a thread."""
    if STT_URL:
        body, ctype = _multipart_file(audio_path)
        return _local_json(STT_URL, body, ctype, timeout=STT_TIMEOUT)
    if STT_CMD:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "first_layer.json"
            cmd = STT_CMD.format(audio=shlex.quote(str(audio_path)), out=shlex.quote(str(out_path)))
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=STT_TIMEOUT)
            if proc.returncode != 0:
                raise RuntimeError(f"STT_CMD exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
            if "{out}" in STT_CMD:
                return json.loads(out_path.read_text())
            text = proc.stdout.strip()
            return json.loads(text[text.find("{"):] if "{" in text else text)
    cached = cached_first_layer(audio_path.stem.split("__", 1)[-1])
    if cached is not None:
        return cached
    raise RuntimeError("No first layer configured: set STT_API, STT_URL or STT_CMD, or upload the first-layer JSON")


def set_turns(job, data):
    segments, audio = load_segments(data)
    job["audio_s"] = audio["duration_s"]
    job["turns"] = [
        {"idx": i, "speaker": f"Speaker {s['speaker_id']}", "start": s["start"], "end": s["end"], "done": True, "text": s["text"]}
        for i, s in enumerate(segments)
    ]


def fail(job, message):
    job["status"] = "failed"
    job["error"] = message
    job["finished_at"] = time.time()


# --- workers ---------------------------------------------------------------

async def first_layer_worker():
    while True:
        job = jobs[await first_layer_queue.get()]
        job["status"] = "diarizing"
        started = time.time()
        try:
            data = await (run_stt_api(job) if STT_API else asyncio.to_thread(run_first_layer, job["_path"]))
            if not is_segment_input(data):
                raise ValueError("First layer returned JSON without a segments list")
            job["_data"] = data
            upstream = {k: v for k, v in (data.get("timings") or {}).items() if v is not None}
            job["_timings"] = {"diarization_s": None, "asr_s": round(time.time() - started, 2), **upstream}
            set_turns(job, data)
            job["status"] = "summarising"
            await agent_queue.put(job["job_id"])
        except Exception as e:  # noqa: BLE001 - any failure goes to the UI
            job["_tries"] += 1
            if job["_tries"] < MAX_TRIES:
                job["status"] = "queued"
                await first_layer_queue.put(job["job_id"])
            else:
                fail(job, f"first layer: {e}")


async def agent_worker():
    while True:
        job = jobs[await agent_queue.get()]
        job["status"] = "summarising"
        data, patient, visit_date = job["_data"], job["_patient"], job["_visit_date"]
        try:
            result = await asyncio.to_thread(run_agent_on_segments, data, patient, visit_date, LLMClient())
            (OUT_DIR / f"{job['_stem']}.result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            job["_agent_tries"] += 1
            if job["_agent_tries"] < MAX_TRIES:
                await agent_queue.put(job["job_id"])
                continue
            cached = OUT_DIR / f"{job['_stem']}.result.json"
            if not cached.exists():
                fail(job, f"agent: {e}")
                continue
            result = json.loads(cached.read_text())
            result.setdefault("meta", {}).update(cached=True, cache_reason=str(e))
        timings = {**job["_timings"], **result.get("timings", {})}
        timings["total_s"] = round(time.time() - job["created_at"], 2)
        result["timings"] = timings
        roles = result.get("speakers", {}).get("roles", {})
        for t in job["turns"]:
            t["speaker"] = roles.get(t["speaker"].removeprefix("Speaker "), t["speaker"])
        job["result"] = result
        job["status"] = "done"
        job["finished_at"] = time.time()


@asynccontextmanager
async def lifespan(_app):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [asyncio.create_task(first_layer_worker()) for _ in range(STT_WORKERS)]
    tasks += [asyncio.create_task(agent_worker()) for _ in range(AGENT_WORKERS)]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ClinicScribe", lifespan=lifespan)


# --- routes ----------------------------------------------------------------

@app.post("/jobs")
async def create_job(request: Request):
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("multipart/form-data"):
        raise HTTPException(400, "Send multipart/form-data with a 'file' field")
    fields = parse_multipart(ctype, await request.body())
    if "file" not in fields:
        raise HTTPException(400, "Missing 'file' field")
    filename, content = fields["file"]
    filename = Path(filename or "upload").name
    stem = Path(filename).stem
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    patient, visit_date = patient_for(filename)
    if "patient" in fields:
        try:
            patient = json.loads(fields["patient"][1])
        except ValueError:
            raise HTTPException(400, "'patient' must be JSON")
    job = {
        "job_id": job_id, "filename": filename, "status": "queued", "audio_s": None, "turns": [],
        "created_at": time.time(), "finished_at": None, "error": None, "approved": False, "result": None,
        "_stem": stem, "_patient": patient, "_visit_date": visit_date, "_tries": 0, "_agent_tries": 0,
        "_timings": {"diarization_s": None, "asr_s": None},
    }

    if filename.lower().endswith(".json"):
        try:
            data = json.loads(content)
        except ValueError:
            raise HTTPException(400, "Not valid JSON")
        if not is_segment_input(data):
            raise HTTPException(400, "JSON needs a segments list (first-layer output)")
        if isinstance(data, dict):
            job["_patient"] = data.get("patient") or job["_patient"]
            job["_visit_date"] = data.get("visit_date") or job["_visit_date"]
            job["_timings"].update(data.get("timings") or {})
        job["_data"] = data
        set_turns(job, data)
        job["status"] = "summarising"
        jobs[job_id] = job
        await agent_queue.put(job_id)
    else:
        path = UPLOAD_DIR / f"{job_id}__{filename}"
        path.write_bytes(content)
        job["_path"] = path
        jobs[job_id] = job
        await first_layer_queue.put(job_id)
    return {"job_id": job_id}


@app.get("/jobs")
async def list_jobs():
    # Results stay in the list (a few KB each) so the metrics bar can sum every finished job.
    return [public(j) for j in jobs.values()]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "job not found")
    return public(jobs[job_id])


@app.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "job not found")
    jobs[job_id]["approved"] = True
    return {"ok": True}


@app.get("/terms")
async def get_terms():
    return {"terms": load_terms()}


@app.put("/terms")
async def put_terms(request: Request):
    try:
        body = await request.json()
        terms = await asyncio.to_thread(save_terms, body.get("terms") if isinstance(body, dict) else body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"terms": terms}


@app.get("/health")
async def health():
    def check():
        client = LLMClient(timeout=5)
        try:
            return {"ok": True, "llm": client.base_url, "model": client.model}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "llm": client.base_url, "error": str(e)}
    first = f"async_stt at {STT_API}" if STT_API else "STT_URL" if STT_URL else "STT_CMD" if STT_CMD else f"cached JSON in {SAMPLES_DIR}"
    return {**await asyncio.to_thread(check), "first_layer": first, "jobs": len(jobs)}


app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
