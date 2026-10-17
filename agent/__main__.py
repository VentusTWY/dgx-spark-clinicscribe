"""CLI: python3 -m agent <transcript.txt | visit.json | diarized.json> [--out result.json]

A visit .json looks like samples/transcript_urti.json:
  {"transcript": "..." or "transcript_file": "x.txt", "patient": {...}, "visit_date": "YYYY-MM-DD"}
A diarized .json (first-layer output) looks like samples/diarized_urti.json:
  {"wav": "...", "total_duration": 95.0, "segments": [{"speaker": 0, "start": 1.0, "end": 10.0, "text": "..."}],
   "patient": {...}, "visit_date": "..."}
"""

import argparse
import json
import sys
from pathlib import Path

from .agent import run_agent
from .llm import LLMClient, LLMError
from .segments import is_segment_input, run_agent_on_segments


def load_input(path):
    path = Path(path)
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        transcript = data.get("transcript") or (path.parent / data["transcript_file"]).read_text()
        return transcript, data.get("patient") or {}, data.get("visit_date")
    return path.read_text(), {}, None


def main():
    ap = argparse.ArgumentParser(prog="python3 -m agent", description="Summarise a consultation transcript with Lightning")
    ap.add_argument("input", help="Transcript .txt, visit .json, or diarized .json (first-layer output)")
    ap.add_argument("--base-url", help="OpenAI-compatible base URL (default $LLM_BASE_URL or http://localhost:8000/v1)")
    ap.add_argument("--model", help="Model id (default $LLM_MODEL, else the first model the server lists)")
    ap.add_argument("--no-verify", action="store_true", help="Skip the verifier pass")
    ap.add_argument("--out", help="Also write the JSON result to this file")
    args = ap.parse_args()

    client = LLMClient(base_url=args.base_url, model=args.model)
    data = json.loads(Path(args.input).read_text()) if args.input.endswith(".json") else None
    try:
        if data is not None and is_segment_input(data):
            patient = data.get("patient") if isinstance(data, dict) else None
            visit_date = data.get("visit_date") if isinstance(data, dict) else None
            result = run_agent_on_segments(data, patient, visit_date, client=client, verify=not args.no_verify)
        else:
            transcript, patient, visit_date = load_input(args.input)
            result = run_agent(transcript, patient, visit_date, client=client, verify=not args.no_verify)
    except (LLMError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"\nSaved to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
