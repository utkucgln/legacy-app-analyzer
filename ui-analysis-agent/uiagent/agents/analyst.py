"""Screen Analyst agent: documents each unique screen from its screenshot and DOM (purpose, fields, actions, rules)."""
from __future__ import annotations

from pathlib import Path

from uiagent.agents import BOOL, STR, Agent, arr, enum, obj

RISK = enum(["read_only", "data_change", "destructive", "unknown"])


def analysis_schema(modules: list[str]) -> dict:
    return obj({
        "screen_name": STR,
        "purpose": STR,
        "business_module": enum(modules + ["Other"]),
        "screen_type": enum(["home", "menu", "search", "list", "detail", "form_create", "form_edit", "approval", "report",
                             "admin", "error", "login", "access_denied", "other"]),
        "fields": arr(obj({"label": STR, "name": STR, "type": STR, "required": BOOL, "constraints": STR, "options": arr(STR)})),
        "actions": arr(obj({"label": STR, "kind": enum(["navigate", "query", "create", "update", "delete", "approve", "reject",
                                                         "submit", "export", "print", "other"]), "risk": RISK})),
        "data_columns": arr(STR),
        "visible_rules": arr(STR),
        "messages": arr(STR),
        "notes": STR,
    })


class Analyst(Agent):
    name = "Screen Analyst"
    role_text = ("You document one screen for a reverse-engineering report. Use the screenshot for layout, labels and "
                 "visual cues (mandatory markers, disabled controls, tabs) and the DOM listing for exact names and options. "
                 "'visible_rules' are business rules a user can infer from the screen itself: mandatory fields, formats, "
                 "length limits, hints, conditional buttons, warning texts. Do not guess hidden behaviour.")

    def analyse(self, role: str, page: str, screenshot: Path | None) -> dict:
        user = f"Role viewing the screen: {role}\n\nDOM LISTING\n{page}"
        images = [screenshot] if screenshot and self.cfg["ai"].get("vision", True) else None
        return self.ask(user, "screen_analysis", analysis_schema(self.cfg["app"]["modules"]), images=images, max_tokens=4000)
