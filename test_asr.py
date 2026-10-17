import json
import os
from pathlib import Path
import sys
import time
import uuid

from celery import Celery

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "mysecurepassword")

BROKER_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0"

# Always points to 'data/jobs' relative to where test_asr.py is located
PROJECT_ROOT = Path(__file__).resolve().parent
JOBS_DIR = Path(
    os.getenv("JOBS_DIR", str(PROJECT_ROOT / "data" / "jobs"))
).resolve()

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
TIMEOUT_SECONDS = float(os.getenv("TIMEOUT_SECONDS", "3600"))

DONE_STATUSES = {"completed", "complete", "done", "success", "succeeded", "finished"}
FAILED_STATUSES = {"failed", "failure", "error", "cancelled"}

client = Celery("pipeline_client", broker=BROKER_URL)


def read_job(job_id):
    job_path = JOBS_DIR / f"{job_id}.json"

    if not job_path.exists():
        return None

    try:
        with job_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # The writer may be updating the JSON file right now.
        return None


def print_status(job, elapsed):
    status = str(job.get("status", "unknown")).strip().lower()

    print(
        f"\n{'-'*50}\n"
        f"[{time.strftime('%H:%M:%S')}] Elapsed: {elapsed:.0f}s | Overall Status: {status.upper()}\n"
        f"{'-'*50}",
        flush=True,
    )

    tasks = job.get("tasks", {})
    if not tasks:
        print("  ⏳ No tasks registered yet. Waiting for worker...", flush=True)
        return

    total_tasks = len(tasks)
    completed_tasks = sum(
        1
        for t in tasks.values()
        if str(t.get("status", "")).strip().lower() in DONE_STATUSES
    )
    failed_tasks = sum(
        1
        for t in tasks.values()
        if str(t.get("status", "")).strip().lower() in FAILED_STATUSES
    )

    print(
        f"  Progress: {completed_tasks}/{total_tasks} tasks completed"
        + (f" ({failed_tasks} failed)" if failed_tasks > 0 else "")
        + "\n",
        flush=True,
    )

    for task_id, task in tasks.items():
        task_status = str(task.get("status", "unknown")).strip().lower()
        task_type = task.get("task_type", "task")

        if task_status in DONE_STATUSES:
            icon = "✅"
        elif task_status in FAILED_STATUSES:
            icon = "❌"
        elif task_status in {"running", "in_progress", "processing"}:
            icon = "🔄"
        else:
            icon = "⏳"

        print(
            f"  {icon} [{task_status.upper():12}] {task_type} (ID: {task_id})",
            flush=True,
        )


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {Path(sys.argv[0]).name} /path/to/audio.wav")
        sys.exit(1)

    audio_path = Path(sys.argv[1]).expanduser().resolve()

    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}")
        sys.exit(1)

    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    job_id = str(uuid.uuid4())
    job_path = JOBS_DIR / f"{job_id}.json"

    payload = {
        "task_id": job_id,
        "job_id": job_id,
        "audio_path": str(audio_path),
        "language": os.getenv("ASR_LANGUAGE", "auto"),
    }

    print(f"🎧 Audio: {audio_path}")
    print(f"🆔 Job ID: {job_id}")
    print(f"📁 Job results path: {job_path}")

    client.send_task(
        "run_pipeline",
        args=[payload],
        queue="pipeline_queue",
        task_id=job_id,
    )

    print("\n🚀 Pipeline submitted. Waiting for results...", flush=True)

    start = time.monotonic()
    last_printed_signature = None

    while True:
        elapsed = time.monotonic() - start
        job = read_job(job_id)

        if job is not None:
            status = str(job.get("status", "unknown")).strip().lower()

            tasks = job.get("tasks") or {}
            signature = (
                status,
                tuple(
                    sorted(
                        (
                            str(task.get("task_type", "task")),
                            str(task.get("status", "unknown")).strip().lower(),
                        )
                        for task in tasks.values()
                    )
                ),
            )

            if signature != last_printed_signature:
                print_status(job, elapsed)
                last_printed_signature = signature

            if status in DONE_STATUSES:
                print("\n" + "=" * 50, flush=True)
                print("🎉 === FINAL RESULT ===", flush=True)
                print("=" * 50, flush=True)
                print(
                    json.dumps(
                        job.get("result"),
                        indent=2,
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                print(f"\n📂 Full job JSON saved at: {job_path}", flush=True)
                return

            if status in FAILED_STATUSES:
                print("\n" + "=" * 50, flush=True)
                print("❌ === PIPELINE FAILED ===", flush=True)
                print("=" * 50, flush=True)
                print(job.get("error", "Unknown error"), flush=True)
                print(json.dumps(job, indent=2, ensure_ascii=False), flush=True)
                sys.exit(1)

        else:
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Elapsed: {elapsed:.0f}s | Waiting for job file to be created...",
                flush=True,
            )

        if elapsed >= TIMEOUT_SECONDS:
            print(f"\n⏰ Timed out waiting. Job ID: {job_id}", flush=True)
            print(f"Check the job file later: {job_path}", flush=True)
            sys.exit(2)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()