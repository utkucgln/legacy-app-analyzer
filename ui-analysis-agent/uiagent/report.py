"""Report files: report.md / report.html, screens.json, role-matrix.csv, blocked-actions.csv, transitions.csv,
facts.json (evidence fact format shared with the Legacy Decompiler and Source Code Analysis Agents) and crawler-sessions.txt."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from uiagent.state import RunState


def _path_key(pattern: str) -> str:
    return "path:" + re.sub(r"\.do$", "", pattern.split("?")[0])


def _fact(type_: str, statement: str, keys: list[str], locator: str, confidence: str = "Probable", **attrs) -> dict:
    digest = hashlib.sha1(f"{type_}|{statement}|{'|'.join(sorted(keys))}".encode("utf-8")).hexdigest()[:10]
    return {"type": type_, "statement": statement, "keys": list(dict.fromkeys(keys)),
            "sources": [{"pillar": "ui", "artifact": "ui-agent", "locator": locator}], "confidence": confidence,
            "conflicts": [], "banca_module": attrs.pop("module", ""), "attrs": attrs, "fact_id": f"UI-{type_}-{digest}"}


def write(out: Path, cfg: dict, state: RunState, matrix: list[dict], narrative: dict | None, coverage: dict) -> dict:
    roles = list(cfg["roles"])
    screens = sorted(state.screens.values(), key=lambda s: ((s.analysis or {}).get("business_module", ""), s.url_pattern))
    (out / "screens.json").write_text(json.dumps([asdict(s) for s in screens], indent=1, ensure_ascii=False), encoding="utf-8")
    with (out / "role-matrix.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["screen", "url", "module", "type", *[f"{r}:crawl" for r in roles], *[f"{r}:direct" for r in roles]])
        for row in matrix:
            w.writerow([row["screen"], row["url"], row["module"], row["type"], *[row[r]["crawl"] for r in roles],
                        *[row[r]["direct"] for r in roles]])
    with (out / "blocked-actions.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["role", "screen", "screen_name", "element", "kind", "reason"])
        w.writeheader()
        seen = set()
        for b in state.blocked:
            key = (b["role"], b["screen"], b["element"])
            if key not in seen:
                seen.add(key)
                w.writerow(b)
    with (out / "transitions.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["role", "from", "to", "via", "action", "popup"])
        w.writeheader()
        w.writerows(state.transitions)
    (out / "crawler-sessions.txt").write_text("\n".join(state.sessions), encoding="utf-8")
    facts = _facts(state, matrix, roles)
    (out / "facts.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False), encoding="utf-8")
    md = _markdown(cfg, state, screens, matrix, narrative, coverage, roles)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.html").write_text(_html(cfg, state, screens, matrix, narrative, coverage, roles), encoding="utf-8")
    summary = {"finished": datetime.now(timezone.utc).isoformat(timespec="seconds"), **coverage, "facts": len(facts),
               "errors_sample": state.errors[:10]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    return summary


# ---------------------------------------------------------------- evidence facts

def _facts(state: RunState, matrix: list[dict], roles: list[str]) -> list[dict]:
    facts = []
    for s in state.screens.values():
        a = s.analysis or {}
        name = a.get("screen_name") or s.title
        facts.append(_fact("screen", f"Screen '{name}' ({a.get('screen_type', '?')}) at {s.url_pattern}: {a.get('purpose', '')} "
                           f"Reached by roles {', '.join(sorted(s.roles))}.", [_path_key(s.url_pattern)], s.screenshot, "Confirmed",
                           module=a.get("business_module", ""), roles=sorted(s.roles)))
        for f in a.get("fields", []):
            facts.append(_fact("control", f"Field '{f['label'] or f['name']}' ({f['type']}){' required' if f['required'] else ''} on {name}"
                               + (f"; {f['constraints']}" if f.get("constraints") else "")
                               + (f"; options {', '.join(f['options'][:20])}" if f.get("options") else "") + ".",
                               [f"field:{_path_key(s.url_pattern)[5:]}#{f['name']}", _path_key(s.url_pattern)], s.screenshot,
                               module=a.get("business_module", "")))
        for rule in a.get("visible_rules", []):
            facts.append(_fact("rule", f"{name}: {rule}", [_path_key(s.url_pattern)], s.screenshot, module=a.get("business_module", "")))
    for row in matrix:
        allowed = [r for r in roles if row[r]["crawl"] or row[r]["direct"] == "allowed"]
        denied = [r for r in roles if r not in allowed]
        if denied and allowed:
            facts.append(_fact("role", f"{row['screen']} ({row['url']}) is available to {', '.join(allowed)} and not to {', '.join(denied)}.",
                               [_path_key(row["url"]), *[f"role:{r}" for r in allowed]], "role-matrix.csv", "Confirmed", allowed=allowed))
        hidden = [r for r in roles if not row[r]["crawl"] and row[r]["direct"] == "allowed"]
        if hidden:
            facts.append(_fact("finding", f"{row['screen']} ({row['url']}) is not reachable through the menus for {', '.join(hidden)} "
                               "but opens by direct URL (menu hiding only).", [_path_key(row["url"]), *[f"role:{r}" for r in hidden]],
                               "role-matrix.csv", "Confirmed"))
    actions: dict[tuple, set] = {}
    for b in state.blocked:
        actions.setdefault((b["screen_name"], b["element"]), set()).add(b["role"])
    for (screen_name, element), rs in actions.items():
        if element:
            facts.append(_fact("control", f"Action '{element}' on {screen_name} is available to roles {', '.join(sorted(rs))} "
                               "(not executed: safety policy).", [f"role:{r}" for r in sorted(rs)], "blocked-actions.csv", "Confirmed"))
    for d in {d["text"]: d for d in state.dialogs}.values():
        facts.append(_fact("rule", f"Dialog shown to {d.get('role')}: \"{d['text'][:200]}\".", [], "dialogs", "Confirmed"))
    for p in state.probes:
        if p["messages"] or p["dialogs"]:
            facts.append(_fact("rule", f"{p['screen']}: {p['case']} -> {' | '.join(p['messages'] + p['dialogs'])[:400]}",
                               [], "probes", "Confirmed"))
    for f in (state.comparison or {}).get("findings", []):
        facts.append(_fact("finding", f"[{f['severity']}] {f['title']}: {f['detail']}", [f"role:{r}" for r in f["roles"]],
                           "role comparison (AI)", "To verify"))
    return facts


# ---------------------------------------------------------------- Markdown

def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _markdown(cfg, state, screens, matrix, narrative, coverage, roles) -> str:
    app = cfg["app"]["name"]
    lines = [f"# UI analysis report: {app}", "",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by the AI UI Analyst. Engine: `{cfg['browser']['engine']}`, "
             f"safety mode: `{cfg['safety']['mode']}`, roles: {', '.join(roles)}.", ""]
    if narrative:
        lines += ["## Executive summary", "", narrative["executive_summary"], ""]
    if (Path(cfg["output_dir"]) / "design" / "design-report.md").exists():
        lines += ["UI design analysis (design system, components, accessibility, usability): [design/design-report.html](design/design-report.html)", ""]
    lines += ["## Coverage", "", "| Metric | Value |", "|---|---|",
              f"| Unique screens | {coverage['screens_total']} |",
              *[f"| Screens reached by {r} | {n} |" for r, n in coverage["screens_per_role"].items()],
              f"| Declared paths not reached | {len(coverage['declared_not_reached'])} |",
              f"| Actions blocked by safety policy | {coverage['blocked_actions']} |",
              f"| Errors | {coverage['errors']} |",
              f"| LLM calls / tokens | {coverage['llm_calls']} / {coverage['prompt_tokens'] + coverage['completion_tokens']} |", ""]
    comp = state.comparison or {}
    if comp:
        lines += ["## Roles and authorisation", "", comp.get("authorization_model", ""), ""]
        for p in comp.get("role_profiles", []):
            lines += [f"- **{p['role']}**: {p['summary']} Key capabilities: {', '.join(p['key_capabilities'])}."]
        lines.append("")
        if comp.get("findings"):
            lines += ["### Findings", "", "| Severity | Finding | Roles | Screens |", "|---|---|---|---|"]
            for f in comp["findings"]:
                lines.append(f"| {f['severity']} | **{_md_escape(f['title'])}**: {_md_escape(f['detail'])} | {', '.join(f['roles'])} | {_md_escape(', '.join(f['screens']))} |")
            lines.append("")
    lines += ["## Access matrix", "", "Legend: ✔ reached by exploring · ↗ allowed by direct URL only · ✖ denied · ? other", "",
              "| Screen | URL | " + " | ".join(roles) + " |", "|---|---|" + "---|" * len(roles)]
    for row in matrix:
        cells = []
        for r in roles:
            c = row[r]
            cells.append("✔" if c["crawl"] else "↗" if c["direct"] == "allowed" else "✖" if c["direct"] in ("denied", "login_required", "redirected") else "?" if c["direct"] else "")
        lines.append(f"| {_md_escape(row['screen'])} | `{row['url']}` | " + " | ".join(cells) + " |")
    lines.append("")
    if narrative:
        lines += ["## Functional areas", ""]
        for m in narrative.get("module_summaries", []):
            lines += [f"### {m['module']}", "", m["summary"], "", "Screens: " + ", ".join(m["screens"]), ""]
    lines += ["## Screen catalogue", ""]
    for s in screens:
        a = s.analysis or {}
        lines += [f"### {a.get('screen_name') or s.title}", "",
                  f"- **URL:** `{s.url_pattern}`  ",
                  f"- **Module / type:** {a.get('business_module', '')} / {a.get('screen_type', '')}  ",
                  f"- **Purpose:** {a.get('purpose', '')}  ",
                  f"- **Roles:** {', '.join(sorted(s.roles))}  ",
                  f"- **Screenshot:** [{s.screenshot}]({s.screenshot.replace(chr(92), '/')})", ""]
        if a.get("fields"):
            lines += ["| Field | Name | Type | Required | Constraints / options |", "|---|---|---|---|---|"]
            for f in a["fields"]:
                lines.append(f"| {_md_escape(f['label'])} | `{f['name']}` | {f['type']} | {'yes' if f['required'] else ''} | "
                             f"{_md_escape(f['constraints'] + ('; ' + ', '.join(f['options'][:12]) if f['options'] else ''))} |")
            lines.append("")
        if a.get("actions"):
            lines.append("Actions: " + ", ".join(f"{x['label']} ({x['kind']})" for x in a["actions"]))
        for r, entry in sorted(s.roles.items()):
            blocked = sorted({b["element"] for b in state.blocked if b["role"] == r and b["screen"] == s.sig})
            if blocked:
                lines.append(f"- {r}: available but not executed (safety): {', '.join(blocked[:15])}")
        if a.get("visible_rules"):
            lines += ["", "Rules visible on screen:", *[f"- {x}" for x in a["visible_rules"]]]
        if a.get("messages"):
            lines += ["", "Messages: " + " | ".join(a["messages"])]
        lines.append("")
    if state.probes:
        lines += ["## Validation probes", "", "| Screen | Case | Result |", "|---|---|---|"]
        for p in state.probes:
            lines.append(f"| {_md_escape(p['screen'])} | {_md_escape(p['case'])} | {_md_escape(' | '.join(p['messages'] + p['dialogs']))[:300]} |")
        lines.append("")
    dialogs = {d["text"]: d for d in state.dialogs}
    if dialogs:
        lines += ["## Dialogs observed", "", *[f"- ({d.get('role')}) {_md_escape(t)[:200]} ({'accepted' if d['accepted'] else 'dismissed'})" for t, d in dialogs.items()], ""]
    if narrative:
        if narrative.get("recommendations"):
            lines += ["## Recommendations", "", *[f"- {x}" for x in narrative["recommendations"]], ""]
        gaps = list(narrative.get("coverage_gaps", []))
    else:
        gaps = []
    gaps += [f"Declared path not reached: `{p}`" for p in coverage["declared_not_reached"][:50]]
    if gaps:
        lines += ["## Coverage gaps", "", *[f"- {g}" for g in gaps], ""]
    if state.errors:
        lines += ["## Errors", "", *[f"- {json.dumps(e)[:300]}" for e in state.errors[:50]], ""]
    if state.agent_events:
        runs: dict[str, int] = {}
        tools: dict[str, int] = {}
        for ev in state.agent_events:
            if ev["t"] == "subagent.started":
                runs[ev["agent"]] = runs.get(ev["agent"], 0) + 1
            elif ev["t"] == "tool":
                tools[f"{ev['agent']} → {ev['tool']}"] = tools.get(f"{ev['agent']} → {ev['tool']}", 0) + 1
        lines += ["## Agent activity (GitHub Copilot SDK runtime)", "",
                  "Sub-agent runs: " + ", ".join(f"{k} ×{v}" for k, v in sorted(runs.items())), "",
                  "| Agent → tool | Calls |", "|---|---|", *[f"| {k} | {v} |" for k, v in sorted(tools.items())], "",
                  "Full trace: `copilot-events.jsonl`.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML

def _html(cfg, state, screens, matrix, narrative, coverage, roles) -> str:
    e = html.escape
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>UI analysis - {e(cfg['app']['name'])}</title><style>"
             "body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;margin:8px 0}"
             "td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px;vertical-align:top}th{background:#305496;color:#fff}"
             ".ok{background:#c6efce}.hid{background:#ffeb9c}.no{background:#f8cbad}.card{border:1px solid #ddd;padding:12px;margin:12px 0}"
             "img.shot{max-width:480px;border:1px solid #999;cursor:zoom-in}h1{color:#203864}</style></head><body>",
             f"<h1>UI analysis report: {e(cfg['app']['name'])}</h1>",
             f"<p>Engine <b>{e(cfg['browser']['engine'])}</b>, safety mode <b>{e(cfg['safety']['mode'])}</b>, "
             f"{coverage['screens_total']} unique screens, roles: {e(', '.join(roles))}.</p>"]
    if narrative:
        parts.append(f"<h2>Executive summary</h2><p>{e(narrative['executive_summary'])}</p>")
    comp = state.comparison or {}
    if comp.get("findings"):
        parts.append("<h2>Findings</h2><table><tr><th>Severity</th><th>Finding</th><th>Roles</th></tr>")
        for f in comp["findings"]:
            parts.append(f"<tr><td>{e(f['severity'])}</td><td><b>{e(f['title'])}</b><br>{e(f['detail'])}</td><td>{e(', '.join(f['roles']))}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>Access matrix</h2><table><tr><th>Screen</th><th>URL</th>" + "".join(f"<th>{e(r)}</th>" for r in roles) + "</tr>")
    for row in matrix:
        cells = []
        for r in roles:
            c = row[r]
            cls, label = ("ok", "explored") if c["crawl"] else ("hid", "direct URL") if c["direct"] == "allowed" else ("no", c["direct"] or "-")
            cells.append(f"<td class='{cls}'>{e(label)}</td>")
        parts.append(f"<tr><td>{e(row['screen'])}</td><td><code>{e(row['url'])}</code></td>{''.join(cells)}</tr>")
    parts.append("</table><h2>Screen catalogue</h2>")
    for s in screens:
        a = s.analysis or {}
        parts.append(f"<div class='card'><h3>{e(a.get('screen_name') or s.title)}</h3><p><code>{e(s.url_pattern)}</code> · "
                     f"{e(a.get('business_module', ''))} · {e(a.get('screen_type', ''))} · roles: {e(', '.join(sorted(s.roles)))}</p>"
                     f"<p>{e(a.get('purpose', ''))}</p>")
        for r, entry in sorted(s.roles.items()):
            shot = entry.get("screenshot", "").replace("\\", "/")
            parts.append(f"<div><b>{e(r)}</b><br><a href='{e(shot)}' target='_blank'><img class='shot' src='{e(shot)}' loading='lazy'></a></div>")
        if a.get("fields"):
            parts.append("<table><tr><th>Field</th><th>Name</th><th>Type</th><th>Req.</th><th>Constraints / options</th></tr>")
            for f in a["fields"]:
                parts.append(f"<tr><td>{e(f['label'])}</td><td><code>{e(f['name'])}</code></td><td>{e(f['type'])}</td>"
                             f"<td>{'yes' if f['required'] else ''}</td><td>{e(f['constraints'])} {e(', '.join(f['options'][:12]))}</td></tr>")
            parts.append("</table>")
        if a.get("visible_rules"):
            parts.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in a["visible_rules"]) + "</ul>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)
