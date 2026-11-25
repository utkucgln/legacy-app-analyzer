"""Planner agent: turns the landing page of a role into prioritised exploration goals, and re-plans when goals run out."""
from __future__ import annotations

import json

from uiagent.agents import INT, STR, Agent, arr, obj

PLAN_SCHEMA = obj({
    "role_summary": STR,
    "goals": arr(obj({"goal_id": STR, "title": STR, "entry_hint": STR, "priority": INT, "why": STR})),
    "coverage_notes": STR,
})


class Planner(Agent):
    name = "Planner"
    role_text = ("You plan how one user role should explore the application so that every screen and function available "
                 "to that role is visited at least once, read-only. Goals are business functions (e.g. 'Customer search', "
                 "'Approval queue'), each with an entry hint naming the menu item, link or URL to start from. "
                 "Priority 1 is highest. Prefer breadth: one goal per menu entry or functional area.")

    def plan(self, role: str, role_desc: str, landing: str, declared: list[str], other_roles: dict[str, list[str]]) -> dict:
        user = (f"Role: {role} ({role_desc or 'no description'})\n"
                f"Landing page after login:\n{landing}\n\n"
                f"Declared application paths from code analysis (may not all be allowed for this role): {json.dumps(declared[:150])}\n"
                f"Screens already discovered by other roles: {json.dumps(other_roles)[:3000]}\n"
                "Produce 5-40 goals covering everything this role can reach.")
        return self.ask(user, "exploration_plan", PLAN_SCHEMA)

    def replan(self, role: str, done_goals: list[dict], visited: list[str], untried: list[str], declared: list[str]) -> dict:
        user = (f"Role: {role}\nGoals finished: {json.dumps(done_goals)[:4000]}\n"
                f"Screens visited: {json.dumps(visited)[:4000]}\n"
                f"Links/menus seen but not yet opened: {json.dumps(untried)[:3000]}\n"
                f"Declared paths not yet visited: {json.dumps(declared[:100])}\n"
                "Return only NEW goals that could still reveal unvisited screens; return an empty list if coverage is complete.")
        return self.ask(user, "exploration_replan", PLAN_SCHEMA)
