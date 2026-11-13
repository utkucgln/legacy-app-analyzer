"""AI code analysis for modernisation over decompiled or original sources, driven by the GitHub Copilot SDK runtime.

Pipeline
  1. workspace  Build a sanitised, read-only analysis workspace either from the Legacy Decompiler output (primary decompiler,
                alternative versions of low-fidelity classes, unpacked descriptors/JSPs, deterministic extractions) or from the
                original source tree once it is available. Secrets are masked.
  2. agents     Copilot sessions with custom agents (architect, component-analyst, cross-cutting-reviewer, modernization-planner).
                Built-in tools are limited to view/grep/glob; a pre-tool hook confines every path to the workspace.
  3. record     Agents report through record_* tools. Every quoted code excerpt is verified against the cited file (whitespace
                insensitive) and line numbers are computed locally; unverifiable evidence is rejected back to the agent.
  4. outputs    components/rules/findings/architecture/plan JSON, a modernisation report and facts.json.
Inference uses the configured Azure OpenAI deployment (BYOK, API key); nothing is sent to GitHub's model service.
"""
from __future__ import annotations

import asyncio
import csv
import html
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from code_analyst.common import RULE_KINDS, FactSink, Timer, api_key, ensure_dir, mask, sanitize_source, write_json

log = logging.getLogger("code_analyst")
READ_TOOLS = ["view", "grep", "glob"]
AGENT_READ_TOOLS = ["view", "grep", "rg", "glob"]
TEXT_EXT = {".java", ".jsp", ".jspf", ".xml", ".properties", ".tld", ".js", ".html", ".htm", ".sql", ".txt", ".json", ".csv", ".md"}
EVIDENCE_FILES = ["actions.json", "webapp.json", "entrypoints.csv", "deps.csv", "sql-strings.csv", "integration-strings.csv", "libraries.csv",
                  "validation-rules.json", "orm-map.json", "jsp-inventory.json", "templates.json", "enums.json", "messages.json",
                  "dead-code.csv", "class-versions.csv"]
SECRET_PROP = re.compile(r"^(\s*[\w.\-]*(?:password|passwd|pwd|secret|token|apikey|api[_.-]?key|credential|private[_.-]?key)[\w.\-]*\s*[=:]\s*)(.+)$",
                         re.I | re.M)
SECRET_XML = re.compile(r"((?:password|passwd|pwd|secret|token|apikey|credential)[\w.\-]*\"?\s*(?:>|=\s*\"))([^<\"]+)", re.I)
DEFAULT_TARGET = ("Java 21 LTS, Spring Boot 3 (Spring MVC REST + Spring Data JPA + Spring Batch/Scheduler), a React single-page front end, "
                  "containerised on Azure (Container Apps or AKS) with Azure SQL or PostgreSQL, Entra ID authentication")

STRATEGIES = ["retain", "rehost", "replatform", "refactor", "rearchitect", "rebuild", "replace", "retire"]
LAYERS = ["presentation", "controller", "service", "domain", "data_access", "integration", "batch", "security", "configuration", "utility", "other"]
CATEGORIES = ["security", "reliability", "performance", "maintainability", "data_integrity", "compliance", "obsolete_technology", "decompilation"]
SEVERITIES = ["low", "medium", "high", "critical"]
TRANSIENT = re.compile(r"dns error|ENOTFOUND|ECONNRESET|ETIMEDOUT|ECONNREFUSED|connection (reset|refused|closed)|timed? ?out|"
                       r"\b(429|500|502|503|504)\b|rate limit|temporarily|overloaded|socket hang up", re.I)


# ================================================================ JSON schema helpers

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props), "additionalProperties": False}


def _arr(item: dict) -> dict:
    return {"type": "array", "items": item}


S = {"type": "string"}
I = {"type": "integer"}


def _enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


# ================================================================ workspace

def _sanitize(path: Path, text: str) -> str:
    if path.suffix in (".properties", ".xml", ".jsp", ".jspf"):
        text = SECRET_PROP.sub(lambda m: m.group(1) + mask(m.group(2).strip()), text)
        text = SECRET_XML.sub(lambda m: m.group(1) + mask(m.group(2)), text)
    return sanitize_source(text)


def _copy_text(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(_sanitize(src, src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


def _fidelity(code_dir: Path) -> tuple[str, dict[str, dict]]:
    dec = code_dir / "decompiled"
    summary = dec / "fidelity-summary.json"
    primary = json.loads(summary.read_text(encoding="utf-8")).get("recommended_primary") if summary.exists() else None
    primary = primary or next((t for t in ("vineflower", "procyon", "cfr") if (dec / t).exists()), None)
    if not primary:
        raise SystemExit(f"no decompiled sources under {dec} - run `legacy-decompiler run` with decompile enabled first")
    per_class: dict[str, dict] = defaultdict(dict)
    agreement = dec / "fidelity-agreement.csv"
    if agreement.exists():
        with agreement.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                per_class[r["class"]]["agreement"] = float(r["min_similarity"] or 0)
    recompile = dec / "fidelity-recompile.csv"
    if recompile.exists():
        with recompile.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                cls = r["file"].removesuffix(".java").replace("/", ".")
                per_class[cls].setdefault("recompile", {})[r["tool"]] = r["result"]
    markers = dec / "fidelity-error-markers.csv"
    if markers.exists():
        with markers.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                per_class[r["class"]].setdefault("error_markers", {})[r["tool"]] = int(r["error_markers"] or 0)
    return primary, per_class


def build_workspace(code_dir: Path, ws: Path, alt_threshold: float) -> dict:
    """Sanitised copy of what an analyst may read. The original source tree is never part of it."""
    if ws.exists():
        shutil.rmtree(ws)
    ensure_dir(ws)
    primary, fidelity = _fidelity(code_dir)
    src_root = code_dir / "decompiled" / primary
    classes: dict[str, dict] = {}
    for f in sorted(src_root.rglob("*.java")):
        rel = f.relative_to(src_root)
        _copy_text(f, ws / "src" / rel)
        cls = ".".join(rel.with_suffix("").parts)
        text = f.read_text(encoding="utf-8", errors="replace")
        classes[cls] = {"file": f"src/{rel.as_posix()}", "lines": text.count("\n") + 1, "package": ".".join(rel.parts[:-1]),
                        **fidelity.get(cls, {})}
    flagged = []
    for cls, info in classes.items():
        low = info.get("agreement", 1.0) < alt_threshold or (info.get("recompile", {}).get(primary) not in (None, "OK"))
        if not low:
            continue
        info["alternatives"] = []
        for tool in ("vineflower", "cfr", "procyon"):
            alt = code_dir / "decompiled" / tool / Path(*cls.split(".")).with_suffix(".java")
            if tool != primary and alt.exists():
                _copy_text(alt, ws / "alt" / tool / alt.relative_to(code_dir / "decompiled" / tool))
                info["alternatives"].append(f"alt/{tool}/{alt.relative_to(code_dir / 'decompiled' / tool).as_posix()}")
        flagged.append(cls)
    unpacked = code_dir / "unpacked"
    web_files = 0
    if unpacked.exists():
        for f in sorted(unpacked.rglob("*")):
            rel = f.relative_to(unpacked)
            if not f.is_file() or f.suffix.lower() not in TEXT_EXT or "lib" in rel.parts[:2] or f.stat().st_size > 400_000:
                continue
            _copy_text(f, ws / "web" / rel)
            web_files += 1
    for name in EVIDENCE_FILES:
        if (code_dir / name).exists():
            _copy_text(code_dir / name, ws / "evidence" / name)
    by_pkg: dict[str, list[str]] = defaultdict(list)
    for cls, info in classes.items():
        by_pkg[info["package"]].append(cls)
    index = ["# Analysis workspace", "",
             f"Decompiled Java sources ({primary}, recommended by round-trip fidelity) are under `src/`. They are reconstructions: names of",
             "local variables, comments and some control flow may differ from the original. Alternative decompilations of classes where",
             f"the decompilers disagree (similarity < {alt_threshold}) or the primary output did not recompile are under `alt/<tool>/`.",
             "`web/` holds deployment descriptors, configuration and JSPs from the deployable; `evidence/` holds deterministic extractions",
             "(Struts actions, servlet mappings, SQL strings, integration strings, package dependencies, libraries). Secrets are masked.", "",
             "## Packages", "", *[f"- {p or '(default)'}: {len(c)} classes" for p, c in sorted(by_pkg.items())], "",
             "## Low-fidelity classes", "", *([f"- {c}: agreement {classes[c].get('agreement', '?')}, alternatives {', '.join(classes[c]['alternatives'])}"
                                                for c in flagged] or ["- none"])]
    (ws / "INDEX.md").write_text("\n".join(index), encoding="utf-8")
    meta = {"primary": primary, "classes": classes, "flagged": flagged, "web_files": web_files, "packages": {p: sorted(c) for p, c in by_pkg.items()}}
    write_json(ws.parent / "workspace.json", meta)
    return meta


PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)


def build_source_workspace(source_dirs: list[Path], web_dirs: list[Path], evidence_dir: Path | None, ws: Path) -> dict:
    """Workspace from the original source tree. Files are placed by their package declaration, so any layout works."""
    if ws.exists():
        shutil.rmtree(ws)
    ensure_dir(ws)
    classes: dict[str, dict] = {}
    for root in source_dirs:
        if not root.is_dir():
            raise SystemExit(f"source directory {root} does not exist")
        for f in sorted(root.rglob("*.java")):
            text = f.read_text(encoding="utf-8", errors="replace")
            m = PACKAGE_RE.search(text)
            pkg = m.group(1) if m else ""
            cls = f"{pkg}.{f.stem}" if pkg else f.stem
            if cls in classes:
                log.warning("duplicate class %s in %s - first copy kept", cls, f)
                continue
            rel = Path(*pkg.split("."), f.name) if pkg else Path(f.name)
            _copy_text(f, ws / "src" / rel)
            classes[cls] = {"file": f"src/{rel.as_posix()}", "lines": text.count("\n") + 1, "package": pkg}
    if not classes:
        raise SystemExit("no .java files found in input.source_dirs")
    web_files = 0
    for root in web_dirs:
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in TEXT_EXT and f.suffix.lower() != ".java" and f.stat().st_size <= 400_000:
                _copy_text(f, ws / "web" / f.relative_to(root))
                web_files += 1
    if evidence_dir:
        for name in EVIDENCE_FILES:
            if (evidence_dir / name).exists():
                _copy_text(evidence_dir / name, ws / "evidence" / name)
    by_pkg: dict[str, list[str]] = defaultdict(list)
    for cls, info in classes.items():
        by_pkg[info["package"]].append(cls)
    index = ["# Analysis workspace", "",
             "Original Java sources are under `src/` (arranged by package). `web/` holds web content, descriptors and configuration;",
             "`evidence/` (when present) holds deterministic extractions from the deployed artifact (Struts actions, servlet mappings,",
             "SQL strings, integration strings, package dependencies, libraries). Secrets are masked.", "",
             "## Packages", "", *[f"- {p or '(default)'}: {len(c)} classes" for p, c in sorted(by_pkg.items())]]
    (ws / "INDEX.md").write_text("\n".join(index), encoding="utf-8")
    meta = {"primary": "source", "classes": classes, "flagged": [], "web_files": web_files, "packages": {p: sorted(c) for p, c in by_pkg.items()}}
    write_json(ws.parent / "workspace.json", meta)
    return meta


def components_from(meta: dict, max_classes: int) -> list[dict]:
    """One component per package; large packages are split so one agent run stays within a focused context."""
    comps = []
    for pkg, classes in sorted(meta["packages"].items()):
        for i in range(0, len(classes), max_classes):
            part = classes[i:i + max_classes]
            name = pkg + (f"#{i // max_classes + 1}" if len(classes) > max_classes else "")
            comps.append({"component": name, "package": pkg, "classes": part,
                          "lines": sum(meta["classes"][c]["lines"] for c in part)})
    return comps


# ================================================================ evidence verification

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


class Verifier:
    def __init__(self, ws: Path) -> None:
        self.ws = ws.resolve()
        self._cache: dict[str, str] = {}

    def inside(self, raw: str) -> Path | None:
        if not raw:
            return None
        p = Path(raw)
        p = (p if p.is_absolute() else self.ws / p).resolve()
        return p if p == self.ws or self.ws in p.parents else None

    def locate(self, file: str, quote: str) -> tuple[str, int] | str:
        """Return (workspace-relative file, 1-based line) for a verbatim quote, or an error message for the agent."""
        path = self.inside(file.replace("\\", "/"))
        if path is None or not path.is_file():
            return f"file '{file}' is not in the analysis workspace; use a path such as src/com/x/Y.java"
        rel = path.relative_to(self.ws).as_posix()
        text = self._cache.setdefault(rel, path.read_text(encoding="utf-8", errors="replace"))
        q = _norm(quote)
        if len(q) < 8:
            return "quote is too short; copy at least one full statement verbatim from the file"
        flat, offsets = [], []
        for i, ch in enumerate(text):
            if not ch.isspace():
                flat.append(ch)
                offsets.append(i)
        pos = "".join(flat).find(q)
        if pos < 0:
            return f"quote not found verbatim in {rel}; view the file again and copy the exact code (whitespace may differ)"
        return rel, text.count("\n", 0, offsets[pos]) + 1


# ================================================================ Copilot runtime

class CodeAnalysis:
    def __init__(self, cfg: dict, out: Path) -> None:
        self.cfg = cfg
        self.ai = cfg.get("ai", {})
        self.inputs = cfg.get("input", {})
        self.c = {"target_stack": DEFAULT_TARGET, "max_classes_per_component": 25, "max_components": 60, "turn_timeout_s": 2400,
                  "idle_timeout_s": 300, "max_error_retries": 5, "session_attempts": 2,
                  "alt_similarity_threshold": 0.8, "max_model_calls": 2500, "reasoning_effort": None, "wire_api": "responses",
                  "cli_path": None, "runtime_telemetry": False, **cfg.get("analysis", {})}
        self.from_source = bool(self.inputs.get("source_dirs"))
        self.out = ensure_dir(out)
        self.ws = out / "workspace"
        self.state_path = out / "state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        for key, default in (("components", {}), ("rules", []), ("findings", []), ("architecture", None), ("plan", None),
                             ("done", []), ("rejected", 0), ("usage", {"calls": 0, "input_tokens": 0, "output_tokens": 0}), ("events", [])):
            self.state.setdefault(key, default)
        self.verifier: Verifier | None = None
        self.phase = ""
        self._last_event = time.monotonic()
        self._error_retries = 0

    def save(self) -> None:
        write_json(self.state_path, self.state)

    # ---------------------------------------------------------------- endpoint & client
    def _endpoint_model(self) -> tuple[str, str]:
        endpoint, model = self.ai.get("endpoint", ""), self.ai.get("model", "")
        if not endpoint or not model:
            raise SystemExit("Azure OpenAI endpoint and deployment are required (ai.endpoint/ai.model, AZURE_OPENAI_ENDPOINT/"
                             "AZURE_OPENAI_DEPLOYMENT, or the prompt)")
        if not api_key(self.ai):
            raise SystemExit(f"set the Azure OpenAI API key in the environment variable {self.ai.get('api_key_env', 'AZURE_OPENAI_API_KEY')}")
        return endpoint, model

    def _provider(self, endpoint: str) -> dict:
        return {"type": "openai", "base_url": endpoint + "/openai/v1/", "wire_api": self.c["wire_api"], "api_key": api_key(self.ai)}

    def _client(self):
        from copilot import CopilotClient, RuntimeConnection

        path = self.c.get("cli_path")
        conn = RuntimeConnection.for_stdio(path=path) if path else RuntimeConnection.for_stdio()
        env = dict(os.environ)
        if not self.c.get("runtime_telemetry"):
            env["COPILOT_TELEMETRY_DISABLED"] = "true"
        if path:
            env["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
        conn.env = env
        return CopilotClient(mode="empty", base_directory=str(self.out / ".copilot"), connection=conn)

    def ping(self) -> str:
        """Preflight: start the Copilot runtime and get one reply from the configured deployment (no tools)."""
        from copilot import ToolSet
        from copilot.session import PermissionHandler

        endpoint, model = self._endpoint_model()

        async def go() -> str:
            async with self._client() as client:
                session = await client.create_session(
                    model=model, available_tools=ToolSet().add_custom("none"), on_permission_request=PermissionHandler.approve_all,
                    provider=self._provider(endpoint))
                ev = await session.send_and_wait("Reply with the single word OK.", timeout=180)
                await session.disconnect()
                return ev.data.content.strip() if ev else ""

        return asyncio.run(go())

    # ---------------------------------------------------------------- hook, events
    async def _guard(self, inp: dict, _invocation) -> dict:
        name = inp.get("toolName", "")
        args = inp.get("toolArgs") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"raw": args}
        if name in ("view", "grep", "rg", "glob"):
            for key, value in args.items():
                values = value if isinstance(value, list) else [value]
                for v in values:
                    if not isinstance(v, str):
                        continue
                    if key in ("path", "paths", "file", "dir", "directory", "cwd") and self.verifier.inside(v) is None:
                        return {"permissionDecision": "deny", "permissionDecisionReason": f"'{v}' is outside the analysis workspace; use relative paths"}
                    if key in ("pattern", "glob") and name == "glob" and (".." in v or re.match(r"^[a-zA-Z]:|^/", v)):
                        return {"permissionDecision": "deny", "permissionDecisionReason": "glob patterns must be relative to the workspace"}
        if self.state["usage"]["calls"] >= int(self.c["max_model_calls"]):
            return {"permissionDecision": "deny", "permissionDecisionReason": "model call budget exhausted; record what you have and stop"}
        return {"permissionDecision": "allow"}

    async def _on_error(self, inp: dict, _invocation) -> dict:
        """Retry transient model/network errors with backoff; the runtime otherwise stalls after a failed model request."""
        err = inp.get("error")
        text = err.get("message", "") if isinstance(err, dict) else str(err or "")
        self._last_event = time.monotonic()
        self.state["events"].append({"ts": time.strftime("%H:%M:%S"), "phase": self.phase, "t": "error",
                                     "agent": inp.get("errorContext", ""), "error": text[:300]})
        if TRANSIENT.search(text) and self._error_retries < int(self.c["max_error_retries"]):
            self._error_retries += 1
            delay = min(60, 5 * 2 ** (self._error_retries - 1))
            log.warning("code-ai: transient error in %s (%s); retry %d in %ds", self.phase, text[:120], self._error_retries, delay)
            await asyncio.sleep(delay)
            self._last_event = time.monotonic()
            return {"errorHandling": "retry", "retryCount": 1}
        log.error("code-ai: error in %s: %s", self.phase, text[:300])
        return {"errorHandling": "abort"}

    def _on_event(self, e) -> None:
        self._last_event = time.monotonic()
        try:
            kind = e.type.value if hasattr(e.type, "value") else str(e.type)
            d = e.data
            if kind == "assistant.usage":
                u = self.state["usage"]
                u["calls"] += 1
                u["input_tokens"] += getattr(d, "input_tokens", None) or 0
                u["output_tokens"] += getattr(d, "output_tokens", None) or 0
            elif kind in ("subagent.started", "subagent.completed", "subagent.failed", "session.error"):
                self.state["events"].append({"ts": time.strftime("%H:%M:%S"), "phase": self.phase, "t": kind,
                                             "agent": getattr(d, "agent_name", ""), "error": str(getattr(d, "message", getattr(d, "error", "")) or "")[:300]})
            elif kind == "tool.execution_start":
                tool = getattr(d, "tool_name", "")
                counts = self.state.setdefault("tool_counts", {})
                counts[tool] = counts.get(tool, 0) + 1
        except Exception:  # noqa: BLE001 - telemetry must never break the session
            pass

    # ---------------------------------------------------------------- record tools
    def _tools(self, component: str | None) -> list:
        from copilot.tools import Tool, ToolResult

        def wrap(fn: Callable[[dict], str]):
            async def run(invocation) -> Any:
                args = invocation.arguments or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                try:
                    return ToolResult(text_result_for_llm=fn(args))
                except (ValueError, KeyError, TypeError) as exc:
                    self.state["rejected"] += 1
                    msg = f"missing field {exc}" if isinstance(exc, KeyError) else str(exc)
                    return ToolResult(text_result_for_llm=f"REJECTED: {msg}", result_type="failure", error=msg)
            return run

        evidence_props = {"file": S, "quote": S}
        rule_schema = _obj({"class_name": S, "method": S, "kind": _enum(RULE_KINDS), "statement": S, "values": _arr(S), **evidence_props,
                            "certainty": _enum(["explicit", "inferred"]), "modernization_note": S})
        finding_schema = _obj({"category": _enum(CATEGORIES), "severity": _enum(SEVERITIES), "title": S, "detail": S, **evidence_props,
                               "cwe": S, "recommendation": S})
        component_schema = _obj({
            "summary": S, "layer": _enum(LAYERS), "responsibilities": _arr(S), "business_capabilities": _arr(S),
            "classes": _arr(_obj({"class_name": S, "role": S, "target_mapping": S})),
            "entry_points": _arr(S), "data_access": _obj({"tables": _arr(S), "access_style": S, "transactions": S}),
            "integrations": _arr(_obj({"system": S, "protocol": S, "direction": _enum(["outbound", "inbound", "both"]), "notes": S})),
            "depends_on": _arr(S), "state_and_concurrency": S,
            "modernization": _obj({"strategy": _enum(STRATEGIES), "target_design": S, "complexity": _enum(["low", "medium", "high", "very_high"]),
                                   "effort": _enum(["S", "M", "L", "XL"]), "blockers": _arr(S), "reusable_logic": S,
                                   "behaviour_to_preserve": _arr(S), "tests_to_write": _arr(S)}),
            "decompilation_caveats": S})
        arch_schema = _obj({
            "overview": S, "architecture_style": S,
            "layers": _arr(_obj({"layer": _enum(LAYERS), "packages": _arr(S), "notes": S})),
            "frameworks": _arr(_obj({"name": S, "version": S, "role": S, "lifecycle": _enum(["current", "outdated", "end_of_life", "unknown"]),
                                     "modern_replacement": S})),
            "entry_points": S, "data_stores": _arr(S),
            "external_systems": _arr(_obj({"name": S, "protocol": S, "packages": _arr(S)})),
            "cross_cutting": _arr(_obj({"concern": S, "current_implementation": S, "target_implementation": S})),
            "technical_debt": S})
        plan_schema = _obj({
            "target_architecture": S,
            "bounded_contexts": _arr(_obj({"name": S, "components": _arr(S), "responsibilities": S, "data_owned": _arr(S), "api_surface": _arr(S)})),
            "migration_waves": _arr(_obj({"wave": I, "name": S, "components": _arr(S), "rationale": S, "prerequisites": _arr(S), "exit_criteria": _arr(S)})),
            "decisions": _arr(_obj({"topic": S, "decision": S, "rationale": S})),
            "risks": _arr(_obj({"risk": S, "impact": _enum(["low", "medium", "high"]), "mitigation": S})),
            "quick_wins": _arr(S), "effort_summary": S})

        def rec_rule(a: dict) -> str:
            got = self.verifier.locate(a["file"], a["quote"])
            if isinstance(got, str):
                raise ValueError(got)
            missing = [v for v in a.get("values", []) if v.strip() and _norm(v) not in _norm(self.verifier._cache[got[0]]) and
                       _norm(v.replace(",", "")) not in _norm(self.verifier._cache[got[0]])]
            self.state["rules"].append({**a, "component": component, "file": got[0], "line": got[1], "values_verified": not missing})
            return f"recorded rule at {got[0]}:{got[1]}" + (f" (values not found literally: {missing}; resolve constants if possible)" if missing else "")

        def rec_finding(a: dict) -> str:
            line, rel = None, a.get("file", "")
            if a.get("file") or a.get("quote"):
                got = self.verifier.locate(a.get("file", ""), a.get("quote", ""))
                if isinstance(got, str):
                    raise ValueError(got)
                rel, line = got
            self.state["findings"].append({**a, "component": component, "file": rel, "line": line, "phase": self.phase})
            return f"recorded finding '{a['title'][:60]}'" + (f" at {rel}:{line}" if line else "")

        def rec_component(a: dict) -> str:
            if component is None:
                raise ValueError("record_component is only available in component analysis")
            self.state["components"][component] = a
            return f"recorded component {component}"

        def rec_arch(a: dict) -> str:
            self.state["architecture"] = a
            return "recorded architecture"

        def rec_plan(a: dict) -> str:
            known = self.state["components"]
            alias: dict[str, str] = {}
            for comp, rec in known.items():
                for name in [comp, *(c.get("class_name", "") for c in rec.get("classes", []))]:
                    if name:
                        alias.setdefault(name.lower(), comp)
                        alias.setdefault(name.rsplit(".", 1)[-1].lower(), comp)
            unknown: list[str] = []
            for b in a["bounded_contexts"]:
                resolved = [alias.get(n.strip().lower()) for n in b["components"]]
                unknown += [n for n, r in zip(b["components"], resolved) if r is None]
                b["components"] = list(dict.fromkeys(r for r in resolved if r))
            for w in a["migration_waves"]:
                resolved = [alias.get(n.strip().lower()) for n in w["components"]]
                unknown += [n for n, r in zip(w["components"], resolved) if r is None]
                w["components"] = list(dict.fromkeys(r for r in resolved if r))
            if not any(b["components"] for b in a["bounded_contexts"]) or len(set(unknown)) > len(known) // 2:
                raise ValueError(f"components must be component names from analysis_summary ({sorted(known)}); unknown: {sorted(set(unknown))[:10]}")
            a["unresolved_components"] = sorted(set(unknown))
            self.state["plan"] = a
            return "recorded modernisation plan" + (f" (ignored unknown names: {a['unresolved_components'][:10]})" if unknown else "")

        def summary_tool(_a: dict) -> str:
            return json.dumps({"architecture": self.state["architecture"],
                               "components": {k: {"summary": v["summary"], "layer": v["layer"], "capabilities": v["business_capabilities"],
                                                  "tables": v["data_access"]["tables"], "integrations": [i["system"] for i in v["integrations"]],
                                                  "depends_on": v["depends_on"], "modernization": v["modernization"]}
                                              for k, v in self.state["components"].items()},
                               "rules_per_component": {k: sum(1 for r in self.state["rules"] if r["component"] == k) for k in self.state["components"]},
                               "findings": [{k: f[k] for k in ("category", "severity", "title", "component")} for f in self.state["findings"]]},
                              ensure_ascii=False)[:120000]

        none = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            Tool(name="record_rule", description="Record one business rule implemented in code, with a verbatim quote from the cited workspace file.",
                 parameters=rule_schema, handler=wrap(rec_rule), skip_permission=True),
            Tool(name="record_finding", description="Record one risk or issue (security, reliability, obsolete technology, ...) with verbatim evidence.",
                 parameters=finding_schema, handler=wrap(rec_finding), skip_permission=True),
            Tool(name="record_component", description="Record the analysis and modernisation assessment of the component under review (once, at the end).",
                 parameters=component_schema, handler=wrap(rec_component), skip_permission=True),
            Tool(name="record_architecture", description="Record the system-level architecture overview.", parameters=arch_schema,
                 handler=wrap(rec_arch), skip_permission=True),
            Tool(name="record_plan", description="Record the target architecture and migration plan.", parameters=plan_schema,
                 handler=wrap(rec_plan), skip_permission=True),
            Tool(name="analysis_summary", description="Recorded architecture, component assessments and findings so far.", parameters=none,
                 handler=wrap(summary_tool), skip_permission=True),
        ]

    # ---------------------------------------------------------------- agents
    def _agents(self) -> list[dict]:
        target = self.c["target_stack"]
        origin = ("from its original source code in a read-only workspace (see INDEX.md). " if self.from_source else
                  "from DECOMPILED code in a read-only workspace (see INDEX.md). Decompiled code may contain artefacts (synthetic "
                  "variables, odd loops, missing generics); when a class has files under alt/, compare versions before concluding. ")
        base = ("You analyse a legacy Java web application " + origin +
                "Never invent classes, values or behaviour; read the files. Quotes passed to record_* tools must be copied "
                "verbatim from the cited file; if a record call is REJECTED, re-read the file and fix the quote. Use relative paths only. "
                f"The application will be modernised to: {target}. ")
        return [
            {"name": "architect", "display_name": "Architect", "tools": [*AGENT_READ_TOOLS, "record_architecture", "record_finding"],
             "description": "Maps the overall architecture, frameworks, layers, entry points, external systems and cross-cutting concerns.",
             "prompt": base + ("Start with INDEX.md, web/WEB-INF descriptors, evidence/actions.json, evidence/deps.csv and evidence/libraries.csv, "
                               "then sample representative classes per package. Identify framework versions and their lifecycle status. "
                               "Record obsolete-technology findings with evidence (e.g. a descriptor DOCTYPE or library version line). "
                               "Finish with exactly one record_architecture call.")},
            {"name": "component-analyst", "display_name": "Component Analyst",
             "tools": [*AGENT_READ_TOOLS, "record_rule", "record_finding", "record_component"],
             "description": "Reads every class of one component and documents behaviour, rules, data access, integrations and modernisation.",
             "prompt": base + ("Read EVERY class file of the component you are given in full (view), plus the descriptors, JSPs and evidence rows "
                               "that reference it (grep). Record each business rule (validations, thresholds, calculations, state transitions, "
                               "authorisation checks, schedules, special cases for specific values, retries) with record_rule; resolve constants "
                               "to values. Record security, reliability, data-integrity and obsolete-technology issues with record_finding "
                               "(include a CWE id for security issues). Map each class to its target counterpart in the target stack. "
                               "Finish with exactly one record_component call, then reply DONE.")},
            {"name": "cross-cutting-reviewer", "display_name": "Cross-cutting Reviewer",
             "tools": [*AGENT_READ_TOOLS, "record_finding", "analysis_summary"],
             "description": "Reviews configuration, security, transactions, error handling and deployment concerns across the codebase.",
             "prompt": base + ("Review across all packages and web/: authentication and session handling, authorisation enforcement, input "
                               "validation and output encoding in JSPs, SQL construction, secrets and configuration, transaction boundaries, "
                               "exception handling and logging, concurrency in batch/scheduler code, integration error handling and retries. "
                               "Call analysis_summary first and record only issues not already recorded. Reply DONE when finished.")},
            {"name": "modernization-planner", "display_name": "Modernization Planner", "tools": ["analysis_summary", "record_plan", "view", "glob"],
             "description": "Designs the target architecture, bounded contexts and migration waves from the recorded analysis.",
             "prompt": base + ("Call analysis_summary. Design the target architecture in the target stack: bounded contexts (use the recorded "
                               "component names), data ownership, API surface, migration waves using a strangler-fig approach (lowest risk and "
                               "most reusable first, shared infrastructure early), key decisions, risks and quick wins. Ground every statement "
                               "in the recorded analysis. Finish with exactly one record_plan call.")},
        ]

    async def _session(self, client, agent: str, prompt: str, component: str | None = None) -> str:
        from copilot import ToolSet
        from copilot.session import PermissionHandler

        endpoint, model = self._endpoint_model()

        kw: dict[str, Any] = dict(
            model=model, working_directory=str(self.ws), provider=self._provider(endpoint),
            tools=self._tools(component), custom_agents=self._agents(), agent=agent,
            available_tools=ToolSet().add_builtin(READ_TOOLS).add_custom("*"),
            hooks={"on_pre_tool_use": self._guard, "on_error_occurred": self._on_error},
            on_permission_request=PermissionHandler.approve_all, on_event=self._on_event)
        if self.c.get("reasoning_effort"):
            kw["reasoning_effort"] = self.c["reasoning_effort"]
        session = await client.create_session(**kw)
        self._last_event = time.monotonic()
        self._error_retries = 0
        turn = asyncio.ensure_future(session.send_and_wait(prompt, timeout=float(self.c["turn_timeout_s"])))
        idle_limit = float(self.c["idle_timeout_s"])
        try:
            while not turn.done():
                await asyncio.wait({turn}, timeout=15)
                if not turn.done() and time.monotonic() - self._last_event > idle_limit:
                    turn.cancel()
                    try:
                        await session.abort()
                    except Exception:  # noqa: BLE001 - session may already be dead
                        pass
                    raise TimeoutError(f"no session activity for {idle_limit:.0f}s")
            ev = turn.result()
            return (ev.data.content or "") if ev else ""
        finally:
            if not turn.done():
                turn.cancel()
            try:
                await session.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _attempt(self, client, agent: str, prompt: str, component: str | None = None) -> str:
        """Run one agent session, retrying with a fresh session after a stall or failure; never raises."""
        attempts = int(self.c["session_attempts"])
        for attempt in range(1, attempts + 1):
            try:
                return await self._session(client, agent, prompt, component)
            except Exception as exc:  # noqa: BLE001 - one failed phase must not abort the run
                log.warning("code-ai: %s attempt %d/%d failed: %s", self.phase, attempt, attempts, str(exc)[:200])
                self.state["events"].append({"ts": time.strftime("%H:%M:%S"), "phase": self.phase, "t": "session.failed",
                                             "agent": agent, "error": str(exc)[:300]})
                self.save()
        return ""

    # ---------------------------------------------------------------- phases
    def _component_prompt(self, comp: dict, meta: dict) -> str:
        files = [meta["classes"][c]["file"] for c in comp["classes"]]
        caveats = [f"{c}: agreement {meta['classes'][c].get('agreement')}, alternatives {meta['classes'][c].get('alternatives')}"
                   for c in comp["classes"] if c in meta["flagged"]]
        return (f"Analyse component `{comp['component']}` ({len(files)} classes, {comp['lines']} lines).\nFiles:\n" + "\n".join(f"- {f}" for f in files)
                + ("\nLow-fidelity classes (compare with alt/):\n" + "\n".join(f"- {c}" for c in caveats) if caveats else "")
                + "\nAlso grep evidence/ and web/ for these class names (Struts action mappings, SQL strings, integration strings, JSPs).")

    async def _run_async(self, only: set[str]) -> None:
        meta = json.loads((self.out / "workspace.json").read_text(encoding="utf-8"))
        comps = components_from(meta, int(self.c["max_classes_per_component"]))[: int(self.c["max_components"])]
        if only:
            comps = [c for c in comps if c["component"] in only or c["package"] in only]
        async with self._client() as client:
            if "architecture" not in self.state["done"]:
                self.phase = "architecture"
                log.info("code-ai: architecture")
                await self._attempt(client, "architect", "Map the architecture of this application.")
                if self.state["architecture"]:
                    self.state["done"].append("architecture")
                self.save()
            for comp in comps:
                key = f"component:{comp['component']}"
                if key in self.state["done"]:
                    continue
                self.phase = key
                started = time.monotonic()
                prompt = self._component_prompt(comp, meta)
                await self._attempt(client, "component-analyst", prompt, comp["component"])
                if comp["component"] not in self.state["components"]:
                    await self._attempt(client, "component-analyst", prompt + "\nYou must finish with record_component.", comp["component"])
                if comp["component"] in self.state["components"]:
                    self.state["done"].append(key)
                log.info("code-ai: %s %s in %.0fs (%d rules, %d findings so far)", comp["component"],
                         "ok" if key in self.state["done"] else "INCOMPLETE", time.monotonic() - started,
                         len(self.state["rules"]), len(self.state["findings"]))
                self.save()
                if self.state["usage"]["calls"] >= int(self.c["max_model_calls"]):
                    log.warning("code-ai: model call budget reached")
                    return
            if "cross-cutting" not in self.state["done"] and not only:
                self.phase = "cross-cutting"
                log.info("code-ai: cross-cutting review")
                if await self._attempt(client, "cross-cutting-reviewer", "Review cross-cutting concerns across the whole application."):
                    self.state["done"].append("cross-cutting")
                self.save()
            if "plan" not in self.state["done"] and not only:
                self.phase = "plan"
                log.info("code-ai: modernisation plan")
                await self._attempt(client, "modernization-planner", "Produce the modernisation plan.")
                if not self.state["plan"]:
                    await self._attempt(client, "modernization-planner",
                                        "Produce the modernisation plan. You must finish with record_plan; components fields take component "
                                        "names exactly as returned by analysis_summary, not class names. If REJECTED, fix and call it again.")
                if self.state["plan"]:
                    self.state["done"].append("plan")
                self.save()

    def run(self, prepare_only: bool = False, only: set[str] | None = None, fresh: bool = False) -> dict:
        timer = Timer()
        if fresh and self.state_path.exists():
            self.state_path.unlink()
            self.__init__(self.cfg, self.out)
        if self.from_source:
            ev = self.inputs.get("decompiler_output")
            meta = build_source_workspace([Path(p) for p in self.inputs["source_dirs"]], [Path(p) for p in self.inputs.get("web_dirs", [])],
                                          Path(ev) if ev else None, self.ws)
        elif self.inputs.get("decompiler_output"):
            meta = build_workspace(Path(self.inputs["decompiler_output"]), self.ws, float(self.c["alt_similarity_threshold"]))
        else:
            raise SystemExit("config: set input.decompiler_output and/or input.source_dirs")
        self.verifier = Verifier(self.ws)
        log.info("workspace: %d classes (%s), %d low-fidelity, %d web files", len(meta["classes"]), meta["primary"],
                 len(meta["flagged"]), meta["web_files"])
        if not prepare_only:
            asyncio.run(self._run_async(only or set()))
        summary = self.write(meta)
        summary["seconds"] = timer.seconds
        write_json(self.out / "summary.json", summary)
        return summary

    # ================================================================ outputs
    def write(self, meta: dict) -> dict:
        st = self.state
        write_json(self.out / "architecture.json", st["architecture"])
        write_json(self.out / "components.json", st["components"])
        write_json(self.out / "rules.json", st["rules"])
        write_json(self.out / "findings.json", st["findings"])
        write_json(self.out / "modernization-plan.json", st["plan"])
        facts = self._facts(meta)
        facts.write(self.out)
        (self.out / "modernization-report.md").write_text(self._markdown(meta), encoding="utf-8")
        (self.out / "modernization-report.html").write_text(self._html(meta), encoding="utf-8")
        sev = defaultdict(int)
        for f in st["findings"]:
            sev[f["severity"]] += 1
        return {"classes": len(meta["classes"]), "primary_decompiler": meta["primary"], "low_fidelity_classes": len(meta["flagged"]),
                "components_analysed": len(st["components"]), "rules": len(st["rules"]),
                "rules_values_verified": sum(1 for r in st["rules"] if r.get("values_verified")), "findings": len(st["findings"]),
                "findings_by_severity": dict(sev), "evidence_rejections": st["rejected"], "architecture": bool(st["architecture"]),
                "plan": bool(st["plan"]), "facts": len(facts.facts), **st["usage"], "tool_calls": st.get("tool_counts", {})}

    def _facts(self, meta: dict) -> FactSink:
        sink = FactSink("ai")
        file_class = {v["file"]: k for k, v in meta["classes"].items()}
        for r in self.state["rules"]:
            cls = file_class.get(r["file"], r["class_name"])
            nums = [v.replace(",", "") for v in r["values"] if re.fullmatch(r"-?\d+(?:\.\d+)?", v.replace(",", ""))]
            lits = [v for v in r["values"] if v.replace(",", "") not in nums and 0 < len(v) <= 40]
            keys = [f"class:{cls}"] + ([f"method:{cls}.{r['method']}"] if r["method"] and r["method"] != "(class)" else []) \
                + [f"number:{n}" for n in nums] + [f"literal:{v}" for v in lits]
            conf = "Probable" if r["certainty"] == "explicit" and r.get("values_verified") else "To verify"
            sink.add("rule", r["statement"], keys, f"code-ai/workspace/{r['file']}", f"line {r['line']}: {r['quote'][:160]}", confidence=conf,
                     kind=r["kind"], values=r["values"], certainty=r["certainty"], values_verified=r.get("values_verified"),
                     modernization_note=r.get("modernization_note", ""), component=r["component"])
        for f in self.state["findings"]:
            cls = file_class.get(f.get("file") or "")
            sink.add("finding", f"[{f['category']}/{f['severity']}] {f['title']}: {f['detail']}", [f"class:{cls}"] if cls else [],
                     f"code-ai/workspace/{f['file']}" if f.get("file") else "code-ai", f"line {f['line']}" if f.get("line") else "",
                     confidence="Probable" if f.get("line") else "To verify", category=f["category"], severity=f["severity"],
                     cwe=f.get("cwe", ""), recommendation=f["recommendation"])
        for name, comp in self.state["components"].items():
            m = comp["modernization"]
            classes = [c for c in meta["classes"] if meta["classes"][c]["package"] == name.split("#")[0]]
            sink.add("finding", f"Component {name} ({comp['layer']}): {comp['summary']} Modernisation: {m['strategy']}, complexity "
                     f"{m['complexity']}, effort {m['effort']}; target: {m['target_design']}" + (f" Blockers: {'; '.join(m['blockers'])}." if m["blockers"] else ""),
                     [f"class:{c}" for c in classes] + [f"table:{t.lower()}" for t in comp["data_access"]["tables"]],
                     "code-ai/components.json", name, confidence="To verify", category="modernization", component=name)
        return sink

    # ---------------------------------------------------------------- reports
    def _markdown(self, meta: dict) -> str:
        st = self.state
        e = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")  # noqa: E731
        a, p = st["architecture"] or {}, st["plan"] or {}
        origin = ("Source: original source code." if meta["primary"] == "source" else
                  f"Source: decompiled deployable ({meta['primary']} primary; {len(meta['flagged'])} low-fidelity classes cross-checked against "
                  "alternative decompilers).")
        lines = ["# Modernisation code analysis", "",
                 f"{origin} {len(meta['classes'])} classes, {len(st['components'])} components analysed. "
                 f"Target stack: {self.c['target_stack']}.", "",
                 f"Evidence discipline: {len(st['rules'])} rules and {len(st['findings'])} findings recorded with verbatim quotes verified "
                 f"against the workspace; {st['rejected']} unverifiable records were rejected and corrected by the agents.", ""]
        if a:
            lines += ["## Architecture", "", a["overview"], "", f"**Style:** {a['architecture_style']}", "",
                      "| Framework | Version | Role | Lifecycle | Modern replacement |", "|---|---|---|---|---|",
                      *[f"| {e(f['name'])} | {e(f['version'])} | {e(f['role'])} | {f['lifecycle']} | {e(f['modern_replacement'])} |" for f in a["frameworks"]], "",
                      "| Concern | Today | Target |", "|---|---|---|",
                      *[f"| {e(c['concern'])} | {e(c['current_implementation'])} | {e(c['target_implementation'])} |" for c in a["cross_cutting"]], "",
                      "**External systems:** " + "; ".join(f"{x['name']} ({x['protocol']})" for x in a["external_systems"]), "",
                      "**Technical debt:** " + a["technical_debt"], ""]
        lines += ["## Components", "", "| Component | Layer | Strategy | Complexity | Effort | Rules | Findings | Target |", "|---|---|---|---|---|---|---|---|"]
        for name, c in sorted(st["components"].items()):
            m = c["modernization"]
            lines.append(f"| {e(name)} | {c['layer']} | {m['strategy']} | {m['complexity']} | {m['effort']} | "
                         f"{sum(1 for r in st['rules'] if r['component'] == name)} | {sum(1 for f in st['findings'] if f['component'] == name)} | {e(m['target_design'])[:160]} |")
        if p:
            lines += ["", "## Target architecture and migration plan", "", p["target_architecture"], "", "### Bounded contexts", "",
                      "| Context | Components | Responsibilities | Data owned | API surface |", "|---|---|---|---|---|",
                      *[f"| {e(b['name'])} | {e(', '.join(b['components']))} | {e(b['responsibilities'])} | {e(', '.join(b['data_owned']))} | {e('; '.join(b['api_surface']))} |"
                        for b in p["bounded_contexts"]], "", "### Migration waves", "",
                      *[f"{w['wave']}. **{w['name']}**: {', '.join(w['components'])}. {w['rationale']} Prerequisites: {'; '.join(w['prerequisites']) or 'none'}. "
                        f"Exit: {'; '.join(w['exit_criteria'])}." for w in sorted(p["migration_waves"], key=lambda w: w["wave"])], "",
                      "### Decisions", "", *[f"- **{d['topic']}**: {d['decision']} ({d['rationale']})" for d in p["decisions"]], "",
                      "### Risks", "", *[f"- [{r['impact']}] {r['risk']} Mitigation: {r['mitigation']}" for r in p["risks"]], "",
                      "### Quick wins", "", *[f"- {q}" for q in p["quick_wins"]], "", f"**Effort:** {p['effort_summary']}", ""]
        order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
        lines += ["## Findings", "", "| Severity | Category | Finding | Location | Recommendation |", "|---|---|---|---|---|",
                  *[f"| {f['severity']} | {f['category']} | **{e(f['title'])}**: {e(f['detail'])}{' (' + e(f['cwe']) + ')' if f.get('cwe') else ''} | "
                    f"{e(f['file'])}{':' + str(f['line']) if f.get('line') else ''} | {e(f['recommendation'])} |"
                    for f in sorted(st["findings"], key=lambda f: order.get(f["severity"], 9))], "",
                  "## Business rules", "", "| Component | Kind | Rule | Values | Location | Modernisation |", "|---|---|---|---|---|---|",
                  *[f"| {e(r['component'])} | {r['kind']} | {e(r['statement'])} | {e(', '.join(r['values']))}{'' if r.get('values_verified') else ' ⚠'} | "
                    f"{e(r['file'])}:{r['line']} | {e(r.get('modernization_note'))} |" for r in sorted(st["rules"], key=lambda r: (r["component"], r["file"], r["line"]))], "",
                  "⚠ = at least one value is not present literally in the file (e.g. a constant resolved from elsewhere); verify.", "",
                  "## Component details", ""]
        for name, c in sorted(st["components"].items()):
            m = c["modernization"]
            lines += [f"### {name}", "", c["summary"], "", "Capabilities: " + "; ".join(c["business_capabilities"]), "",
                      f"Data: tables {', '.join(c['data_access']['tables']) or '-'}; {c['data_access']['access_style']}; transactions: {c['data_access']['transactions']}", "",
                      "| Class | Role | Target |", "|---|---|---|", *[f"| {e(x['class_name'])} | {e(x['role'])} | {e(x['target_mapping'])} |" for x in c["classes"]], "",
                      f"**Modernisation:** {m['strategy']} / {m['complexity']} / {m['effort']}. {m['target_design']}", "",
                      *([f"Blockers: {'; '.join(m['blockers'])}", ""] if m["blockers"] else []),
                      "Behaviour to preserve: " + "; ".join(m["behaviour_to_preserve"]), "", "Tests to write: " + "; ".join(m["tests_to_write"]), "",
                      *([f"Decompilation caveats: {c['decompilation_caveats']}", ""] if c.get("decompilation_caveats") else [])]
        u = st["usage"]
        lines += ["## Run", "", f"Model calls {u['calls']}, input tokens {u['input_tokens']}, output tokens {u['output_tokens']}. "
                  f"Tool calls: {json.dumps(st.get('tool_counts', {}))}."]
        return "\n".join(lines)

    def _html(self, meta: dict) -> str:
        """Markdown rendered as preformatted sections keeps the report dependency-free; tables become HTML tables."""
        md = self._markdown(meta)
        out, table = [], []

        def flush() -> None:
            if table:
                rows = [r for r in table if not re.fullmatch(r"\|(-+\|)+", r.replace(" ", ""))]
                cells = [[c.strip() for c in re.split(r"(?<!\\)\|", r.strip())[1:-1]] for r in rows]
                out.append("<table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells[0]) + "</tr>" +
                           "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:]) + "</table>")
                table.clear()

        for line in md.splitlines():
            if line.startswith("|"):
                table.append(line)
                continue
            flush()
            if line.startswith("### "):
                out.append(f"<h3>{_inline(line[4:])}</h3>")
            elif line.startswith("## "):
                out.append(f"<h2>{_inline(line[3:])}</h2>")
            elif line.startswith("# "):
                out.append(f"<h1>{_inline(line[2:])}</h1>")
            elif line.startswith("- "):
                out.append(f"<li>{_inline(line[2:])}</li>")
            elif line.strip():
                out.append(f"<p>{_inline(line)}</p>")
        flush()
        css = ("body{font-family:Segoe UI,Arial,sans-serif;margin:24px;max-width:1500px;color:#222}h1{color:#203864}h2{border-bottom:2px solid #305496;margin-top:32px}"
               "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px;vertical-align:top;text-align:left}"
               "th{background:#305496;color:#fff}code{background:#f3f3f3;padding:0 3px}")
        return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Modernisation code analysis</title><style>{css}</style></head><body>{''.join(out)}</body></html>"


def _inline(text: str) -> str:
    t = html.escape(text.replace("\\|", "|"))
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", t)


def run(cfg: dict, out: Path, prepare_only: bool = False, only: set[str] | None = None, fresh: bool = False) -> dict:
    return CodeAnalysis(cfg, out).run(prepare_only, only, fresh)
