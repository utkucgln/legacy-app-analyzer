"""Navigator agent: decides the next browser action towards the current goal, choosing only from elements on the page."""
from __future__ import annotations

import json

from uiagent.agents import STR, Agent, arr, enum, obj

ACTIONS = ["click", "fill_and_click", "back", "home", "goal_done", "goal_unreachable", "role_done"]


def decision_schema(clickable: list[str], fillable: list[str]) -> dict:
    return obj({
        "action": enum(ACTIONS),
        "element_id": enum(clickable + ["none"]),
        "inputs": arr(obj({"element_id": enum(fillable + ["none"]), "value": STR})),
        "intent": STR,
        "expected_result": STR,
        "risk": enum(["read_only", "data_change", "destructive", "unknown"]),
    })


class Navigator(Agent):
    name = "Navigator"
    role_text = ("You choose the single next action for a browser exploring the application as a given role. "
                 "Objectives: reach the current goal, discover screens not yet visited, and never change data. "
                 "Use 'fill_and_click' only for search/filter/lookup forms, filling them with values seen earlier (known values) "
                 "or obvious wildcards, so that list and detail screens become visible. Never click buttons or links that save, "
                 "submit for approval, approve, reject, delete, cancel, pay or log out. Prefer unvisited menu entries and links. "
                 "Use 'goal_done' when the goal's screen has been reached and its sub-screens explored, 'goal_unreachable' if the "
                 "role cannot reach it, 'back'/'home' to escape dead ends, 'role_done' only when no goal remains.")

    def decide(self, role: str, goal: dict, page: str, clickable: list[str], fillable: list[str], history: list[dict],
               visited: list[str], tried_here: list[str], known_values: dict, blocked: list[str]) -> dict:
        user = (f"Role: {role}\nCurrent goal: {json.dumps(goal)}\n\nCURRENT PAGE\n{page}\n\n"
                f"Elements already tried on this screen: {tried_here}\n"
                f"Elements blocked by the safety policy (do not choose): {blocked}\n"
                f"Screens visited so far by this role: {json.dumps(visited)[:2500]}\n"
                f"Known identifier values from earlier tables: {json.dumps(known_values)[:800]}\n"
                f"Last steps: {json.dumps(history[-8:])[:2500]}")
        return self.ask(user, "next_action", decision_schema(clickable, fillable), max_tokens=1500)
