"""FastAPI front door for the team's async_stt pipeline (diarization + ASR on Ray Serve / Celery).

It submits a wav to the existing `run_pipeline` task on `pipeline_queue` and reads progress from the
job files that common/job_store.py writes. Nothing in async_stt changes.

It runs inside the env_edwin container, next to the workers (same /workspace paths, Redis settings
and root-owned data/ folder). scripts/start_stt_gateway.sh copies it into async_stt and starts it on
127.0.0.1:8100.

Routes:
    POST /jobs                 multipart "file" (+ optional "language")   -> {"job_id"}
    GET  /jobs/{job_id}        status, per-turn progress, and the first-layer transcript once completed
    POST /transcribe           multipart "file"; waits and returns the first-layer transcript
    GET  /health

First-layer transcript (what ClinicScribe's agent reads):
    {"job_id", "wav", "total_duration", "segments": [{"speaker", "start", "end", "text"}], "timings"}
"""

import asyncio
import os
import time
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

from celery import Celery
from fastapi import FastAPI, HTTPException, Request

from common.job_store import create_job, get_job

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "mysecurepassword")
BROKER_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./data/uploads")).resolve()
LANGUAGE = os.getenv("ASR_LANGUAGE", "auto")
TRANSCRIBE_TIMEOUT = float(os.getenv("TRANSCRIBE_TIMEOUT", "1800"))

celery_client = Celery("stt_gateway", broker=BROKER_URL)
app = FastAPI(title="async_stt gateway")


def parse_multipart(content_type, body):
    """{field: (filename, bytes)} with the stdlib, so python-multipart is not needed."""
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return fields


def _ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def summarise(job):
    """Turn a job file into status + turns (one per diarization segment) + first-layer result."""
    tasks = job.get("tasks", {}).values()
    diar = next((t for t in tasks if t.get("task_type") == "diarization"), None)
    asr_tasks = [t for t in tasks if t.get("task_type") == "asr"]
    wav = job.get("input_data", {}).get("audio_path", "")
    duration = (diar or {}).get("duration")

    # The pipeline names each cut "<diarization index:05d>_<uuid>.wav", which ties ASR tasks to turns.
    asr_by_index = {}
    for t in asr_tasks:
        name = Path(t.get("audio_path", "")).name
        if name[:5].isdigit():
            asr_by_index[int(name[:5])] = t

    turns = []
    for i, seg in enumerate(((diar or {}).get("result") or {}).get("segments", [])):
        t = asr_by_index.get(i)
        done = bool(t and t.get("status") == "completed")
        turns.append({
            "idx": i, "speaker": str(seg["speaker"]), "start": seg["start"], "end": seg["end"], "done": done,
            **({"text": (t.get("result") or {}).get("text", "")} if done else {}),
        })

    if job.get("status") == "failed":
        stage = "failed"
    elif job.get("status") == "completed":
        stage = "done"
    elif asr_tasks:
        stage = "transcribing"
    elif diar:
        stage = "diarizing"
    else:
        stage = "queued"

    out = {
        "job_id": job["job_id"], "status": stage, "error": job.get("error"), "wav": Path(wav).name,
        "total_duration": duration, "turns": turns,
        "turns_done": sum(t["done"] for t in turns), "turns_total": len(turns), "result": None,
    }
    if job.get("status") == "completed":
        result = job.get("result") or {}
        segments = [
            {"speaker": s["speaker"], "start": s["start"], "end": s["end"], "text": s.get("text", "").strip()}
            for s in result.get("segments", [])
        ]
        created, finished = _ts(job.get("created_at")), _ts(job.get("updated_at"))
        d0, d1 = (_ts(diar.get("created_at")), _ts(diar.get("updated_at"))) if diar else (None, None)
        diar_s = d1 - d0 if d0 and d1 else None
        total_s = finished - created if created and finished else None
        out["result"] = {
            "job_id": job["job_id"], "wav": Path(wav).name,
            "total_duration": duration or (segments[-1]["end"] if segments else 0.0),
            "segments": segments,
            "timings": {
                "diarization_s": round(diar_s, 2) if diar_s is not None else None,
                "asr_s": round(total_s - diar_s, 2) if total_s is not None and diar_s is not None else None,
            },
        }
    return out


def submit(filename, content, language):
    job_id = str(uuid.uuid4())
    suffix = Path(filename or "audio.wav").suffix or ".wav"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{job_id}{suffix}"
    path.write_bytes(content)
    create_job(job_id, pipeline="diarization_asr", input_data={"audio_path": str(path), "language": language})
    payload = {"task_id": job_id, "job_id": job_id, "audio_path": str(path), "language": language}
    celery_client.send_task("run_pipeline", args=[payload], queue="pipeline_queue", task_id=job_id)
    return job_id


async def read_upload(request):
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("multipart/form-data"):
        raise HTTPException(400, "Send multipart/form-data with a 'file' field")
    fields = parse_multipart(ctype, await request.body())
    if "file" not in fields:
        raise HTTPException(400, "Missing 'file' field")
    language = fields["language"][1].decode().strip() if "language" in fields else LANGUAGE
    return fields["file"][0], fields["file"][1], language


@app.post("/jobs")
async def post_job(request: Request):
    filename, content, language = await read_upload(request)
    return {"job_id": await asyncio.to_thread(submit, filename, content, language)}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    try:
        job = await asyncio.to_thread(get_job, job_id)
    except ValueError:
        raise HTTPException(400, "Invalid job id")
    if job is None:
        raise HTTPException(404, "job not found")
    return summarise(job)


@app.post("/transcribe")
async def transcribe(request: Request):
    filename, content, language = await read_upload(request)
    job_id = await asyncio.to_thread(submit, filename, content, language)
    started = time.monotonic()
    while time.monotonic() - started < TRANSCRIBE_TIMEOUT:
        job = await asyncio.to_thread(get_job, job_id)
        if job and job.get("status") == "completed":
            return summarise(job)["result"]
        if job and job.get("status") == "failed":
            raise HTTPException(502, f"async_stt job {job_id} failed: {job.get('error')}")
        await asyncio.sleep(1)
    raise HTTPException(504, f"async_stt job {job_id} still running; poll GET /jobs/{job_id}")


@app.get("/health")
async def health():
    return {"ok": True, "broker": f"redis://{REDIS_HOST}:{REDIS_PORT}/0", "upload_dir": str(UPLOAD_DIR)}
