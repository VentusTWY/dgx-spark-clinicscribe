# ClinicScribe — ACE 2026 Mini Hackathon (3 hours, 5 people)

## Goal
Turn recorded GP consult audio into a checked SOAP note, MC and panel-claim check,
running **entirely on the Gigabyte AI TOP ATOM** (DGX Spark class, GB10, ARM64/aarch64, unified memory).
No cloud APIs anywhere. The demo runs with Wi-Fi OFF.

Judging: AI capability 25% · Local AI on the ATOM 25% · Impact 20% · Technical 15% · Pitch + GitHub docs 15%.
"No slop": no basic chatbot, no cloud LLM wrapper, nothing that would run the same without the ATOM.

## Models (all local)
- **Diarization**: NVIDIA Sortformer via NeMo (e.g. `nvidia/diar_sortformer_4spk-v1`, verify name and that it installs on aarch64).
  Fallback if it won't run: skip diarization, send the whole file to ASR, and have Lightning label Doctor/Patient from the text.
- **ASR**: `nvidia/nemotron-3.5-asr-streaming-0.6b` (English; built-in punctuation and capitalisation).
  Try the Hugging Face Transformers path first (needs transformers >= 5.13), NeMo as plan B. Need segment or word timestamps.
- **LLM agent**: Nemotron 3.5 Lightning (30B MoE, 3B active), served with vLLM (>= 0.27.1), OpenAI-compatible API, tool calling on.
  Reasoning comes back separately; if `content` is empty, raise `max_tokens`.

## Architecture: async, decoupled stages (single FastAPI process, asyncio queues)
```
POST /jobs (audio upload) -> returns job_id immediately
  -> diar_queue  -> Diarization worker: segments [{speaker, start, end}]
                    cut audio per turn (pydub/ffmpeg); merge turns < 1 s into the next same-speaker turn; pad 0.2 s
  -> asr_queue   -> ASR workers x N: one job per turn {job_id, turn_idx, speaker, clip}; batch turns per model call
                    job complete when done_turns == total_turns
  -> agent_queue -> Agent workers (4-8 concurrent, vLLM batches them): ordered "Doctor: / Patient:" transcript in, JSON out
  -> events      -> UI (WebSocket or 1 s polling)
```
- Job status per job: `queued -> diarizing -> transcribing (k/n turns) -> summarising -> done | failed`
- Retries: a failed turn or stage re-enqueues only itself, max 2 tries.
- State: in-memory dict or SQLite. No Celery or Redis (pitch: "same design moves to Redis/NATS in production").
- Record timings per stage per job for the metrics panel.

## Agent tools (Lightning function calling)
- `write_soap_note(subjective, objective, assessment, plan)`
- `check_drug(name, dose)`: lookup in local `data/formulary.json` (20-30 common GP drugs, max doses, interactions)
- `draft_mc(days, reason)`
- `get_panel_rules(panel)`: from `data/panels.json` (3 made-up panels, e.g. "Panel A": MC limit 2 days, drug caps, referral rules)
- `flag(issue, fix)`: raised when a rule is broken
- Second "verifier" pass with Lightning confirms or drops each flag.
- Use structured/JSON output. Doctor/Patient labelling: map SPEAKER_0/1 from context if needed.

### Output JSON contract (UI builds against this with mock data first)
```json
{
  "job_id": "...",
  "transcript": [{"speaker": "Doctor", "start": 0.0, "end": 3.2, "text": "..."}],
  "soap": {"S": "...", "O": "...", "A": "...", "P": "..."},
  "mc": {"days": 3, "reason": "..."},
  "drug_alerts": [{"drug": "...", "issue": "..."}],
  "flags": [{"issue": "MC exceeds Panel A limit (2 days)", "fix": "Reduce to 2 days or add a reason", "verified": true}],
  "tool_calls": [{"name": "get_panel_rules", "args": {}, "result": {}}],
  "timings": {"diarization_s": 0, "asr_s": 0, "agent_s": 0, "total_s": 0, "audio_s": 0}
}
```

## UI (single page, dark, big fonts for a projector)
- Upload one or many files (batch of 10 for the demo).
- Job list: per job a row of turn chips that light up as each turn is transcribed; stage status.
- Detail view: speaker-labelled transcript | streaming SOAP note | tool-call feed | flags (amber, with text "FLAG") | Approve button.
- Metrics bar: audio minutes processed, wall time, real-time factor, Lightning tokens/s, "API cost: RM0".

## Demo data
- Hero file: 2-3 min English consult recorded by the team, with a planted error (3-day MC on Panel A, limit 2).
- Long file: 20-30 min (e.g. concatenated PriMock57 mock consults; check licence).
- 10 short files for the parallel batch.
- Pre-run everything once and cache the JSON as a fallback.

## Team split
1. Infra: vLLM + Lightning, ASR service, FastAPI skeleton, queues, metrics
2. Agent: prompts, tool schemas, verifier, JSON contract
3. Diarization service + demo audio + data/formulary.json + data/panels.json
4. Frontend against mock JSON, then real API
5. Pitch, README (GitHub docs are judged), demo script

## Timeline
0:00-0:15 agree contracts · 0:15-1:30 parallel build on mocks · 1:30 integration checkpoint (cut features if not working by 1:45)
· 2:00 feature freeze · 2:15-2:45 two full rehearsals with Wi-Fi off · 2:45-3:00 push to GitHub.

## Rules for Claude Code in this repo
- Never call any cloud API or download at runtime; everything must work offline once models are cached.
- Prefer the simplest thing that runs in the time limit. Measure on the ATOM; never quote spec-sheet numbers as results.
