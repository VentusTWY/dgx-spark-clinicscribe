# ClinicScribe UI

Single-page demo UI: upload consult audio (one file or a batch), watch each job move through
diarization → ASR → Lightning agent, and review the transcript, SOAP note, tool calls, flags and MC.
Dark theme with large type for a projector. Plain HTML/CSS/JS, no build step and no CDN assets,
so it works with Wi-Fi off.

## Run it

```bash
pip install fastapi uvicorn
python serve_ui.py        # http://localhost:8080 (set PORT to change)
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
appears in mock mode; it queues the hero consult (3-day MC on the AIA panel, limit 2) plus 10 short files.

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
  "filename": "hero_consult_aia.wav",
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
- `result` is set once `status == "done"`: the agent output from `run_agent(...)`
  ([docs/agent_contract.md](../docs/agent_contract.md)) with two keys the pipeline adds:
  `transcript` (`[{speaker, start, end, text}]`, optional, else the UI uses `turns`) and
  `timings` (`{diarization_s, asr_s, agent_s, total_s, audio_s, llm_tokens}`; `agent_s` falls back to
  `meta.latency_ms`). The original `CLAUDE.md` contract shape is also accepted; `normalizeResult`
  in `app.js` maps both. `GET /jobs` may leave `result` null to stay small; the UI then fetches
  `GET /jobs/{id}` for the selected job.
- Optional: `timings.llm_tokens` (completion tokens) powers the "Lightning tokens/s" metric.
- `created_at` / `finished_at` (epoch seconds) drive the wall-time and real-time-factor metrics.

## Clinician view and /dashboard

`/` is the clinician view: consult list, transcript, SOAP note, the items to review
(flags, MC, prescriptions, panel rules) and Approve. Model names, pipeline stages, tool calls,
timings and the metrics bar are left out.

`/dashboard/` is the same app with all of that shown: turn chips, pipeline stages and timings,
the agent's tool calls, flags the verifier dropped, and the metrics bar. Use it for demos and
debugging. Each page links to the other from the header. With the real backend both pages show
the same jobs; in mock mode each page runs its own simulation.

On phones (860 px wide or less) both pages switch to one column. The consult list and the
open consult take turns on screen, with an "All consults" button to go back.

## What the metrics bar measures

In LIVE mode every number comes from the backend; nothing is estimated in the browser.

| Metric | Source |
|---|---|
| Audio minutes processed | `result.timings.audio_s` (audio length from the first layer) of finished jobs |
| Wall time | Server `created_at` → `finished_at`, counting only time when at least one job was in flight |
| To process 1 min of audio | Busy wall time of finished jobs × 60 ÷ their audio seconds (and the real-time factor under it) |
| Lightning tokens/s per consult | Completion tokens reported by vLLM's `usage` field ÷ `agent_s`, averaged over finished jobs. `agent_s` includes prompt processing, tool execution and the verifier pass, so this is lower than raw decode speed |
| API cost | RM0: nothing leaves the ATOM |

Results served from `out/<stem>.result.json` because Lightning failed (`meta.cached`) are marked in the
detail view and left out of the metrics. The SOAP note "typing" is a replay of the finished note, not
token streaming.

## Files

- `index.html` – layout (clinician view)
- `dashboard/index.html` – technical dashboard: same markup with `data-view="tech"` and `<base href="../">`
- `styles.css` – dark projector theme
- `app.js` – polling, rendering, SOAP typewriter, metrics
- `api.js` – `HttpApi` and `MockApi` (simulated queues: 2 diarization, 6 ASR, 6 agent slots)
- `mock/consults.js` – four mock consults (the hero in the agent's output shape)
