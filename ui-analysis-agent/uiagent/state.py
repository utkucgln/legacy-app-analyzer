"""Run state shared by all agents: screen catalogue, transitions, per-role progress; checkpointed to state.json."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

CARD = re.compile(r"(?<!\d)(\d{6})\d{3,9}(\d{4})(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")


def mask_text(text: str) -> str:
    text = CARD.sub(lambda m: f"{m.group(1)}******{m.group(2)}", text or "")
    text = EMAIL.sub("<email>", text)
    return PHONE.sub(lambda m: m.group(0)[:4] + "****", text)


def url_pattern(url: str, base_url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r";jsessionid=[^?/#]*", "", parts.path, flags=re.I)
    base_path = urlsplit(base_url).path.rstrip("/")
    if base_path and path.startswith(base_path):
        path = path[len(base_path):] or "/"
    names = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    return path + ("?" + "&".join(names) if names else "")


def screen_signature(obs: dict, base_url: str) -> str:
    head = (obs.get("headings") or [""])[0] or obs.get("title", "")
    heading = re.sub(r"\d+", "#", head)
    fields = sorted({e.get("name") or e.get("label") or "" for e in obs.get("elements", []) if e["kind"] == "field" and e.get("visible")})[:25]
    raw = "|".join([url_pattern(obs.get("url", ""), base_url), heading.lower(), ",".join(fields)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def condense(obs: dict, base_url: str, *, max_elements: int = 160, tables: bool = True, mask: bool = True) -> tuple[str, list[str], list[str]]:
    """Text view of a page for the agents, plus the ids of actionable and fillable elements."""
    m = mask_text if mask else (lambda s: s)
    lines = [f"URL: {url_pattern(obs.get('url', ''), base_url)}", f"Title: {m(obs.get('title', ''))}"]
    if obs.get("headings"):
        lines.append("Headings: " + " | ".join(m(h) for h in obs["headings"][:8]))
    if obs.get("messages"):
        lines.append("Messages: " + " | ".join(m(x) for x in obs["messages"][:10]))
    if obs.get("frame_urls"):
        lines.append("Frames: " + ", ".join(url_pattern(u, base_url) for u in obs["frame_urls"][:8]))
    for f in obs.get("forms", [])[:10]:
        lines.append(f"Form[{f['index']}] {f['method'].upper()} {url_pattern(f['action'], base_url)} name={f['name']}")
    lines.append("Elements (id | kind | label | details):")
    clickable, fillable = [], []
    for e in obs.get("elements", []):
        if not e.get("visible") or e["kind"] == "hidden" or e.get("disabled"):
            continue
        if len(clickable) + len(fillable) >= max_elements:
            lines.append("... (more elements truncated)")
            break
        tag = e["kind"] + (" nav" if e.get("nav") else "") + (f" form{e['form']}" if e.get("form", -1) >= 0 else "")
        if e["kind"] == "field":
            fillable.append(e["id"])
            detail = f"name={e.get('name')} type={e.get('type')}"
            if e.get("maxlength"):
                detail += f" maxlength={e['maxlength']}"
            if e.get("readonly"):
                detail += " readonly"
            if e.get("options"):
                detail += " options=" + ", ".join(f"{o['value']}={m(o['text'])}" for o in e["options"][:15])
            elif e.get("value"):
                detail += f" value={m(e['value'])!r}"
            lines.append(f"{e['id']} | {tag} | {m(e.get('label') or '')!r} | {detail}")
        else:
            clickable.append(e["id"])
            target = url_pattern(e["href"], base_url) if e.get("href") and not e["href"].lower().startswith("javascript") else (e.get("href") or "")
            extra = (f"-> {target}" if target else "") + (f" onclick={e['onclick'][:80]}" if e.get("onclick") else "")
            lines.append(f"{e['id']} | {tag} | {m(e.get('text') or '')!r} | {extra.strip()}")
    if tables:
        for t in obs.get("tables", [])[:6]:
            lines.append(f"Table: headers={[m(h) for h in t['headers']]} rows={t['rows']} sample={[[m(c) for c in r] for r in t['sample']]}")
    lines.append("Text: " + m(obs.get("text", ""))[:1500])
    return "\n".join(lines), clickable, fillable


@dataclass
class Screen:
    sig: str
    url_pattern: str
    title: str
    heading: str
    screenshot: str
    first_role: str
    sample_url: str
    analysis: dict | None = None
    roles: dict[str, dict] = field(default_factory=dict)


@dataclass
class RunState:
    started: str = ""
    screens: dict[str, Screen] = field(default_factory=dict)
    transitions: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    dialogs: list[dict] = field(default_factory=list)
    plans: dict[str, dict] = field(default_factory=dict)
    role_done: dict[str, bool] = field(default_factory=dict)
    access: dict[str, dict[str, dict]] = field(default_factory=dict)
    probes: list[dict] = field(default_factory=list)
    comparison: dict | None = None
    sessions: list[str] = field(default_factory=list)
    known_values: dict[str, list[str]] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    agent_events: list[dict] = field(default_factory=list)
    design_raw: dict[str, dict] = field(default_factory=dict)
    design_audits: dict[str, dict] = field(default_factory=dict)
    design_system: dict | None = None

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "RunState":
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        data["screens"] = {k: Screen(**v) for k, v in data.get("screens", {}).items()}
        return cls(**data)

    def remember_values(self, obs: dict) -> None:
        """Keep identifier-like values seen in tables so the Navigator can use them in search forms."""
        for t in obs.get("tables", []):
            for row in t.get("sample", []):
                for cell, header in zip(row, t.get("headers") or [""] * len(row)):
                    if re.fullmatch(r"[A-Z]{2,5}[-/]?\d{3,}|\d{5,12}", cell or "") and not CARD.search(cell):
                        bucket = self.known_values.setdefault(header or "id", [])
                        if cell not in bucket and len(bucket) < 8:
                            bucket.append(cell)
