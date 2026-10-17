#!/usr/bin/env bash
# Start the async_stt gateway (stt_gateway/api.py) inside the env_edwin container, next to the
# diarization + ASR workers, on 127.0.0.1:8100. start_demo.sh picks it up automatically.
# Usage: ./scripts/start_stt_gateway.sh   (ASYNC_STT_DIR, STT_CONTAINER, STT_GATEWAY_PORT to override)
set -euo pipefail
cd "$(dirname "$0")/.."
ASYNC_STT_DIR="${ASYNC_STT_DIR:-$HOME/Documents/async_stt}"
CONTAINER="${STT_CONTAINER:-env_edwin}"
PORT="${STT_GATEWAY_PORT:-8100}"

cp stt_gateway/api.py "$ASYNC_STT_DIR/clinicscribe_gateway.py"
docker exec -d "$CONTAINER" bash -lc \
  "cd /workspace && python3 -m uvicorn clinicscribe_gateway:app --host 127.0.0.1 --port $PORT > /tmp/clinicscribe_gateway.log 2>&1"

for _ in $(seq 1 20); do
  if curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null; then
    echo "async_stt gateway: up at http://127.0.0.1:$PORT"; exit 0
  fi
  sleep 1
done
echo "async_stt gateway did not answer. Log: docker exec $CONTAINER cat /tmp/clinicscribe_gateway.log"
exit 1
