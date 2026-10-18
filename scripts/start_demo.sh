#!/usr/bin/env bash
# One command for the demo: checks Lightning, then serves the API + UI on :8080.
# Usage: ./clinicscribe   (or ./scripts/start_demo.sh)   Lightning must already be up: `demo kit-up`
# Optional env: HOST (default 127.0.0.1; 0.0.0.0 opens it to the network, no login), LLM_BASE_URL, LLM_MODEL, STT_API / STT_URL / STT_CMD (first layer), PORT, AGENT_WORKERS,
#               PYTHON (default: .venv/bin/python from ./clinicscribe install, else python3)
set -euo pipefail
cd "$(dirname "$0")/.."
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:9000/v1}"
export PORT="${PORT:-8080}"
export HOST="${HOST:-127.0.0.1}"
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"

if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi
"$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null || { echo "Missing fastapi/uvicorn for $PYTHON. Run ./clinicscribe install (offline if vendor/wheels exists)."; exit 1; }

if curl -sf --max-time 5 "$LLM_BASE_URL/models" >/dev/null; then
  echo "Lightning: up at $LLM_BASE_URL"
else
  echo "Lightning: NOT reachable at $LLM_BASE_URL. Run 'demo kit-up' and wait for it to load."
  echo "  (The UI still runs; jobs fall back to cached results in out/ if a pre-run exists.)"
fi
if [ -z "${STT_API:-}${STT_URL:-}${STT_CMD:-}" ] && curl -sf --max-time 2 http://127.0.0.1:8100/health >/dev/null; then
  export STT_API=http://127.0.0.1:8100
fi
if [ -n "${STT_API:-}" ]; then echo "First layer: async_stt gateway at $STT_API"
elif [ -n "${STT_URL:-}" ]; then echo "First layer: $STT_URL"
elif [ -n "${STT_CMD:-}" ]; then echo "First layer: $STT_CMD"
else echo "First layer: not set (run ./scripts/start_stt_gateway.sh, or set STT_URL / STT_CMD). Upload first-layer .json files, or audio with a cached <name>.json in samples/."
fi

if [ "$HOST" = "0.0.0.0" ]; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "ClinicScribe: http://localhost:$PORT  ${IP:+(from another machine: http://$IP:$PORT)}"
  echo "  Open to the network with no login: anyone who can reach this port can read and upload consults."
else
  echo "ClinicScribe: http://localhost:$PORT  (this machine only; HOST=0.0.0.0 ./clinicscribe to open it to the network)"
fi
exec "$PYTHON" -m server
