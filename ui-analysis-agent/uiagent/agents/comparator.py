"""Role Comparator agent: explains the authorisation model and flags anomalies from the role x screen matrix."""
from __future__ import annotations

import json

from uiagent.agents import STR, Agent, arr, enum, obj

COMPARE_SCHEMA = obj({
    "authorization_model": STR,
    "role_profiles": arr(obj({"role": STR, "summary": STR, "key_capabilities": arr(STR)})),
    "findings": arr(obj({"severity": enum(["high", "medium", "low", "info"]), "title": STR, "detail": STR,
                         "screens": arr(STR), "roles": arr(STR)})),
})


class Comparator(Agent):
    name = "Role Comparator"
    role_text = ("You compare what each role can see and do. Explain the authorisation model in business terms and flag "
                 "anomalies: screens reachable by direct URL but hidden from the menu, actions visible to a role that should "
                 "not have them, segregation-of-duties gaps (same role can create and approve), inconsistent enforcement.")

    def compare(self, matrix: list[dict], screens: list[dict]) -> dict:
        user = (f"ROLE x SCREEN MATRIX (menu = linked for role, crawl = reached by exploring, direct = result of opening URL):\n"
                f"{json.dumps(matrix)[:30000]}\n\nSCREENS:\n{json.dumps(screens)[:20000]}")
        return self.ask(user, "role_comparison", COMPARE_SCHEMA, max_tokens=6000)
