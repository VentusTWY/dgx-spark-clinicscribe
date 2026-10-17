#!/usr/bin/env bash
# End-to-end check of the async_stt gateway: POST a wav to /jobs, poll GET /jobs/{id} until done.
# Usage: ./scripts/test_stt_gateway.sh [audio.wav] [seconds]
#   audio defaults to the first file in ~/Documents/async_stt/long_audio_samples
#   seconds (default 60) trims the clip first when ffmpeg is installed, so the test is quick
set -euo pipefail
BASE="${STT_API:-http://127.0.0.1:8100}"
AUDIO="${1:-$(ls ~/Documents/async_stt/long_audio_samples/*.wav 2>/dev/null | head -1)}"
SECS="${2:-60}"
[ -f "$AUDIO" ] || { echo "No audio file: give a path, e.g. ./scripts/test_stt_gateway.sh consult.wav"; exit 1; }

curl -sf --max-time 3 "$BASE/health" >/dev/null || { echo "Gateway not answering at $BASE. Run ./scripts/start_stt_gateway.sh"; exit 1; }

if command -v ffmpeg >/dev/null && [ "$SECS" != "0" ]; then
  CLIP="$(mktemp --suffix=.wav)"
  ffmpeg -loglevel error -y -i "$AUDIO" -t "$SECS" -ac 1 -ar 16000 "$CLIP"
  echo "Audio: $AUDIO (first ${SECS}s)"
else
  CLIP="$AUDIO"
  echo "Audio: $AUDIO (whole file; install ffmpeg to trim)"
fi

echo "POST $BASE/jobs"
JOB_ID=$(curl -sf -F "file=@$CLIP" "$BASE/jobs" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
echo "job_id: $JOB_ID"
START=$(date +%s)
while true; do
  RESP=$(curl -sf "$BASE/jobs/$JOB_ID")
  STATUS=$(printf '%s' "$RESP" | python3 -c 'import json,sys; j=json.load(sys.stdin); print("%s %s/%s turns" % (j["status"], j["turns_done"], j["turns_total"]))')
  echo "[$(( $(date +%s) - START ))s] GET /jobs/$JOB_ID -> $STATUS"
  case "$STATUS" in
    done*|failed*) break ;;
  esac
  sleep 2
done
printf '%s' "$RESP" | python3 -c '
import json, sys
j = json.load(sys.stdin)
if j["status"] == "failed":
    sys.exit("\nFailed: %s" % j["error"])
r = j["result"]
print("\nDone: %ss audio, %d segments, timings %s" % (r["total_duration"], len(r["segments"]), r["timings"]))
for s in r["segments"][:8]:
    print("  [%7.2f-%7.2f] speaker %s: %s" % (s["start"], s["end"], s["speaker"], s["text"][:90]))'
