# ClinicScribe on DGX Spark

A GP consultation goes in as a transcript. Out comes a SOAP note, checked prescriptions, a
drafted MC and the panel billing rules, all produced by NVIDIA Nemotron 3.5 Lightning running
locally on the team's GIGABYTE AI TOP ATOM. No patient data leaves the clinic.

## Demo UI

```bash
pip install fastapi uvicorn
python serve_ui.py        # http://localhost:8080
```

A single dark page for the projector: upload consults (or load the demo batch), watch each
job's speaker turns light up through diarization → ASR → Lightning, then review the transcript,
SOAP note, tool-call feed, flags, prescriptions and MC, and approve. It runs on built-in mock
data until the backend answers `GET /jobs`, then switches to live. No CDN assets, so it works
with Wi-Fi off. Details and the HTTP contract: [ui/README.md](ui/README.md).

## The agent stage (transcript → visit record)

### How it works

```
transcript + patient ─▶ Lightning (vLLM, tool calling) ─┬─ get_panel_rules ─▶ data/panels.json
                                                        ├─ check_drug ──────▶ data/formulary.json + allergies
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
4. **Fallback**. If the server was started without tool calling, the agent asks for the SOAP
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
```

The result prints to the terminal and is saved to `out/<input name>.result.json`.

**Feeding it the diarization + ASR output.** Give the agent the first layer's JSON as is
(`wav`, `total_duration`, and per segment `speaker` 0-8, `start`, `end`, `text`; see
[samples/diarized_urti.json](samples/diarized_urti.json)). Lightning works out which speaker id is the
doctor and which is the patient from what they say, and the result carries the labelled
`transcript` with timestamps, plus `timings` for the metrics bar.

**Is Lightning up?** `check_server.sh` lists the served models, sends a test message and
tests tool calling. On the event ATOMs Lightning is served through Emarque's *switchyard* router
on `127.0.0.1:9010` (and `:9011`), model id `lightning` (also `nano`, and `auto` which routes
between them). Port 8000 is **not** the model on our ATOM: another service answers there with
HTTP 500. To check by hand:

```bash
curl -s http://127.0.0.1:9010/v1/models
curl -s http://127.0.0.1:9010/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"lightning","max_tokens":64,"messages":[{"role":"user","content":"Say OK"}]}'
ss -ltnp | grep -E '9010|9011'               # if refused: is switchyard running?
```

The organisers run the model service. Per the event guide, do not start a second vLLM process
or stop services that other people are running. If it is down, ask the EMARQUE mentors.

**Different port or model name?** Everything is configurable:

```bash
export LLM_BASE_URL=http://127.0.0.1:9010/v1   # default
export LLM_MODEL=lightning                     # default: "lightning" if listed, else the first model
export LLM_THINKING=0                          # optional: turn off reasoning for speed
export LLM_MAX_TOKENS=8192                     # optional: raise if replies come back empty (default 4096)
./scripts/run_agent.sh samples/transcript_urti.json
# or: python3 -m agent my_visit.json --base-url ... --model ... --no-verify
```

**Running from your laptop instead of the ATOM.** The model port only listens on the ATOM's
127.0.0.1, so forward it over SSH (same login NVIDIA Sync uses), then run the agent on your laptop:

```bash
ssh -N -L 9010:127.0.0.1:9010 admin@<your-atom-hostname-or-ip>
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
| `data/formulary.json`, `data/panels.json` | Synthetic demo formulary and panel rules |
| `samples/` | Sample transcript, visit input and output |
| `scripts/check_server.sh`, `scripts/run_agent.sh` | One-command server check and run |

All data here is synthetic and for demonstration only; it is not clinical guidance.
