"""Guard agent: second opinion on actions the deterministic policy cannot classify (e.g. ambiguous or non-English labels)."""
from __future__ import annotations

import json

from uiagent.agents import BOOL, STR, Agent, enum, obj

GUARD_SCHEMA = obj({
    "read_only": BOOL,
    "category": enum(["navigation", "query", "data_change", "destructive", "unknown"]),
    "reason": STR,
})


class Guard(Agent):
    name = "Safety Guard"
    role_text = ("You protect the application's data. Decide whether clicking an element is strictly read-only "
                 "(navigation, search, filter, paging, viewing details, opening a report on screen). Anything that could "
                 "create, change, submit, approve, send or delete data is NOT read-only. When in doubt answer read_only=false.")

    def review(self, element: dict, form: dict | None, page_title: str, mode: str) -> dict:
        user = (f"Safety mode: {mode}\nPage: {page_title}\nElement: {json.dumps(element)}\nForm: {json.dumps(form)}\n"
                "Is activating this element strictly read-only?")
        return self.ask(user, "safety_review", GUARD_SCHEMA, max_tokens=600)
