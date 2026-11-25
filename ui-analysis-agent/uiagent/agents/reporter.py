"""Reporter agent: writes the executive narrative; the deterministic report files are produced by uiagent.report."""
from __future__ import annotations

import json

from uiagent.agents import STR, Agent, arr, obj

REPORT_SCHEMA = obj({
    "executive_summary": STR,
    "module_summaries": arr(obj({"module": STR, "summary": STR, "screens": arr(STR)})),
    "recommendations": arr(STR),
    "coverage_gaps": arr(STR),
})


class Reporter(Agent):
    name = "Reporter"
    role_text = ("You write the executive summary of a UI reverse-engineering exercise for business analysts and "
                 "modernisation architects: what the application does, how it is organised, who can do what, notable "
                 "rules and risks, and what was not covered. Only state what the evidence shows.")

    def summarise(self, screens: list[dict], comparison: dict | None, coverage: dict, probes: list[dict]) -> dict:
        user = (f"SCREENS: {json.dumps(screens)[:40000]}\n\nROLE COMPARISON: {json.dumps(comparison)[:8000]}\n\n"
                f"COVERAGE: {json.dumps(coverage)}\n\nVALIDATION PROBES: {json.dumps(probes)[:6000]}")
        return self.ask(user, "ui_report", REPORT_SCHEMA, max_tokens=6000)
