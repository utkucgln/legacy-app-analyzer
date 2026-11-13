"""UI design analysis: measured design data per screen, AI design audits, cross-screen design system synthesis and reports."""
from __future__ import annotations

import csv
import html
import json
import logging
import math
import re
from collections import defaultdict
from pathlib import Path

from uiagent.agents.designer import DesignAuditor, DesignSystemArchitect
from uiagent.llm import BudgetExceeded
from uiagent.state import url_pattern

log = logging.getLogger("uiagent.design")
VARIANT_FIELDS = ("background", "color", "border", "radius", "font_size", "font_weight", "height", "padding_x")


def capture(orch, b, sig: str) -> None:
    if not orch.cfg["phases"].get("design", True) or sig in orch.state.design_raw:
        return
    try:
        orch.state.design_raw[sig] = merge_frames(b.extract_design())
    except Exception as exc:  # noqa: BLE001 - design capture must never break exploration
        log.debug("design capture failed for %s: %s", sig, exc)


def merge_frames(raw: dict) -> dict:
    frames = raw.get("frames") or []
    if not frames:
        return {}
    out = {"doc": frames[0]["doc"], "frame_docs": [f["doc"] for f in frames[1:]], "maps": {}, "a11y": {}, "legacy": {},
           "legacy_samples": {}, "layout": frames[0].get("layout", {}), "libs": sorted({x for f in frames for x in f.get("libs", [])})}
    for f in frames:
        for name, items in f.get("maps", {}).items():
            bucket = out["maps"].setdefault(name, {})
            for it in items:
                cur = bucket.setdefault(it["k"], {"k": it["k"], "n": 0, "x": it.get("x", "")})
                cur["n"] += it["n"]
        for rule, r in f.get("a11y", {}).items():
            cur = out["a11y"].setdefault(rule, {"wcag": r["wcag"], "level": r["level"], "n": 0, "samples": []})
            cur["n"] += r["n"]
            cur["samples"] = (cur["samples"] + r.get("samples", []))[:4]
        for k, n in f.get("legacy", {}).items():
            out["legacy"][k] = out["legacy"].get(k, 0) + n
        for k, s in f.get("legacy_samples", {}).items():
            out["legacy_samples"][k] = (out["legacy_samples"].get(k, []) + s)[:4]
    out["maps"] = {k: sorted(v.values(), key=lambda x: -x["n"]) for k, v in out["maps"].items()}
    return out


def parse_variant(sig: str) -> dict:
    parts = sig.split("|")
    if len(parts) != 8:
        return {"signature": sig}
    return {"background": parts[0], "color": parts[1], "border": parts[2], "radius": parts[3][1:] + "px", "font_size": parts[4],
            "font_weight": parts[5], "height": "" if parts[6] == "h-" else parts[6][1:] + "px", "padding_x": parts[7][1:] + "px"}


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def cluster_colors(counts: dict[str, int], threshold: float = 18.0) -> list[dict]:
    """Greedy clustering of near-identical colours (legacy CSS often has many one-off shades)."""
    clusters: list[dict] = []
    for hexv, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if not re.fullmatch(r"#[0-9a-f]{6}", hexv):
            continue
        for c in clusters:
            if math.dist(_rgb(c["hex"]), _rgb(hexv)) < threshold:
                c["uses"] += n
                c["members"].append(hexv)
                break
        else:
            clusters.append({"hex": hexv, "uses": n, "members": [hexv]})
    return clusters


class DesignAnalysis:
    def __init__(self, orch) -> None:
        self.o = orch
        self.cfg = orch.cfg
        self.out = orch.out / "design"
        self.out.mkdir(parents=True, exist_ok=True)

    # ================================================================ pipeline
    def run(self) -> dict:
        self._revisit_missing()
        try:
            self._audit_screens()
            aggregate = self.aggregate()
            if self.o.state.design_system is None:
                self.o.state.design_system = self._synthesise(aggregate)
                self.o._save()
        except BudgetExceeded as exc:
            log.warning("design analysis stopped: %s", exc)
            aggregate = self.aggregate()
        return self.write(aggregate)

    def _screens(self) -> list:
        limit = int(self.cfg.get("design", {}).get("max_screens", 200))
        return sorted(self.o.state.screens.values(), key=lambda s: s.url_pattern)[:limit]

    def _revisit_missing(self) -> None:
        missing = [s for s in self._screens() if s.sig not in self.o.state.design_raw]
        if not missing:
            return
        by_role: dict[str, list] = defaultdict(list)
        for s in missing:
            by_role[s.first_role].append(s)
        for role, screens in by_role.items():
            def visit(b, _role, screens=screens):
                for s in screens:
                    try:
                        b.goto(s.sample_url)
                        obs = b.observe()
                        if url_pattern(obs["url"], self.o.base) != s.url_pattern:
                            continue  # reached by POST or redirected; audit falls back to the screenshot
                        self.o.state.design_raw[s.sig] = merge_frames(b.extract_design())
                    except Exception as exc:  # noqa: BLE001
                        log.debug("revisit %s failed: %s", s.url_pattern, exc)
            visit.__name__ = "design_capture"
            self.o._with_browser(role, visit)

    def _digest(self, raw: dict) -> dict:
        if not raw:
            return {"note": "no DOM measurements (screen reached by form post); use the screenshot"}
        maps = raw["maps"]
        comps = {k[5:]: [{"uses": v["n"], "sample": v["x"], **parse_variant(v["k"])} for v in items[:6]]
                 for k, items in maps.items() if k.startswith("comp:")}
        return {"document": raw["doc"], "layout": raw["layout"], "libraries": raw["libs"],
                "typography": [{"family|size|weight": v["k"], "uses": v["n"], "sample": v["x"]} for v in maps.get("typography", [])[:12]],
                "text_colors": [(v["k"], v["n"]) for v in maps.get("text_color", [])[:10]],
                "background_colors": [(v["k"], v["n"]) for v in maps.get("background_color", [])[:10]],
                "borders": [(v["k"], v["n"]) for v in maps.get("border", [])[:6]],
                "components": comps, "wcag_automated": raw["a11y"], "legacy_markers": raw["legacy"]}

    def _audit_screens(self) -> None:
        auditor = DesignAuditor(self.o.llm, self.cfg)
        for s in self._screens():
            if s.sig in self.o.state.design_audits:
                continue
            a = s.analysis or {}
            screen = {"name": a.get("screen_name") or s.title, "url": s.url_pattern, "type": a.get("screen_type"),
                      "purpose": a.get("purpose"), "module": a.get("business_module"), "roles": sorted(s.roles)}
            shot = self.o.out / s.screenshot
            try:
                self.o.state.design_audits[s.sig] = auditor.audit(screen, self._digest(self.o.state.design_raw.get(s.sig, {})), shot)
                log.info("design audit: %s", screen["name"])
            except RuntimeError as exc:
                log.warning("design audit failed for %s: %s", s.url_pattern, exc)
            self.o._save()

    def aggregate(self) -> dict:
        screens = self._screens()
        names = {s.sig: (s.analysis or {}).get("screen_name") or s.title for s in screens}
        typo: dict[str, dict] = {}
        colors = {"text": defaultdict(int), "background": defaultdict(int), "border": defaultdict(int)}
        comps: dict[str, dict[str, dict]] = defaultdict(dict)
        a11y: dict[str, dict] = {}
        legacy: dict[str, dict] = {}
        libs: dict[str, set] = defaultdict(set)
        modes: dict[str, int] = defaultdict(int)
        for s in screens:
            raw = self.o.state.design_raw.get(s.sig)
            if not raw:
                continue
            doc = raw["doc"]
            mode = f"IE document mode {doc['document_mode']}" if doc.get("document_mode") else "non-IE engine"
            modes[f"{mode}, {'standards' if doc.get('compat_mode') == 'CSS1Compat' else 'quirks'} mode"] += 1
            for v in raw["maps"].get("typography", []):
                t = typo.setdefault(v["k"], {"uses": 0, "screens": set(), "sample": v["x"]})
                t["uses"] += v["n"]
                t["screens"].add(s.sig)
            for key, name in (("text_color", "text"), ("background_color", "background"), ("border_color", "border")):
                for v in raw["maps"].get(key, []):
                    colors[name][v["k"]] += v["n"]
            for k, items in raw["maps"].items():
                if k.startswith("comp:"):
                    for v in items:
                        c = comps[k[5:]].setdefault(v["k"], {"uses": 0, "screens": set(), "sample": v["x"]})
                        c["uses"] += v["n"]
                        c["screens"].add(s.sig)
            for rule, r in raw["a11y"].items():
                cur = a11y.setdefault(rule, {"wcag": r["wcag"], "level": r["level"], "occurrences": 0, "screens": {}, "samples": []})
                cur["occurrences"] += r["n"]
                cur["screens"][names[s.sig]] = r["n"]
                cur["samples"] = (cur["samples"] + r["samples"])[:5]
            for k, n in raw["legacy"].items():
                cur = legacy.setdefault(k, {"occurrences": 0, "screens": set(), "samples": []})
                cur["occurrences"] += n
                cur["screens"].add(names[s.sig])
                cur["samples"] = (cur["samples"] + raw["legacy_samples"].get(k, []))[:4]
            for lib in raw["libs"]:
                libs[lib].add(names[s.sig])
        return {
            "screens_measured": sum(1 for s in screens if self.o.state.design_raw.get(s.sig)),
            "screens_total": len(screens),
            "document_modes": dict(modes),
            "typography": sorted(({"family": k.split("|")[0], "size_px": int(k.split("|")[1] or 0), "weight": int(k.split("|")[2] or 400),
                                   "uses": v["uses"], "screens": len(v["screens"]), "sample": v["sample"]} for k, v in typo.items()),
                                 key=lambda x: -x["uses"]),
            "palette": {k: cluster_colors(v) for k, v in colors.items()},
            "components": {t: sorted(({**parse_variant(k), "signature": k, "uses": v["uses"], "screens": len(v["screens"]),
                                       "sample": v["sample"]} for k, v in variants.items()), key=lambda x: -x["uses"])
                           for t, variants in sorted(comps.items())},
            "accessibility": dict(sorted(a11y.items(), key=lambda kv: -len(kv[1]["screens"]))),
            "legacy_technology": {k: {**v, "screens": sorted(v["screens"])} for k, v in sorted(legacy.items(), key=lambda kv: -kv[1]["occurrences"])},
            "libraries": {k: sorted(v) for k, v in libs.items()},
        }

    def _synthesise(self, aggregate: dict) -> dict | None:
        hexes = sorted({c["hex"] for group in aggregate["palette"].values() for c in group[:20]})
        families = sorted({t["family"] for t in aggregate["typography"] if t["family"]})
        names = sorted({(s.analysis or {}).get("screen_name") or s.title for s in self._screens()})
        compact = {**aggregate, "palette": {k: [(c["hex"], c["uses"], len(c["members"])) for c in v[:20]] for k, v in aggregate["palette"].items()},
                   "typography": aggregate["typography"][:25],
                   "components": {t: [{k: v[k] for k in (*VARIANT_FIELDS, "uses", "screens", "sample") if k in v} for v in vs[:8]]
                                  for t, vs in aggregate["components"].items()},
                   "variant_counts": {t: len(vs) for t, vs in aggregate["components"].items()}}
        audits = []
        for s in self._screens():
            a = self.o.state.design_audits.get(s.sig)
            if a:
                audits.append({"screen": (s.analysis or {}).get("screen_name") or s.title, "layout": a["layout_pattern"],
                               "style": a["visual_style"][:300], "density": a["information_density"],
                               "components": sorted({c["component"] for c in a["components"]}),
                               "top_issues": [f"[{i['severity']}] {i['issue']}"[:200] for i in a["ux_issues"][:4]],
                               "complexity": a["modernization_complexity"]})
        try:
            return DesignSystemArchitect(self.o.llm, self.cfg).synthesise(compact, audits, hexes, families, names)
        except RuntimeError as exc:
            log.warning("design system synthesis failed: %s", exc)
            return None

    # ================================================================ outputs
    def write(self, agg: dict) -> dict:
        ds = self.o.state.design_system or {}
        names = {s.sig: (s.analysis or {}).get("screen_name") or s.title for s in self._screens()}
        (self.out / "design-measurements.json").write_text(json.dumps(agg, indent=1, ensure_ascii=False, default=sorted), encoding="utf-8")
        (self.out / "design-system.json").write_text(json.dumps(ds, indent=1, ensure_ascii=False), encoding="utf-8")
        (self.out / "screen-audits.json").write_text(json.dumps({names.get(k, k): v for k, v in self.o.state.design_audits.items()},
                                                                indent=1, ensure_ascii=False), encoding="utf-8")
        (self.out / "design-tokens.json").write_text(json.dumps(self._tokens(agg, ds), indent=1, ensure_ascii=False), encoding="utf-8")
        with (self.out / "components.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["component", "variant", *VARIANT_FIELDS, "uses", "screens", "sample"])
            for t, vs in agg["components"].items():
                for i, v in enumerate(vs, 1):
                    w.writerow([t, i, *[v.get(f, "") for f in VARIANT_FIELDS], v["uses"], v["screens"], v["sample"]])
        with (self.out / "accessibility-issues.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["rule", "wcag", "level", "screen", "occurrences", "samples"])
            for rule, r in agg["accessibility"].items():
                for screen, n in r["screens"].items():
                    w.writerow([rule, r["wcag"], r["level"], screen, n, " | ".join(r["samples"][:3])])
        with (self.out / "ux-issues.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["screen", "heuristic", "severity", "element", "issue", "recommendation"])
            for sig, a in self.o.state.design_audits.items():
                for i in a["ux_issues"]:
                    w.writerow([names.get(sig, sig), i["heuristic"], i["severity"], i["element"], i["issue"], i["recommendation"]])
        with (self.out / "legacy-technology.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["marker", "occurrences", "screens", "samples"])
            for k, v in agg["legacy_technology"].items():
                w.writerow([k, v["occurrences"], "; ".join(v["screens"]), " | ".join(v["samples"])])
        facts = self._facts(agg, ds, names)
        (self.out / "facts.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False), encoding="utf-8")
        (self.out / "design-report.md").write_text(self._markdown(agg, ds, names), encoding="utf-8")
        (self.out / "design-report.html").write_text(self._html(agg, ds, names), encoding="utf-8")
        ux = sum(len(a["ux_issues"]) for a in self.o.state.design_audits.values())
        return {"screens_measured": agg["screens_measured"], "screens_audited": len(self.o.state.design_audits),
                "component_types": len(agg["components"]), "component_variants": sum(len(v) for v in agg["components"].values()),
                "wcag_rules_failed": len(agg["accessibility"]), "ux_issues": ux, "legacy_markers": len(agg["legacy_technology"]),
                "design_system": bool(ds), "facts": len(facts), "report": str(self.out / "design-report.html")}

    @staticmethod
    def _tokens(agg: dict, ds: dict) -> dict:
        """W3C Design Tokens (DTCG) format: semantic roles from the synthesis plus the raw observed palette."""
        tokens: dict = {"color": {}, "typography": {}, "legacy": {"palette": {}}}
        for c in ds.get("color_roles", []):
            tokens["color"][c["role"].replace("_", "-")] = {"$type": "color", "$value": c["hex"], "$description": c["usage"]}
        for t in ds.get("typography_roles", []):
            tokens["typography"][t["role"].replace("_", "-")] = {"$type": "typography", "$value": {
                "fontFamily": t["family"], "fontSize": f"{t['size_px']}px", "fontWeight": t["weight"]}, "$description": t["usage"]}
        for group, clusters in agg["palette"].items():
            for i, c in enumerate(clusters[:16], 1):
                tokens["legacy"]["palette"][f"{group}-{i}"] = {"$type": "color", "$value": c["hex"],
                                                               "$description": f"{c['uses']} uses; variants {', '.join(c['members'][:6])}"}
        return tokens

    def _facts(self, agg: dict, ds: dict, names: dict) -> list[dict]:
        from uiagent.report import _fact

        facts = []
        for rule, r in agg["accessibility"].items():
            facts.append(_fact("finding", f"WCAG {r['wcag']} ({r['level']}) '{rule}' fails on {len(r['screens'])} screens "
                               f"({r['occurrences']} occurrences), e.g. {'; '.join(r['samples'][:2])}.", [], "design/accessibility-issues.csv", "Confirmed"))
        for k, v in agg["legacy_technology"].items():
            facts.append(_fact("finding", f"Legacy UI technology '{k}' used {v['occurrences']} times on {len(v['screens'])} screens.", [],
                               "design/legacy-technology.csv", "Confirmed"))
        for t, vs in agg["components"].items():
            if len(vs) > 1:
                facts.append(_fact("finding", f"Component '{t}' has {len(vs)} visual variants across screens (inconsistent styling).", [],
                                   "design/components.csv", "Confirmed"))
        for f in ds.get("inconsistencies", []):
            facts.append(_fact("finding", f"Design inconsistency ({f['area']}, {f['impact']}): {f['finding']}", [], "design system (AI)", "To verify"))
        for sig, a in self.o.state.design_audits.items():
            for i in a["ux_issues"]:
                if i["severity"] in ("high", "critical"):
                    facts.append(_fact("finding", f"{names.get(sig, sig)}: [{i['severity']}] {i['issue']} ({i['heuristic']})", [],
                                       "design audit (AI)", "To verify"))
        return facts

    # ---------------------------------------------------------------- markdown
    def _markdown(self, agg: dict, ds: dict, names: dict) -> str:
        e = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
        target = self.cfg.get("design", {}).get("target_design_system", "Fluent UI React v9")
        lines = [f"# UI design analysis: {self.cfg['app']['name']}", "",
                 f"{agg['screens_measured']} of {agg['screens_total']} screens measured from the live DOM; "
                 f"{len(self.o.state.design_audits)} audited by the AI Design Auditor; target design system: **{target}**.", ""]
        if ds:
            lines += ["## Summary", "", ds["summary"], "", "**Design language:** " + ds["design_language"], ""]
        lines += ["## Rendering", "", *[f"- {k}: {v} screens" for k, v in agg["document_modes"].items()],
                  f"- Libraries detected: {', '.join(agg['libraries']) or 'none'}", ""]
        if ds.get("color_roles"):
            lines += ["## Colour roles", "", "| Role | Hex | Usage |", "|---|---|---|",
                      *[f"| {c['role']} | `{c['hex']}` | {e(c['usage'])} |" for c in ds["color_roles"]], ""]
        lines += ["## Observed palette", ""]
        for group, clusters in agg["palette"].items():
            lines.append(f"- **{group}**: " + ", ".join(f"`{c['hex']}`×{c['uses']}" + (f" (+{len(c['members']) - 1} near-duplicates)" if len(c["members"]) > 1 else "")
                                                       for c in clusters[:12]))
        lines += ["", "## Typography", ""]
        if ds.get("typography_roles"):
            lines += ["| Role | Family | Size | Weight | Usage |", "|---|---|---|---|---|",
                      *[f"| {t['role']} | {t['family']} | {t['size_px']}px | {t['weight']} | {e(t['usage'])} |" for t in ds["typography_roles"]], ""]
        lines += ["Observed type scale:", "", "| Family | Size | Weight | Uses | Screens | Sample |", "|---|---|---|---|---|---|",
                  *[f"| {t['family']} | {t['size_px']}px | {t['weight']} | {t['uses']} | {t['screens']} | {e(t['sample'])[:40]} |" for t in agg["typography"][:20]], ""]
        if ds.get("spacing_and_shape"):
            lines += ["**Spacing and shape:** " + ds["spacing_and_shape"], ""]
        lines += ["## Component catalogue", "", f"| Component | Variants | Canonical | {e(target)} equivalent | Consolidation |", "|---|---|---|---|---|"]
        for c in ds.get("components", []):
            lines.append(f"| {c['component']} | {c['variants_observed']} | {e(c['canonical_variant'])} | {e(c['target_component'])} | {e(c['consolidation'])} |")
        lines += ["", "Measured variants (top 3 per component):", "", "| Component | # | Background | Text | Border | Radius | Font | Height | Uses | Screens |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for t, vs in agg["components"].items():
            for i, v in enumerate(vs[:3], 1):
                lines.append(f"| {t} | {i}/{len(vs)} | {v.get('background', '')} | {v.get('color', '')} | {v.get('border', '')} | {v.get('radius', '')} | "
                             f"{v.get('font_size', '')} {v.get('font_weight', '')} | {v.get('height', '')} | {v['uses']} | {v['screens']} |")
        if ds.get("page_templates"):
            lines += ["", "## Page templates", "", "| Template | Structure | Target layout | Screens |", "|---|---|---|---|",
                      *[f"| {e(p['template'])} | {e(p['structure'])} | {e(p['target_layout'])} | {e(', '.join(p['screens']))} |" for p in ds["page_templates"]]]
        lines += ["", "## Accessibility (automated WCAG 2.2 checks)", "", "| Rule | WCAG | Level | Screens | Occurrences | Examples |", "|---|---|---|---|---|---|",
                  *[f"| {k} | {r['wcag']} | {r['level']} | {len(r['screens'])} | {r['occurrences']} | {e('; '.join(r['samples'][:2]))[:160]} |"
                    for k, r in agg["accessibility"].items()]]
        if ds.get("accessibility_summary"):
            lines += ["", ds["accessibility_summary"]]
        lines += ["", "Automated checks cover a subset of WCAG; keyboard, screen-reader and focus-order testing remain manual.", "",
                  "## Usability findings (AI design audit)", "", "| Screen | Severity | Heuristic | Element | Issue | Recommendation |", "|---|---|---|---|---|---|"]
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        rows = sorted(((names.get(sig, sig), i) for sig, a in self.o.state.design_audits.items() for i in a["ux_issues"]),
                      key=lambda x: order.get(x[1]["severity"], 9))
        lines += [f"| {e(n)} | {i['severity']} | {i['heuristic']} | {e(i['element'])} | {e(i['issue'])} | {e(i['recommendation'])} |" for n, i in rows]
        if ds.get("inconsistencies"):
            lines += ["", "## Inconsistencies", "", *[f"- **{f['area']}** ({f['impact']}): {f['finding']}" for f in ds["inconsistencies"]]]
        lines += ["", "## Legacy UI technology", "", "| Marker | Occurrences | Screens | Examples |", "|---|---|---|---|",
                  *[f"| {k} | {v['occurrences']} | {len(v['screens'])} | {e(' / '.join(v['samples'][:2]))[:120]} |" for k, v in agg["legacy_technology"].items()]]
        for r in ds.get("legacy_technology_risks", []):
            lines.append(f"- **{r['item']}**: {r['risk']} Migration: {r['migration']}")
        if ds.get("modernization_recommendations"):
            lines += ["", "## Modernisation recommendations", "", "| # | Recommendation | Effort |", "|---|---|---|",
                      *[f"| {r['priority']} | {e(r['recommendation'])} | {r['effort']} |" for r in sorted(ds["modernization_recommendations"], key=lambda r: r["priority"])]]
        lines += ["", "## Screens", ""]
        for s in self._screens():
            a = self.o.state.design_audits.get(s.sig)
            if not a:
                continue
            lines += [f"### {names[s.sig]}", "", f"`{s.url_pattern}` · {a['layout_pattern']} · density {a['information_density']} · "
                      f"modernisation {a['modernization_complexity']} · [screenshot](../{s.screenshot.replace(chr(92), '/')})", "",
                      a["layout_description"], "", "| Component | Label | Today | Target |", "|---|---|---|---|",
                      *[f"| {c['component']} | {e(c['label'])} | {e(c['legacy_implementation'])} | {e(c['modern_equivalent'])} |" for c in a["components"]], ""]
            if a["visual_accessibility"]:
                lines += ["Visual accessibility: " + "; ".join(a["visual_accessibility"]), ""]
            lines += [f"Modernisation: {a['modernization_notes']}", ""]
        return "\n".join(lines)

    # ---------------------------------------------------------------- html
    def _html(self, agg: dict, ds: dict, names: dict) -> str:
        e = html.escape
        target = self.cfg.get("design", {}).get("target_design_system", "Fluent UI React v9")
        css = ("body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222;max-width:1400px}h1{color:#203864}h2{border-bottom:2px solid #305496;padding-bottom:4px;margin-top:32px}"
               "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px;vertical-align:top;text-align:left}"
               "th{background:#305496;color:#fff}.sw{display:inline-block;width:110px;margin:4px;font-size:11px;text-align:center}"
               ".sw div{height:48px;border:1px solid #999;border-radius:4px}.card{border:1px solid #ddd;padding:12px;margin:12px 0;border-radius:6px}"
               "img.shot{max-width:560px;border:1px solid #999}.sev-critical,.sev-high{color:#b00020;font-weight:600}.sev-medium{color:#b26a00}"
               ".pill{display:inline-block;padding:2px 8px;border-radius:10px;background:#eef;margin:2px;font-size:12px}")
        p = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>UI design analysis - {e(self.cfg['app']['name'])}</title><style>{css}</style></head><body>",
             f"<h1>UI design analysis: {e(self.cfg['app']['name'])}</h1>",
             f"<p>{agg['screens_measured']} of {agg['screens_total']} screens measured · {len(self.o.state.design_audits)} AI-audited · "
             f"target design system <b>{e(target)}</b></p>"]
        if ds:
            p.append(f"<h2>Summary</h2><p>{e(ds['summary'])}</p><p><b>Design language:</b> {e(ds['design_language'])}</p>")
        p.append("<h2>Colour</h2>")
        if ds.get("color_roles"):
            p.append("".join(f"<span class='sw'><div style='background:{e(c['hex'])}'></div><b>{e(c['role'])}</b><br>{e(c['hex'])}</span>" for c in ds["color_roles"]))
        for group, clusters in agg["palette"].items():
            p.append(f"<h3>Observed {e(group)} colours</h3>" + "".join(
                f"<span class='sw' title='{e(', '.join(c['members']))}'><div style='background:{e(c['hex'])}'></div>{e(c['hex'])}<br>{c['uses']} uses"
                + (f" · {len(c['members'])} shades" if len(c["members"]) > 1 else "") + "</span>" for c in clusters[:16]))
        p.append("<h2>Typography</h2><table><tr><th>Sample</th><th>Family</th><th>Size</th><th>Weight</th><th>Uses</th><th>Screens</th></tr>")
        for t in agg["typography"][:20]:
            p.append(f"<tr><td style=\"font-family:'{e(t['family'])}';font-size:{t['size_px']}px;font-weight:{t['weight']}\">{e(t['sample'][:40] or 'Aa')}</td>"
                     f"<td>{e(t['family'])}</td><td>{t['size_px']}px</td><td>{t['weight']}</td><td>{t['uses']}</td><td>{t['screens']}</td></tr>")
        p.append("</table><h2>Component catalogue</h2>")
        if ds.get("components"):
            p.append(f"<table><tr><th>Component</th><th>Variants</th><th>Canonical</th><th>{e(target)}</th><th>Consolidation</th></tr>" + "".join(
                f"<tr><td>{e(c['component'])}</td><td>{c['variants_observed']}</td><td>{e(c['canonical_variant'])}</td><td>{e(c['target_component'])}</td>"
                f"<td>{e(c['consolidation'])}</td></tr>" for c in ds["components"]) + "</table>")
        p.append("<h3>Measured variants (rendered from captured styles)</h3><table><tr><th>Component</th><th>Preview</th><th>Style</th><th>Uses</th><th>Screens</th></tr>")
        for t, vs in agg["components"].items():
            for i, v in enumerate(vs[:4], 1):
                if "background" not in v:
                    continue
                bg = v["background"] if v["background"] != "-" else "transparent"
                fg = v["color"] if v["color"] != "-" else "#000"
                border = v["border"] if v["border"] != "none" else "1px dashed #ddd"
                style = (f"background:{bg};color:{fg};border:{border};border-radius:{v['radius']};font-size:{v['font_size']};"
                         f"font-weight:{v['font_weight']};padding:2px {v['padding_x']};display:inline-block;min-width:60px")
                p.append(f"<tr><td>{e(t)} {i}/{len(vs)}</td><td><span style='{e(style)}'>{e((v['sample'] or t)[:30])}</span></td>"
                         f"<td><code>{e(' · '.join(str(v[f]) for f in VARIANT_FIELDS))}</code></td><td>{v['uses']}</td><td>{v['screens']}</td></tr>")
        p.append("</table>")
        if ds.get("page_templates"):
            p.append("<h2>Page templates</h2><table><tr><th>Template</th><th>Structure</th><th>Target layout</th><th>Screens</th></tr>" + "".join(
                f"<tr><td>{e(t['template'])}</td><td>{e(t['structure'])}</td><td>{e(t['target_layout'])}</td><td>{e(', '.join(t['screens']))}</td></tr>"
                for t in ds["page_templates"]) + "</table>")
        p.append("<h2>Accessibility (automated WCAG 2.2)</h2><table><tr><th>Rule</th><th>WCAG</th><th>Level</th><th>Screens</th><th>Occurrences</th><th>Examples</th></tr>")
        for k, r in agg["accessibility"].items():
            p.append(f"<tr><td>{e(k)}</td><td>{e(r['wcag'])}</td><td>{e(r['level'])}</td><td>{len(r['screens'])}</td><td>{r['occurrences']}</td>"
                     f"<td>{e('; '.join(r['samples'][:3]))}</td></tr>")
        p.append("</table>")
        if ds.get("accessibility_summary"):
            p.append(f"<p>{e(ds['accessibility_summary'])}</p>")
        p.append("<h2>Usability findings</h2><table><tr><th>Screen</th><th>Severity</th><th>Heuristic</th><th>Issue</th><th>Recommendation</th></tr>")
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for n, i in sorted(((names.get(sig, sig), i) for sig, a in self.o.state.design_audits.items() for i in a["ux_issues"]),
                           key=lambda x: order.get(x[1]["severity"], 9)):
            p.append(f"<tr><td>{e(n)}</td><td class='sev-{e(i['severity'])}'>{e(i['severity'])}</td><td>{e(i['heuristic'])}</td>"
                     f"<td><b>{e(i['element'])}</b>: {e(i['issue'])}</td><td>{e(i['recommendation'])}</td></tr>")
        p.append("</table>")
        if agg["legacy_technology"] or ds.get("legacy_technology_risks"):
            p.append("<h2>Legacy UI technology</h2><table><tr><th>Marker</th><th>Occurrences</th><th>Screens</th><th>Examples</th></tr>" + "".join(
                f"<tr><td>{e(k)}</td><td>{v['occurrences']}</td><td>{len(v['screens'])}</td><td>{e(' / '.join(v['samples'][:2]))}</td></tr>"
                for k, v in agg["legacy_technology"].items()) + "</table>")
            p.append("<ul>" + "".join(f"<li><b>{e(r['item'])}</b>: {e(r['risk'])} <i>Migration:</i> {e(r['migration'])}</li>"
                                      for r in ds.get("legacy_technology_risks", [])) + "</ul>")
        if ds.get("modernization_recommendations"):
            p.append("<h2>Modernisation recommendations</h2><table><tr><th>#</th><th>Recommendation</th><th>Effort</th></tr>" + "".join(
                f"<tr><td>{r['priority']}</td><td>{e(r['recommendation'])}</td><td>{e(r['effort'])}</td></tr>"
                for r in sorted(ds["modernization_recommendations"], key=lambda r: r["priority"])) + "</table>")
        p.append("<h2>Screens</h2>")
        for s in self._screens():
            a = self.o.state.design_audits.get(s.sig)
            if not a:
                continue
            shot = "../" + s.screenshot.replace("\\", "/")
            p.append(f"<div class='card'><h3>{e(names[s.sig])}</h3><p><code>{e(s.url_pattern)}</code> "
                     f"<span class='pill'>{e(a['layout_pattern'])}</span><span class='pill'>density {e(a['information_density'])}</span>"
                     f"<span class='pill'>modernisation {e(a['modernization_complexity'])}</span></p>"
                     f"<a href='{e(shot)}' target='_blank'><img class='shot' src='{e(shot)}' loading='lazy' alt='Screenshot of {e(names[s.sig])}'></a>"
                     f"<p>{e(a['layout_description'])}</p><p><i>{e(a['visual_style'])}</i></p>"
                     "<table><tr><th>Component</th><th>Label</th><th>Today</th><th>Target</th></tr>" + "".join(
                         f"<tr><td>{e(c['component'])}</td><td>{e(c['label'])}</td><td>{e(c['legacy_implementation'])}</td><td>{e(c['modern_equivalent'])}</td></tr>"
                         for c in a["components"]) + "</table>"
                     + (f"<p><b>Visual accessibility:</b> {e('; '.join(a['visual_accessibility']))}</p>" if a["visual_accessibility"] else "")
                     + f"<p><b>Modernisation:</b> {e(a['modernization_notes'])}</p></div>")
        p.append("</body></html>")
        return "".join(p)
