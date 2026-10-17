#!/usr/bin/env bash
# Run the ClinicScribe agent on a transcript against Lightning on this ATOM.
# Usage: ./scripts/run_agent.sh [samples/transcript_urti.json] [extra args]
set -euo pipefail
cd "$(dirname "$0")/.."
INPUT="${1:-samples/transcript_urti.json}"
shift || true
mkdir -p out
python3 -m agent "$INPUT" --out "out/$(basename "${INPUT%.*}").result.json" "$@"
