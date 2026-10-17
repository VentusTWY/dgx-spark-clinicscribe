#!/usr/bin/env bash
# One command for the demo: checks Lightning, then serves the API + UI on :8080.
# Usage: ./scripts/start_demo.sh            (Lightning must already be up: `demo kit-up`)
# Optional env: LLM_BASE_URL, LLM_MODEL, STT_URL or STT_CMD (first layer), PORT, AGENT_WORKERS
set -euo pipefail
cd "$(dirname "$0")/.."
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:9000/v1}"
export PORT="${PORT:-8080}"
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"

python3 -c "import fastapi, uvicorn" 2>/dev/null || { echo "Missing packages: pip install fastapi uvicorn (do it while online)"; exit 1; }

if curl -sf --max-time 5 "$LLM_BASE_URL/models" >/dev/null; then
  echo "Lightning: up at $LLM_BASE_URL"
else
  echo "Lightning: NOT reachable at $LLM_BASE_URL. Run 'demo kit-up' and wait for it to load."
  echo "  (The UI still runs; jobs fall back to cached results in out/ if a pre-run exists.)"
fi
if [ -n "${STT_URL:-}" ]; then echo "First layer: $STT_URL"
elif [ -n "${STT_CMD:-}" ]; then echo "First layer: $STT_CMD"
else echo "First layer: not set (STT_URL / STT_CMD). Upload first-layer .json files, or audio with a cached <name>.json in samples/."
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "ClinicScribe: http://localhost:$PORT  ${IP:+(from another machine: http://$IP:$PORT)}"
exec python3 -m server
