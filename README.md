# ClinicScribe

**Team Omega Vision · ACE 2026 Malaysia (GIGABYTE x NVIDIA, Agentic Computing at the Edge) · winning project**

A GP needs a trustworthy record from every consult, without patient data leaving the clinic.
ClinicScribe takes the recording of a GP consultation and produces a checked SOAP note, a drafted
MC and a panel-claim check. Everything runs on the team's **GIGABYTE AI TOP ATOM** (GB10, DGX Spark
class) with Wi-Fi off: three local NVIDIA models, five tools whose facts are computed in code, and one
doctor approval at the end. No cloud model, no speech API, RM0 API cost.

| Problem | ClinicScribe |
|---|---|
| Notes after every visit: the SOAP note, MC and claim are typed up by hand. | Drafted from the conversation: speaker-labelled transcript to SOAP note and MC. |
| Rules checked too late: allergy, dose and panel limits slip through, such as a 3-day MC on a 2-day panel. | Checked before approval: code-based checks and a verifier pass raise flags with fixes. |
| Audio leaves the clinic: cloud scribes send sensitive recordings off-site. | 100% local on the ATOM, Wi-Fi off. |

## How it works

```
Upload ─▶ Diarize ─▶ ASR ─▶ Agent ─▶ Review
queued → diarizing → transcribing (k/n turns) → summarising → done
```

1. **Hear.** NVIDIA Sortformer works out who is speaking and when; Nemotron 3.5 ASR Streaming 0.6B
   transcribes each turn with timestamps (the team's `async_stt` pipeline on Ray Serve + Celery).
2. **Draft.** Nemotron 3.5 Lightning (30B MoE, 3B active), served locally by vLLM, labels Doctor and
   Patient and calls five tools: `get_panel_rules`, `check_drug`, `draft_mc`, `flag`, `write_soap_note`.
3. **Check.** Allergies, max dose, interactions, MC dates and panel limits are computed in Python over
   local JSON, not taken from the model's memory. A second Lightning call (the verifier) flags anything
   unsupported or missing.
4. **Approve.** The doctor reviews the flags and presses Approve.

Example flag from the demo: *3-day MC exceeds the panel limit of 2 days. Reduce to 2 days or add a reason.*

**Scope.** The speech model is English only. The formulary and panel rules in `data/` are synthetic.
A pilot would need real formulary and panel rules, clinician validation of notes, MCs and flags, and a
data protection and regulatory review.

**Links.** [Pitch deck (PDF)](docs/ClinicScribe-Omega-Vision-Deck.pdf). Mock-data page (no backend, simulated
numbers): [ventustan.com/clinicscribe](https://ventustan.com/clinicscribe/).

| Path | What |
|---|---|
| `server/`, `clinicscribe`, `scripts/start_demo.sh` | FastAPI backend: `/jobs` API, async queues, serves the UI on :8080 |
| `stt_gateway/`, `scripts/start_stt_gateway.sh` | Front door to the team's `async_stt` diarization + ASR pipeline |
| `agent/` | Lightning agent, tools, verifier (stdlib Python) |
| `ui/` | Single-page UI, no build step, no CDN |
| `data/` | Synthetic formulary and panel rules |
| `samples/` | Sample consults, first-layer JSON and agent output |
| `host/async_stt/` | Snapshot of the team's original host pipeline sources (reference only) |

## Run the demo (API + UI, fully offline)

On the ATOM, with Lightning already up (`demo kit-up`):

```bash
./clinicscribe                   # first run makes .venv and installs; then http://localhost:8080
```

No Docker and no system-wide pip installs: `./clinicscribe` keeps its two packages (FastAPI,
Uvicorn) in a Python venv inside the repo. Other commands:

| Command | What it does |
|---|---|
| `./clinicscribe install` | Create `.venv` and install `requirements.txt`. Uses `vendor/wheels` with no network when present. |
| `./clinicscribe vendor` | While online, save the wheels (and pip) to `vendor/wheels` so installs work with Wi-Fi off. |
| `./clinicscribe bundle` | Vendor, then pack the repo and wheels into `dist/clinicscribe-offline.tar.gz`. |
| `./clinicscribe check` | Check Lightning answers and tool calling works. |
| `./clinicscribe agent FILE` | Run the agent on one transcript (`python -m agent`). |
| `./clinicscribe test` | Run the unit tests. |

**Fully offline install.** Run `./clinicscribe vendor` on the ATOM while it is online (or on a Mac
with `VENDOR_PLATFORM=manylinux_2_17_aarch64 VENDOR_PYTHON=3.12 ./clinicscribe vendor` to fetch the
ARM64 wheels), or copy the bundle over and `tar xzf clinicscribe-offline.tar.gz && cd clinicscribe &&
./clinicscribe`. If the OS Python has no `python3-venv`, the installer bootstraps pip from the
vendored pip wheel instead.

`./clinicscribe` runs `scripts/start_demo.sh` with the venv's Python. `start_demo.sh` checks Lightning on `127.0.0.1:9000`, then serves the `/jobs` API and the UI from
one process (`python3 -m server`). The badge in the top right turns **LIVE**. Open the page in the
ATOM's own browser, or from a laptop on the same network at `http://<atom-ip>:8080`. Nothing in the
page or the backend touches the internet, so Wi-Fi can be off.

**What you can upload**

| Upload | What happens |
|---|---|
| First-layer JSON (`wav`, `total_duration`, `segments[{speaker, start, end, text}]`) | Goes straight to the Lightning agent. Try `samples/diarized_urti.json`. |
| Audio (`.wav` etc.) with `STT_URL` or `STT_CMD` set | The first layer (diarization + ASR) runs on it, then the agent. |
| Audio with neither set | Uses a cached first-layer JSON with the same name from `samples/` (`<name>.json` or `diarized_<name>.json`). |

**The team's async_stt pipeline as the first layer.** `stt_gateway/api.py` is a small FastAPI
front door for `~/Documents/async_stt` (diarization + ASR workers on Ray Serve / Celery in the
`env_edwin` container). It submits a wav to their `run_pipeline` task and reads progress from their
job files, so async_stt itself is unchanged. With it, each turn chip in the UI lights up as async_stt
transcribes that turn.

```bash
./scripts/start_stt_gateway.sh   # copies the gateway into async_stt, starts it in env_edwin on 127.0.0.1:8100
./scripts/test_stt_gateway.sh    # POSTs a wav (first 60 s of a sample), polls GET until the transcript is back
./clinicscribe                   # finds the gateway on :8100 and uses it for audio uploads
```

Gateway routes: `POST /jobs` (multipart `file`), `GET /jobs/{id}` (status, per-turn progress, transcript),
`POST /transcribe` (waits and returns the transcript), `GET /health`.

Other ways to hook up a first layer:

```bash
STT_URL=http://127.0.0.1:PORT/transcribe ./scripts/start_demo.sh                # POST multipart "file", JSON back
STT_CMD='python3 ~/Documents/async_stt/run.py {audio}' ./scripts/start_demo.sh   # JSON on stdout
STT_CMD='... {audio} --out {out}' ./scripts/start_demo.sh                        # or JSON written to {out}
```

Don't quote `{audio}` or `{out}`; the backend quotes them. Patient details (panel, allergies) come
from the JSON's `patient`, or from [samples/patients.json](samples/patients.json) by file name.

**Demo safety net.** Every finished job is saved to `out/<name>.result.json`. If Lightning fails
during the demo, a job whose file was run before serves that saved result instead
(`meta.cached: true`). So run each demo file once before going on stage.

**Demo steps (Wi-Fi off)**

1. Before: `demo kit-up`, wait until `curl -s 127.0.0.1:9000/v1/models` lists `lightning`.
2. `./clinicscribe`, open `http://localhost:8080`, check the badge says LIVE.
3. Drop the hero consult; walk through transcript, tool calls, the MC flag, Approve.
4. Drop the batch of short files at once to show the queue and the metrics bar.

`GET /health` shows whether Lightning answers and which first layer is configured.
The UI on its own (mock data, no backend) still runs with `python serve_ui.py`; see
[ui/README.md](ui/README.md) for the HTTP contract.

## The agent stage (transcript → visit record)

### Agent internals

```
transcript + patient ─▶ Lightning (vLLM, tool calling) ─┬─ get_panel_rules ─▶ data/panels.json
                                                        ├─ check_drug ──────▶ data/formulary.json + clinic terms + allergies
                                                        ├─ draft_mc ────────▶ dates, panel MC limit
                                                        ├─ flag
                                                        └─ write_soap_note
                         ─▶ verifier pass (2nd Lightning call: anything unsupported or missing?)
                         ─▶ JSON output (docs/agent_contract.md)
```

1. **Agent loop** (`agent/agent.py`). Lightning gets the transcript and five tools. It decides
   which to call: it looks up the patient's panel, checks each prescribed drug, drafts the MC
   if the doctor gave one, raises flags and finally writes the SOAP note.
2. **Tools** (`agent/tools.py`). Plain Python over local JSON. The facts come from here, not
   from the model's memory: allergy conflicts, max daily dose, drug interactions, MC end date
   and panel MC limits are all computed deterministically and turned into flags.
3. **Verifier**. A second, separate Lightning call compares the draft with the transcript and
   lists anything unsupported (hallucinated) or missing. Those become flags too.
4. **Clinic terms** (`agent/terms.py`, `data/clinic_terms.json`). Each clinic keeps a small list of
   its own brand names, shorthand and panel names, with what they mean and how the ASR tends to
   mishear them. Known mishearings are corrected in the transcript ("by solvent" → Bisolvon), the
   list goes into Lightning's prompt, and `check_drug` resolves a clinic term to its formulary drug.
   A drug name that matches neither the formulary nor the list is flagged for the doctor instead of
   guessed. Edit it at `/terms.html` (the "Clinic terms" button in the UI); changes apply to the next
   consult. The ASR model takes no vocabulary hints, so the correction runs right after ASR.
5. **Fallback**. If the server was started without tool calling, the agent asks for the SOAP
   note as plain JSON instead and flags that the checks were skipped.

The client (`agent/llm.py`) speaks the OpenAI-compatible API that vLLM exposes and uses only
the Python standard library, so there is nothing to `pip install` on the ATOM.

### Run it on the ATOM

Connect with **NVIDIA Sync** and open **Terminal** (or VS Code) on your ATOM. Then:

```bash
git clone https://github.com/VentusTWY/dgx-spark-clinicscribe.git
cd dgx-spark-clinicscribe

./scripts/check_server.sh                         # 1. is Lightning up? does tool calling work?
./scripts/run_agent.sh samples/transcript_urti.json   # 2. run the agent on the sample visit
./scripts/run_agent.sh samples/diarized_urti.json     # 3. same visit as diarization + ASR output
./scripts/run_agent.sh samples/diarized_hero_aia.json # 4. demo case: 3-day MC on AIA (limit 2) gets flagged
```

Measured on our ATOM (EMARQUE-DGX-H): the 100 s sample consult takes about 64 s through the agent
with Lightning thinking on (6.3k completion tokens). `LLM_THINKING=0` turns reasoning off for the
whole run, which is faster; compare the note quality before using it for the demo. The verifier and
speaker labelling always run with thinking off, because they only need a short JSON answer.

The result prints to the terminal and is saved to `out/<input name>.result.json`.

**Feeding it the diarization + ASR output.** Give the agent the first layer's JSON as is
(`wav`, `total_duration`, and per segment `speaker` 0-8, `start`, `end`, `text`; see
[samples/diarized_urti.json](samples/diarized_urti.json)). Lightning works out which speaker id is the
doctor and which is the patient from what they say, and the result carries the labelled
`transcript` with timestamps, plus `timings` for the metrics bar.

**Is Lightning up?** `check_server.sh` lists the served models, sends a test message and
tests tool calling. On the event ATOMs, the hackathon kit serves Lightning with vLLM on
`127.0.0.1:9000`, model id `lightning`. Port 8000 is **not** the model on our ATOM: another service
answers there with HTTP 500. If the model isn't up, start the kit's Lightning (takes ~5-10 min the
first time, ~37 GiB):

```bash
demo kit-status      # is lightning :9000 ticked?
demo kit-up          # start Lightning only (no argument)
curl -s http://127.0.0.1:9000/v1/models
curl -s http://127.0.0.1:9000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"lightning","max_tokens":64,"messages":[{"role":"user","content":"Say OK"}]}'
```

`demo kit-up switchyard` also starts Nano, Omni and the switchyard router on `:9010`; set
`LLM_BASE_URL=http://127.0.0.1:9010/v1` to go through it. Do not start your own vLLM, and do not
stop services other people are running (`demo stop-all`, `down`, `reset`).

**Different port or model name?** Everything is configurable:

```bash
export LLM_BASE_URL=http://127.0.0.1:9000/v1   # default
export LLM_MODEL=lightning                     # default: "lightning" if listed, else the first model
export LLM_THINKING=0                          # optional: turn off reasoning for speed
export LLM_MAX_TOKENS=8192                     # optional: raise if replies come back empty (default 4096)
./scripts/run_agent.sh samples/transcript_urti.json
# or: python3 -m agent my_visit.json --base-url ... --model ... --no-verify
```

**Running from your laptop instead of the ATOM.** The model port only listens on the ATOM's
127.0.0.1, so forward it over SSH (same login NVIDIA Sync uses), then run the agent on your laptop:

```bash
ssh -N -L 9000:127.0.0.1:9000 admin@<your-atom-hostname-or-ip>
# in another terminal on the laptop:
./scripts/run_agent.sh samples/transcript_urti.json
```

Running on the ATOM itself is simpler and is what the judges want to see.

### Use it from Python (e.g. the web UI)

```python
from agent import run_agent, run_agent_on_segments
result = run_agent(transcript_text, patient={"name": "...", "allergies": ["penicillin"], "panel_id": "PANEL-AIA"})
result = run_agent_on_segments(first_layer_json, patient={...})   # diarized segments in, roles mapped
```

The output shape is documented in [docs/agent_contract.md](docs/agent_contract.md); a full
example is [samples/sample_output.json](samples/sample_output.json).

### Test without a GPU

```bash
python3 -m unittest discover tests
```

This runs the whole pipeline against a fake OpenAI-compatible server that replays the tool
calls Lightning would make.

### Files

| Path | What |
|---|---|
| `agent/llm.py` | OpenAI-compatible client for vLLM (stdlib only) |
| `agent/tools.py` | Tool schemas and implementations |
| `agent/agent.py` | Agent loop, verifier, output assembly |
| `agent/segments.py` | Diarized input: loader, Doctor/Patient role mapping, timings |
| `agent/terms.py` | Clinic terms: mishearing correction, glossary for the prompt, drug-name lookup |
| `data/formulary.json`, `data/panels.json` | Synthetic demo formulary and panel rules |
| `data/clinic_terms.json` | The clinic's own terms (synthetic demo list; edited at `/terms.html`) |
| `samples/` | Sample transcript, visit input and output |
| `scripts/check_server.sh`, `scripts/run_agent.sh` | One-command server check and run |

All data here is synthetic and for demonstration only; it is not clinical guidance.

## Appendix: Python modules from the original host

The Python files in [`host/async_stt/`](host/async_stt/) were copied from the
original host's `async_stt` implementation. They document how the host pipeline is
split into workers; they are not replacement modules for the ClinicScribe agent.

| Repository file | Role in the host pipeline |
|---|---|
| [`host/async_stt/pipeline.py`](host/async_stt/pipeline.py) | Orchestrates the pipeline and dispatches work to worker queues. |
| [`host/async_stt/vad.py`](host/async_stt/vad.py) | Diarization: identifies speaker segments in the audio. |
| [`host/async_stt/asr.py`](host/async_stt/asr.py) | Transcribes audio segments as separate worker tasks. |
| [`host/async_stt/llm.py`](host/async_stt/llm.py) | Host summarizer worker using vLLM directly; distinct from `agent/llm.py`. |
| [`test_asr.py`](test_asr.py) | Submits an audio file to the host pipeline and polls its job files. |

The host setup uses `~/Documents/async_stt` and the `env_edwin` container. The
gateway startup script expects that project's files at `/workspace` inside the
container; ClinicScribe ran separately at `/workspace/clinicscribe` in the
reported deployment. It no longer needs a container: `./clinicscribe` runs it on the
ATOM host from its own venv. `host/async_stt/` is the repository's source snapshot folder,
not a required installation path or a verified copy of every host subdirectory.

The original worker deployment also needs `common/job_store.py`, shared job/audio
storage, Redis/Celery, Ray Serve and model dependencies. Those components and the
complete deployment configuration are not all included here. When running
`test_asr.py` from this checkout, set `JOBS_DIR` to the worker's actual shared job
directory; its default is `data/jobs` relative to the script. Audio paths must also
be accessible to the workers. See the [host source notes](host/async_stt/README.md).
