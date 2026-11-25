"""Validation Prober agent (opt-in, test environments only): designs negative test inputs for forms."""
from __future__ import annotations

from uiagent.agents import STR, Agent, arr, enum, obj


def probe_schema(fillable: list[str], buttons: list[str]) -> dict:
    return obj({"cases": arr(obj({
        "name": STR,
        "inputs": arr(obj({"element_id": enum(fillable + ["none"]), "value": STR})),
        "submit_element_id": enum(buttons + ["none"]),
        "expectation": STR,
    }))})


class Prober(Agent):
    name = "Validation Prober"
    role_text = ("You design negative tests that reveal validation rules without creating valid records: leave mandatory "
                 "fields empty, exceed length limits, use wrong formats, out-of-range amounts and dates. Every case must "
                 "contain at least one invalid value so the server rejects it. Never target delete/approve/reject buttons.")

    def design(self, page: str, fillable: list[str], buttons: list[str], max_cases: int) -> dict:
        user = f"FORM SCREEN\n{page}\n\nDesign up to {max_cases} cases."
        return self.ask(user, "validation_probes", probe_schema(fillable, buttons))
