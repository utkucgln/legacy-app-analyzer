"""Sub-agents of the UI analysis team. Each one has a single responsibility and a strict JSON output schema."""
from __future__ import annotations

from typing import Any

from uiagent.llm import LLM


def obj(props: dict[str, Any]) -> dict:
    """Strict-mode JSON schema object: every property required, no extras."""
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def enum(values: list[str]) -> dict:
    return {"type": "string", "enum": list(dict.fromkeys(values)) or ["none"]}


class Agent:
    name = "agent"
    role_text = ""

    def __init__(self, llm: LLM, cfg: dict) -> None:
        self.llm = llm
        self.cfg = cfg

    def system(self) -> str:
        app = self.cfg["app"]
        return (f"You are the {self.name} in a team of AI agents analysing the legacy web application '{app['name']}'. "
                f"{app.get('description') or ''} {self.role_text} "
                "Work only from the evidence given. Never invent element ids, screens or values. Be concise.")

    def ask(self, user: str, schema_name: str, schema: dict, **kw) -> dict:
        return self.llm.structured(self.name, self.system(), user, schema_name, schema, **kw)
