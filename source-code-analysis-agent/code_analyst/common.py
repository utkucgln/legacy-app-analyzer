"""Shared primitives: fact model, writers, masking and source sanitisation."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("code_analyst")

FACT_TYPES = (
    "screen", "control", "rule", "entity", "relationship", "code_value", "integration",
    "job", "config", "role", "db_logic", "email", "usage", "dead_feature", "finding",
)
CONFIDENCE = ("Confirmed", "Probable", "To verify")
RULE_KINDS = ["validation", "calculation", "threshold", "state_transition", "authorization", "schedule", "integration", "special_case",
              "notification", "data_access", "other"]


@dataclass
class Fact:
    type: str
    statement: str
    keys: list[str]
    sources: list[dict]
    confidence: str = "Probable"
    conflicts: list[str] = field(default_factory=list)
    business_module: str = ""
    attrs: dict = field(default_factory=dict)
    fact_id: str = ""

    def __post_init__(self) -> None:
        if self.type not in FACT_TYPES:
            raise ValueError(f"unknown fact type {self.type}")
        if self.confidence not in CONFIDENCE:
            raise ValueError(f"unknown confidence {self.confidence}")
        if not self.fact_id:
            digest = hashlib.sha1("|".join([self.type, self.statement, *sorted(self.keys)]).encode("utf-8")).hexdigest()[:10]
            pillar = self.sources[0]["pillar"] if self.sources else "x"
            self.fact_id = f"{pillar[:2].upper()}-{self.type}-{digest}"


class FactSink:
    def __init__(self, pillar: str) -> None:
        self.pillar = pillar
        self.facts: dict[str, Fact] = {}

    def add(self, type_: str, statement: str, keys: Iterable[str], artifact: str,
            locator: str = "", confidence: str = "Probable", **attrs: Any) -> Fact:
        src = {"pillar": self.pillar, "artifact": artifact, "locator": locator}
        fact = Fact(type_, statement, [k for k in dict.fromkeys(keys) if k], [src], confidence, attrs=attrs)
        existing = self.facts.get(fact.fact_id)
        if existing:
            if src not in existing.sources:
                existing.sources.append(src)
            return existing
        self.facts[fact.fact_id] = fact
        return fact

    def write(self, out_dir: Path) -> Path:
        path = out_dir / "facts.json"
        write_json(path, [asdict(f) for f in self.facts.values()])
        log.info("%s: %d facts -> %s", self.pillar, len(self.facts), path)
        return path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


# ---------------------------------------------------------------- masking

CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{3,9}(\d{4})(?!\d)")
SECRET_ASSIGN = re.compile(r"((?:password|passwd|pwd|secret|token|apikey|api_key|credential|private_?key)\w*\s*[=:]\s*)\"([^\"]*)\"", re.I)
STRING_LIT = re.compile(r"\"((?:[^\"\\]|\\.){16,})\"")


def mask(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"{text[:2]}***({len(text)})" if text else ""


def mask_cards(text: str) -> str:
    return CARD_RE.sub(lambda m: f"{m.group(1)}******{m.group(2)}", text)


def _entropy(text: str) -> float:
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values()) if text else 0.0


def looks_secret_value(text: str) -> bool:
    return len(text) >= 16 and " " not in text and _entropy(text) > 3.5 and any(c.isdigit() for c in text)


def sanitize_source(text: str) -> str:
    """Mask credential assignments, high-entropy string literals and card numbers before code reaches the model."""
    text = SECRET_ASSIGN.sub(lambda m: f'{m.group(1)}"{mask(m.group(2))}"', text)
    text = STRING_LIT.sub(lambda m: f'"{mask(m.group(1))}"' if looks_secret_value(m.group(1)) else m.group(0), text)
    return mask_cards(text)


class Timer:
    def __init__(self) -> None:
        self.started = time.monotonic()

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 1)


# ---------------------------------------------------------------- Azure OpenAI settings

def _value(*candidates: str | None) -> str:
    """First real value; template placeholders such as <resource> count as unset."""
    return next((c.strip() for c in candidates if c and c.strip() and "<" not in c), "")


def _ask(prompt: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    return input(f"{prompt}: ").strip()


API_STYLES = ("v1", "deployments", "base_url")


def resolve_ai(ai: dict) -> dict:
    """Endpoint and deployment from the config, then the environment, then an interactive prompt."""
    ai.setdefault("endpoint_env", "AZURE_OPENAI_ENDPOINT")
    ai.setdefault("model_env", "AZURE_OPENAI_DEPLOYMENT")
    ai.setdefault("api_key_env", "AZURE_OPENAI_API_KEY")
    ai.setdefault("api_style", "v1")
    ai.setdefault("api_version", "2025-04-01-preview")
    ai.setdefault("auth_header", "api-key")
    ai.setdefault("headers", {})
    if ai["api_style"] not in API_STYLES:
        raise SystemExit("ai.api_style must be 'v1' (/openai/v1), 'deployments' (/openai/deployments/<name>?api-version=, classic "
                         "Azure OpenAI and most API Management imports) or 'base_url' (OpenAI-compatible base URL used as is)")
    if not isinstance(ai["headers"], dict):
        raise SystemExit("ai.headers must be an object of header names and (non-secret) values")
    endpoint = _value(os.environ.get(ai["endpoint_env"]), ai.get("endpoint")) or \
        _ask("Azure OpenAI or APIM endpoint (e.g. https://<resource>.openai.azure.com)")
    endpoint = endpoint.rstrip("/")
    if ai["api_style"] != "base_url":
        for suffix in ("/openai/v1", "/openai"):
            if endpoint.lower().endswith(suffix):
                endpoint = endpoint[: -len(suffix)]
    url = urllib.parse.urlsplit(endpoint)
    if endpoint and not (url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"))):
        raise SystemExit("the Azure OpenAI endpoint must be an https:// URL")
    ai["endpoint"] = endpoint
    ai["model"] = _value(os.environ.get(ai["model_env"]), ai.get("model")) or _ask("Azure OpenAI deployment name")
    return ai


def api_key(ai: dict) -> str:
    return os.environ.get(ai.get("api_key_env") or "AZURE_OPENAI_API_KEY", "").strip()


def copilot_provider(ai: dict, wire_api: str) -> dict:
    """GitHub Copilot SDK BYOK provider for an Azure OpenAI resource or an API Management gateway.

    The 'azure' provider type is always given the key explicitly: without one the runtime falls back to an Entra ID token
    of the signed-in Windows/Azure CLI identity, which must never be sent to a gateway.
    """
    key, header, style = api_key(ai), (ai.get("auth_header") or "api-key").strip(), ai.get("api_style", "v1")
    extra = {str(k): str(v) for k, v in (ai.get("headers") or {}).items()}
    base = ai["endpoint"].rstrip("/")
    if style == "deployments":
        provider: dict = {"type": "azure", "base_url": base, "wire_api": wire_api, "azure": {"api_version": ai["api_version"]}}
        if header.lower() == "authorization":
            provider["bearer_token"] = key
        else:
            provider["api_key"] = key  # sent as api-key
            if header.lower() != "api-key":
                extra[header] = key
    else:
        provider = {"type": "openai", "base_url": (base + "/" if style == "base_url" else base + "/openai/v1/"), "wire_api": wire_api}
        if header.lower() == "authorization":
            provider["api_key"] = key  # sent as Authorization: Bearer
        else:
            extra[header] = key
    if extra:
        provider["headers"] = extra
    return provider


def describe_endpoint(ai: dict, wire_api: str) -> str:
    """Request URL and key header for diagnostics; never the key itself."""
    base, style, model = ai["endpoint"].rstrip("/"), ai.get("api_style", "v1"), ai.get("model", "")
    op = "responses" if wire_api == "responses" else "chat/completions"
    if style == "deployments":
        url = (f"{base}/openai/responses" if op == "responses" else f"{base}/openai/deployments/{model}/{op}") + f"?api-version={ai['api_version']}"
    else:
        url = (base if style == "base_url" else base + "/openai/v1") + "/" + op
    return f"{url} (style {style}, key in header '{ai.get('auth_header') or 'api-key'}')"
