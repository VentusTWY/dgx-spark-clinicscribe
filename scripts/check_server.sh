#!/usr/bin/env bash
# Check the vLLM server on the ATOM is up and can answer.
# Usage: ./scripts/check_server.sh   (override with LLM_BASE_URL / LLM_MODEL)
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}" no_proxy="localhost,127.0.0.1,${no_proxy:-}"
BASE="${LLM_BASE_URL:-http://127.0.0.1:9000/v1}"

echo "== Models at $BASE/models"
if ! MODELS=$(curl -sf --max-time 10 "$BASE/models"); then
  echo "Cannot reach $BASE. Is vLLM running? Try: demo kit-status ; ss -ltnp | grep -E '9000|9010'" >&2
  exit 1
fi
echo "$MODELS" | python3 -m json.tool
MODEL="${LLM_MODEL:-$(echo "$MODELS" | python3 -c 'import json,sys; ids=[m["id"] for m in json.load(sys.stdin)["data"]]; print("lightning" if "lightning" in ids else ids[0])')}"

echo "== Chat test with $MODEL"
CHAT=$(curl -s --max-time 120 "$BASE/chat/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with: ready\"}],\"max_tokens\":256}")
if ! echo "$CHAT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])' 2>/dev/null; then
  echo "Chat request failed. Server said:" >&2
  echo "$CHAT" | head -c 600 >&2; echo >&2
  exit 1
fi

echo "== Tool-calling test"
OUT=$(curl -s --max-time 120 "$BASE/chat/completions" -H 'Content-Type: application/json' -d "{
  \"model\":\"$MODEL\",
  \"messages\":[{\"role\":\"user\",\"content\":\"What is the weather in Kuala Lumpur? Use the tool.\"}],
  \"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"parameters\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}}],
  \"tool_choice\":\"auto\",\"max_tokens\":512}")
if echo "$OUT" | grep -q '"tool_calls": *\[ *{'; then
  echo "Tool calling works."
else
  echo "No tool call returned. The agent will fall back to a plain SOAP note."
  echo "(vLLM needs --enable-auto-tool-choice --tool-call-parser <parser>; ask the organisers.)"
  echo "$OUT" | head -c 600; echo
fi
