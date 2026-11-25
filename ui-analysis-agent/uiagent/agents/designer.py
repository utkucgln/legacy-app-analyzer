"""Design agents: the Design Auditor reviews one screen (screenshot + measured styles); the Design System Architect
synthesises tokens, a component catalogue and a modernisation mapping across all screens."""
from __future__ import annotations

import json
from pathlib import Path

from uiagent.agents import INT, STR, Agent, arr, enum, obj

COMPONENTS = ["header", "navigation_menu", "tabs", "breadcrumb", "toolbar", "data_grid", "pagination", "search_form", "data_entry_form",
              "text_input", "password_input", "date_picker", "select", "multi_select", "checkbox", "radio_group", "button_primary", "button_secondary",
              "icon_button", "link", "message_banner", "modal_dialog", "popup_window", "file_upload", "tree", "accordion", "card",
              "status_badge", "footer", "other"]
HEURISTICS = ["visibility_of_status", "match_real_world", "user_control", "consistency_standards", "error_prevention",
              "recognition_over_recall", "flexibility_efficiency", "aesthetic_minimalist", "error_recovery", "help_documentation"]
SEVERITY = enum(["low", "medium", "high", "critical"])

AUDIT_SCHEMA = obj({
    "layout_pattern": enum(["header_sidebar_content", "header_content", "frameset", "single_form", "list_detail", "master_detail",
                            "dashboard", "popup", "login", "other"]),
    "layout_description": STR,
    "visual_style": STR,
    "information_density": enum(["low", "medium", "high", "very_high"]),
    "components": arr(obj({"component": enum(COMPONENTS), "label": STR, "legacy_implementation": STR, "observations": STR,
                           "modern_equivalent": STR})),
    "ux_issues": arr(obj({"heuristic": enum(HEURISTICS), "severity": SEVERITY, "element": STR, "issue": STR, "recommendation": STR})),
    "visual_accessibility": arr(STR),
    "responsiveness": STR,
    "modernization_complexity": enum(["low", "medium", "high"]),
    "modernization_notes": STR,
})


def system_schema(hexes: list[str], families: list[str], screens: list[str]) -> dict:
    return obj({
        "summary": STR,
        "design_language": STR,
        "color_roles": arr(obj({"role": enum(["primary", "primary_dark", "secondary", "accent", "surface", "surface_alt", "text_primary",
                                              "text_secondary", "border", "link", "error", "warning", "success", "info", "disabled", "other"]),
                                "hex": enum(hexes), "usage": STR})),
        "typography_roles": arr(obj({"role": enum(["display", "page_title", "section_title", "body", "body_strong", "label", "caption",
                                                   "table_cell", "button", "other"]),
                                     "family": enum(families), "size_px": INT, "weight": INT, "usage": STR})),
        "spacing_and_shape": STR,
        "components": arr(obj({"component": enum(COMPONENTS), "variants_observed": INT, "canonical_variant": STR, "target_component": STR,
                               "consolidation": STR})),
        "page_templates": arr(obj({"template": STR, "screens": arr(enum(screens)), "structure": STR, "target_layout": STR})),
        "inconsistencies": arr(obj({"area": STR, "finding": STR, "impact": enum(["low", "medium", "high"])})),
        "accessibility_summary": STR,
        "legacy_technology_risks": arr(obj({"item": STR, "risk": STR, "migration": STR})),
        "modernization_recommendations": arr(obj({"priority": INT, "recommendation": STR, "effort": enum(["S", "M", "L", "XL"])})),
    })


class DesignAuditor(Agent):
    name = "Design Auditor"
    role_text = ("You are a senior UI/UX designer auditing one screen of a legacy application that will be modernised. Use the "
                 "screenshot for layout, hierarchy, alignment, density, visual affordances and focus/state cues; use the measured style "
                 "and component data for exact values. List every distinct UI component with how it is built today and its modern "
                 "equivalent in the target design system. Report usability issues against Nielsen's heuristics with concrete, "
                 "element-level evidence; do not repeat automated WCAG findings unless the screenshot adds evidence. "
                 "visual_accessibility covers what automated checks cannot see (focus visibility, colour-only meaning, icon-only "
                 "controls, text in images, crowding, reading order).")

    def audit(self, screen: dict, measured: dict, screenshot: Path | None) -> dict:
        target = self.cfg.get("design", {}).get("target_design_system", "Fluent UI React v9")
        user = (f"Target design system: {target}\nScreen: {json.dumps(screen, ensure_ascii=False)}\n\n"
                f"MEASURED STYLES AND COMPONENTS (from the live DOM)\n{json.dumps(measured, ensure_ascii=False)[:9000]}")
        images = [screenshot] if screenshot and screenshot.exists() and self.cfg["ai"].get("vision", True) else None
        return self.ask(user, "screen_design_audit", AUDIT_SCHEMA, images=images, max_tokens=6000)


class DesignSystemArchitect(Agent):
    name = "Design System Architect"
    role_text = ("You reverse-engineer the implicit design system of a legacy application from measurements across all its screens, "
                 "then map it to the target design system for modernisation. Assign colour and typography roles only from the observed "
                 "values; a role may be omitted if no value fits. Count variants from the data, choose a canonical variant per component, "
                 "and state how to consolidate the others. Group screens into page templates. Prioritise recommendations (1 = first).")

    def synthesise(self, aggregate: dict, audits: list[dict], hexes: list[str], families: list[str], screens: list[str]) -> dict:
        target = self.cfg.get("design", {}).get("target_design_system", "Fluent UI React v9")
        user = (f"Target design system: {target}\n\nCROSS-SCREEN MEASUREMENTS\n{json.dumps(aggregate, ensure_ascii=False)[:24000]}\n\n"
                f"PER-SCREEN AUDITS (digest)\n{json.dumps(audits, ensure_ascii=False)[:24000]}")
        return self.ask(user, "design_system", system_schema(hexes or ["#000000"], families or ["unknown"], screens or ["none"]),
                        max_tokens=12000)
