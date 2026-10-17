# ClinicScribe UI

Single-page demo UI: upload consult audio (one file or a batch), watch each job move through
diarization → ASR → Lightning agent, and review the transcript, SOAP note, tool calls, flags and MC.
Dark theme with large type for a projector. Plain HTML/CSS/JS, no build step and no CDN assets,
so it works with Wi-Fi off.

## Run it

```bash
pip install fastapi uvicorn
python serve_ui.py        # http://localhost:8000
```

Opening `ui/index.html` directly from disk also works (mock data only).

## Mock data vs the real backend

`app.js` picks the API on load:

| URL                | API used                                                    |
|--------------------|-------------------------------------------------------------|
| `/?api=mock`       | In-browser simulation (`api.js` → `MockApi`)                |
| `/?api=http`       | Real FastAPI backend (`api.js` → `HttpApi`)                 |
| `/` (default)      | Real backend if `GET /jobs` answers with a list, else mock |

The badge in the top right shows **MOCK DATA** or **LIVE**. The "Load demo batch" button only
appears in mock mode; it queues the hero consult (3-day MC on Panel A, limit 2) plus 10 short files.

To serve the UI from the real backend, mount the folder after the API routes:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="ui", html=True), name="ui")
```

## HTTP contract the backend should implement

```
POST /jobs                    multipart form, field "file"  -> {"job_id": "..."}
GET  /jobs                                                  -> [Job, ...]
GET  /jobs/{job_id}                                         -> Job
POST /jobs/{job_id}/approve                                 -> {"ok": true}
```

The UI polls `GET /jobs` once a second. A `Job` looks like:

```json
{
  "job_id": "job_001",
  "filename": "hero_consult_panelA.wav",
  "status": "transcribing",
  "audio_s": 164.0,
  "turns": [
    {"idx": 0, "speaker": "Doctor", "start": 0.0, "end": 3.4, "done": true, "text": "Good morning..."},
    {"idx": 1, "speaker": "Patient", "start": 3.8, "end": 10.2, "done": false}
  ],
  "created_at": 1760680000.0,
  "finished_at": null,
  "error": null,
  "approved": false,
  "result": null
}
```

- `status` is one of `queued`, `diarizing`, `transcribing`, `summarising`, `done`, `failed`.
- `turns` is filled once diarization finishes; flip `done` and add `text` as each turn is transcribed.
  Each turn is one chip in the job list.
- `result` is the output JSON contract from `CLAUDE.md` once `status == "done"`. `GET /jobs` may
  leave it `null` to stay small; the UI then fetches `GET /jobs/{id}` for the selected job.
- Optional: `result.timings.llm_tokens` (completion tokens) powers the "Lightning tokens/s" metric.
- `created_at` / `finished_at` (epoch seconds) drive the wall-time and real-time-factor metrics.

## Files

- `index.html` – layout
- `styles.css` – dark projector theme
- `app.js` – polling, rendering, SOAP typewriter, metrics
- `api.js` – `HttpApi` and `MockApi` (simulated queues: 2 diarization, 6 ASR, 6 agent slots)
- `mock/consults.js` – four mock consults in the output contract
