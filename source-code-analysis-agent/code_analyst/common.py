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
    banca_module: str = ""
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


def resolve_ai(ai: dict) -> dict:
    """Endpoint and deployment from the config, then the environment, then an interactive prompt."""
    ai.setdefault("endpoint_env", "AZURE_OPENAI_ENDPOINT")
    ai.setdefault("model_env", "AZURE_OPENAI_DEPLOYMENT")
    ai.setdefault("api_key_env", "AZURE_OPENAI_API_KEY")
    endpoint = _value(os.environ.get(ai["endpoint_env"]), ai.get("endpoint")) or _ask("Azure OpenAI endpoint (e.g. https://<resource>.openai.azure.com)")
    endpoint = endpoint.rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if endpoint.lower().endswith(suffix):
            endpoint = endpoint[: -len(suffix)]
    if endpoint and not endpoint.lower().startswith("https://"):
        raise SystemExit("the Azure OpenAI endpoint must be an https:// URL")
    ai["endpoint"] = endpoint
    ai["model"] = _value(os.environ.get(ai["model_env"]), ai.get("model")) or _ask("Azure OpenAI deployment name")
    return ai


def api_key(ai: dict) -> str:
    return os.environ.get(ai.get("api_key_env") or "AZURE_OPENAI_API_KEY", "").strip()
