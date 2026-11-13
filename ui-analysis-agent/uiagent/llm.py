"""Azure OpenAI chat client: API-key auth, strict JSON-schema output, optional image input, bounded retries."""
from __future__ import annotations

import base64
import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("uiagent.llm")


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, endpoint: str, model: str, api_key: str, *, max_calls: int = 2000,
                 timeout: int = 300, calls_log: Path | None = None) -> None:
        base = endpoint.rstrip("/")
        if not base.endswith("/openai/v1"):
            base += "/openai/v1"
        self.url = base + "/chat/completions"
        self.model = model
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls_log = calls_log
        self._lock = threading.Lock()
        self._api_key = api_key

    def structured(self, agent: str, system: str, user: str, schema_name: str, schema: dict, *,
                   images: list[Path] | None = None, max_tokens: int = 4000) -> dict:
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"LLM call budget of {self.max_calls} reached")
            self.calls += 1
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()}}
                for p in images if p.exists()]
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
            "max_completion_tokens": max_tokens,
        }).encode("utf-8")
        last_error = ""
        for attempt in range(6):
            started = time.monotonic()
            req = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "api-key": self._api_key})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - operator-configured https endpoint
                    payload = json.loads(resp.read().decode("utf-8"))
                usage = payload.get("usage") or {}
                self.prompt_tokens += usage.get("prompt_tokens") or 0
                self.completion_tokens += usage.get("completion_tokens") or 0
                self._record(agent, 200, time.monotonic() - started, usage)
                choice = payload["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise RuntimeError("model output truncated")
                return json.loads(choice["message"].get("content") or "{}")
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("retry-after") if exc.headers else None
                try:
                    last_error = json.loads(exc.read().decode("utf-8")).get("error", {}).get("code", "")
                except (ValueError, AttributeError):
                    last_error = ""
                self._record(agent, exc.code, time.monotonic() - started, {}, last_error)
                if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(min(60.0, float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 3 * 2 ** attempt))
                    continue
                raise RuntimeError(f"model call failed: HTTP {exc.code} {last_error}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last_error = type(exc).__name__
                self._record(agent, "network", time.monotonic() - started, {}, last_error)
                if attempt < 5:
                    time.sleep(2 * 2 ** attempt)
                    continue
        raise RuntimeError(f"model call failed after retries: {last_error}")

    def _record(self, agent: str, status: Any, seconds: float, usage: dict, error: str = "") -> None:
        if not self.calls_log:
            return
        with self._lock, self.calls_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": agent, "status": status, "seconds": round(seconds, 2),
                                 "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                                 "error": error}) + "\n")
