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
