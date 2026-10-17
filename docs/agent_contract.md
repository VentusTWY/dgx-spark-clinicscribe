# Agent JSON contract

The agent (`agent.run_agent(...)` or `python3 -m agent`) takes a transcript plus patient
details and returns one JSON object. The UI renders this object. A full example is
[`samples/sample_output.json`](../samples/sample_output.json).

## Input

```json
{
  "transcript": "Doctor: ... Patient: ...",
  "patient": {"name": "Ahmad bin Ismail", "age": 34, "sex": "M", "ic": "optional",
              "allergies": ["penicillin"], "current_meds": [], "panel_id": "PANEL-AIA"},
  "visit_date": "2026-10-17"
}
```

In a file you can use `"transcript_file": "x.txt"` (relative to the JSON file) instead of `"transcript"`.
Only `transcript` is required.

### Diarized input (first-layer output)

`agent.run_agent_on_segments(data, patient, visit_date)`, or `python3 -m agent diarized.json`, takes the
diarization + ASR output directly ([`samples/diarized_urti.json`](../samples/diarized_urti.json)):

```json
{
  "wav": "consult_urti.wav",
  "total_duration": 100.1,
  "segments": [{"speaker": 1, "start": 1.0, "end": 4.5, "text": "Good morning, Encik Ahmad..."}],
  "patient": {"...": "optional, as above"},
  "visit_date": "optional",
  "timings": {"diarization_s": 3.1, "asr_s": 4.6}
}
```

Accepted spellings: the list may be `segments`, `breakdown`, `turns`, `utterances` or `results` (or a bare list);
speaker `speaker` / `speaker_id` / `spk` (`0`-`8` or `"SPEAKER_3"`); times `start`/`end`, `start_time`/`end_time`,
or `speaker_duration` / `duration` / `time` as `[1, 10]`, `"(1, 10)"` or `{"start", "end"}`; file `wav` / `file` /
`audio` / `filename` / `audio_file`; length `total_duration` / `duration` / `audio_s`. `timings` is optional and
passed through.

Lightning maps each speaker id to a role (Doctor, Patient, Nurse, Caregiver, Other) from what they say. If its
answer is unusable (not exactly one Doctor), a keyword heuristic decides instead. The agent then sees the
transcript as `[00:01-00:04] Doctor: ...` lines.

## Output

| Key | Type | Meaning |
|---|---|---|
| `soap` | object | `subjective`, `objective`, `assessment`, `plan` (strings), `diagnoses` (string[]) |
| `prescriptions` | array | One line per drug: `drug`, `dose`, `frequency`, `duration`, `in_formulary` (bool), `status` (`ok` / `warning` / `danger`), `notes` (string[]) |
| `mc` | object or null | `days`, `start_date`, `end_date` (YYYY-MM-DD, inclusive), `reason`, `text`. Null if no MC was given |
| `panel` | object or null | `id`, `name`, `max_mc_days` (int or null), `formulary_only`, `notes` (string[]). Null for no panel |
| `flags` | array | `severity` (`critical` / `warning` / `info`), `message`, `source` (`check_drug`, `draft_mc`, `get_panel_rules`, `model`, `verifier`, `agent`). Sorted most severe first |
| `verification` | object or null | `passed` (bool), `unsupported` (string[]), `missing` (string[]); may also carry `error` and `raw` if the verifier output could not be parsed. Null when run with `--no-verify` |
| `meta` | object | `model`, `base_url`, `visit_date`, `latency_ms`, `tool_calls` (array of `{name, arguments, ok}`, in call order) |

### Extra keys for diarized input

`run_agent_on_segments` returns everything above plus:

| Key | Type | Meaning |
|---|---|---|
| `transcript` | array | `[{speaker, speaker_id, start, end, text}]`, in time order. `speaker` is the role, `speaker_id` the diarization id (string) |
| `speakers` | object | `roles` (`{"1": "Doctor", "0": "Patient"}`), `method` (`model` or `heuristic`) |
| `audio` | object | `file`, `duration_s` |
| `timings` | object | `agent_s` (role labelling + agent + verifier), `audio_s`, `llm_tokens` (completion tokens), plus any upstream `diarization_s` / `asr_s`; `total_s` is their sum when upstream timings are given |
