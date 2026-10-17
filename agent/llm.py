"""Minimal client for an OpenAI-compatible server (vLLM on the ATOM).

Standard library only, so it runs on the ATOM without installing anything.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://localhost:8000/v1"


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url=None, model=None, api_key=None, timeout=180):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or "EMPTY"
        self.timeout = timeout
        self._model = model or os.environ.get("LLM_MODEL")
        # Reasoning tokens count against max_tokens; raise it if content comes back empty.
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
        self.completion_tokens = 0
        # A local server must not go through an http_proxy set in the environment.
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise LLMError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Cannot reach {self.base_url} ({e.reason}). Is vLLM running?") from e

    def list_models(self):
        return [m["id"] for m in self._request("GET", "/models").get("data", [])]

    @property
    def model(self):
        # If no model name is configured, use whatever the server is serving.
        if not self._model:
            models = self.list_models()
            if not models:
                raise LLMError("Server reports no models at /models")
            self._model = models[0]
        return self._model

    def chat(self, messages, tools=None, temperature=0.2, max_tokens=None, json_mode=False):
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        # Nemotron reasoning models think by default; set LLM_THINKING=0 to switch it off.
        if os.environ.get("LLM_THINKING") == "0":
            body["chat_template_kwargs"] = {"enable_thinking": False}
        resp = self._request("POST", "/chat/completions", body)
        self.completion_tokens += (resp.get("usage") or {}).get("completion_tokens", 0)
        try:
            return resp["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Unexpected response: {json.dumps(resp)[:500]}") from e


def strip_thinking(text):
    """Remove <think>...</think> blocks that reasoning models may emit."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json_loose(text):
    """Parse JSON from model text, tolerating code fences and surrounding prose."""
    text = strip_thinking(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise
