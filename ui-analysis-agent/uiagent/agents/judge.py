"""Access Judge agent: classifies what a role got when opening a screen URL directly."""
from __future__ import annotations

import re

from uiagent.agents import STR, Agent, enum, obj

OUTCOMES = ["allowed", "denied", "login_required", "error", "not_found", "redirected"]
JUDGE_SCHEMA = obj({"outcome": enum(OUTCOMES), "evidence": STR})
DENIED = re.compile(r"(access denied|not authori[sz]ed|forbidden|permission|insufficient (rights|privileges)|not allowed|غير مصرح)", re.I)
ERROR = re.compile(r"(exception|stack trace|internal server error|http status 5\d\d|an error (has )?occurred)", re.I)
NOT_FOUND = re.compile(r"(404|not found|page cannot be (found|displayed))", re.I)


class Judge(Agent):
    name = "Access Judge"
    role_text = "You classify the result of opening a screen URL directly as a given role."

    def judge(self, role: str, requested: str, landed_pattern: str, expected_pattern: str, obs: dict, page: str) -> dict:
        text = " ".join([obs.get("title", ""), *obs.get("headings", []), *obs.get("messages", []), obs.get("text", "")[:600]])
        if obs.get("has_password"):
            return {"outcome": "login_required", "evidence": "login form shown"}
        if DENIED.search(text):
            return {"outcome": "denied", "evidence": DENIED.search(text).group(0)}
        if NOT_FOUND.search(obs.get("title", "")):
            return {"outcome": "not_found", "evidence": obs.get("title", "")}
        if ERROR.search(text[:400]):
            return {"outcome": "error", "evidence": ERROR.search(text[:400]).group(0)}
        if landed_pattern.split("?")[0] == expected_pattern.split("?")[0]:
            return {"outcome": "allowed", "evidence": "requested screen rendered"}
        user = (f"Role {role} requested {requested} (expected screen {expected_pattern}) and landed on {landed_pattern}.\n"
                f"PAGE\n{page[:4000]}")
        return self.ask(user, "access_outcome", JUDGE_SCHEMA, max_tokens=400)
