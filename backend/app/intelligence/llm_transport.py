"""A real language model for the agents' explanations: any OpenAI-compatible chat-completions endpoint.

Works with Groq, OpenAI, OpenRouter and a local Ollama (``http://127.0.0.1:11434/v1``) alike. Configuration
comes from the environment:

  INTELLIGENCE_REASONING_MODE=live      turns it on (anything else keeps the rule-based text)
  INTELLIGENCE_LIVE_MODEL=<model id>    e.g. llama-3.3-70b-versatile (Groq) or qwen2.5:7b (Ollama)
  LLM_BASE_URL=<url>                    no default; Groq: https://api.groq.com/openai/v1
  LLM_API_KEY=<key>                     required for a hosted endpoint; never logged, never put in a URL

The model may only reword explanation text (see ``enrich_rationales`` / ``enrich_explanations``): rankings,
severities, scores and every number stay deterministic and the number check rejects invented figures. A
missing key, an unreachable endpoint, a timeout or a malformed answer all end in the rule-based text and a
stated limitation, never in an error response.

Explanations are cached per input (``ExplanationCache``) so a dashboard refresh never calls the model again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

MAX_ITEMS_PER_CALL = 10   # at most this many findings/proposals go to the model in one request
BACKOFF_S = 30.0          # after a 429, a 5xx, a timeout or an unreachable host, send nothing for this long
MAX_BACKOFF_S = 120.0     # ... or for Retry-After, whichever is larger, but never longer than this
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)
CACHE_LIMIT = 500


_clock = time.monotonic
_backoff_until: dict[str, float] = {}
_backoff_lock = threading.Lock()


def reset_backoff() -> None:
    """Forget every backoff (tests, or after the user fixes the endpoint)."""
    with _backoff_lock:
        _backoff_until.clear()


def _start_backoff(host: str, seconds: float) -> None:
    with _backoff_lock:
        _backoff_until[host] = _clock() + min(max(seconds, BACKOFF_S), MAX_BACKOFF_S)


class LLMError(RuntimeError):
    """A failed model call. The message never contains the key, a header or a response body."""


def endpoint_location(base_url: str) -> str:
    """``local`` for a loopback endpoint (Ollama on this machine), ``hosted`` for anything else."""
    return "local" if (urlparse(base_url).hostname or "") in _LOOPBACK else "hosted"


def _host(base_url: str) -> str:
    return urlparse(base_url).netloc or "the endpoint"


def make_transport(
    base_url: str,
    model: str,
    api_key: str = "",
    http_transport: httpx.BaseTransport | None = None,
) -> Callable[[str, str, int, float], str]:
    """Build a ``(system, user, max_tokens, timeout_s) -> text`` transport for ``chat/completions``."""

    url = base_url.rstrip("/") + "/chat/completions"
    host = _host(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    def transport(system: str, user: str, max_tokens: int, timeout_s: float) -> str:
        with _backoff_lock:
            wait = _backoff_until.get(host, 0.0) - _clock()
        if wait > 0:  # a recent 429/5xx/timeout: do not hammer the API; the rule-based text is used meanwhile
            raise LLMError(f"backing off for {int(wait) + 1}s after a rate limit or server error")
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(transport=http_transport, timeout=timeout_s, follow_redirects=False) as client:
                response = client.post(url, headers=headers, json=body)
                if response.status_code == 400:  # some models or hosts reject JSON mode; ask again without it
                    body.pop("response_format")
                    response = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            _start_backoff(host, BACKOFF_S)
            raise LLMError("the model request timed out") from None
        except httpx.HTTPError as e:
            _start_backoff(host, BACKOFF_S)
            raise LLMError(f"the model endpoint is unreachable ({type(e).__name__})") from None
        if response.status_code != 200:
            if response.status_code == 429 or response.status_code >= 500:
                try:
                    retry_after = float(response.headers.get("retry-after", ""))
                except ValueError:
                    retry_after = BACKOFF_S
                _start_backoff(host, retry_after)
            raise LLMError(f"the model endpoint answered HTTP {response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("the model endpoint returned an unexpected response shape") from None
        if not isinstance(content, str) or not content.strip():
            raise LLMError("the model returned no text")
        fenced = _FENCE.match(content)
        return fenced.group(1) if fenced else content

    return transport


def transport_from_env() -> tuple[Callable[[str, str, int, float], str] | None, str | None, list[str]]:
    """``(transport, location, notes)`` from the environment, or ``(None, None, [why not])``."""

    model = os.environ.get("INTELLIGENCE_LIVE_MODEL", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not base_url:
        return None, None, ["LLM_BASE_URL is not set (Groq: https://api.groq.com/openai/v1); rule-based explanations are shown."]
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, None, ["LLM_BASE_URL is not a valid http(s) URL; rule-based explanations are shown."]
    location = endpoint_location(base_url)
    if location == "hosted":
        if parsed.scheme != "https":
            return None, None, [f"The hosted model endpoint {_host(base_url)} must use https; rule-based explanations are shown."]
        if not key:
            return None, None, [f"LLM_API_KEY is not set for {_host(base_url)}; rule-based explanations are shown."]
    return make_transport(base_url, model, key), location, []


# ---------------------------------------------------------------------------
# Cache: one small file per explanation, keyed by a hash of the model and the finding's own data.


def cache_key(model: str, kind: str, item: Any) -> str:
    raw = json.dumps({"model": model, "kind": kind, "item": item}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExplanationCache:
    """``results/.llm_cache/<sha256>.json`` holding only the generated text and the model id."""

    def __init__(self, directory: Path, limit: int = CACHE_LIMIT):
        self.directory, self.limit = Path(directory), limit

    def get(self, key: str) -> str | None:
        try:
            data = json.loads((self.directory / f"{key}.json").read_text(encoding="utf-8"))
            text = data["text"]
            return text if isinstance(text, str) and text else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, text: str, model: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"text": text, "model": model, "created": time.time()}, fh)
            os.replace(tmp, self.directory / f"{key}.json")
            self._trim()
        except OSError:
            pass  # a cache that cannot be written only costs a repeat call

    def _trim(self) -> None:
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - self.limit)]:
            old.unlink(missing_ok=True)


def fallback_limitation(provider_mode: str, total: int, from_model: int) -> str | None:
    """A stated limitation when a live provider was configured but some items kept the rule-based text."""
    if provider_mode != "live" or total == 0 or from_model >= total:
        return None
    missing = total - from_model
    return (f"The language model gave no usable text for {missing} of {total} item(s) (unreachable, rate-limited, too slow, "
            f"an unexpected answer, a number that was not in the evidence, or beyond the cap of {MAX_ITEMS_PER_CALL} per request); "
            "rule-based text is shown for them.")
