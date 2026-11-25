"""GitHub Copilot SDK runtime for the exploration phase.

The Copilot agent runtime supervises one role and delegates to custom sub-agents (planner, navigator, screen-analyst)
that act only through the browser tools defined here. Inference goes to the configured Azure OpenAI deployment (BYOK with an
API key). Built-in Copilot tools (shell, files, web) are never enabled; every browser action passes the
safety policy in the pre-tool-use hook and again inside the tool handler."""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from uiagent.agents import INT, STR, arr, enum, obj
from uiagent.agents.analyst import Analyst, analysis_schema
from uiagent.config import copilot_provider, role_password
from uiagent.state import condense, screen_signature, url_pattern

log = logging.getLogger("uiagent.copilot")
ACTION_TOOLS = {"page_click", "page_fill_and_click", "page_back", "page_home"}
DONE_MARKER = "EXPLORATION COMPLETE"


class ToolError(Exception):
    pass


def _seconds(value: Any) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value) / 1000


class BrowserBridge:
    """Runs browser calls on the thread that owns the browser; Playwright and IEDriver sessions are thread-bound."""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()

    def submit(self, fn: Callable, *args) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fut, fn, args))
        return fut

    def serve(self, until: concurrent.futures.Future) -> None:
        while not until.done():
            try:
                fut, fn, args = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args))
                except Exception as exc:  # noqa: BLE001 - returned to the awaiting tool handler
                    fut.set_exception(exc)
        while not self._jobs.empty():
            self._jobs.get_nowait()[0].cancel()


def _runtime_path(cfg: dict) -> str | None:
    path = cfg["ai"]["copilot"].get("cli_path")
    if path:
        return path
    if getattr(sys, "frozen", False):  # packaged exe: runtime shipped next to it, or shared one level up in the release folder
        exe_dir = Path(sys.executable).parent
        for root in (exe_dir, exe_dir.parent):
            found = sorted(root.glob("copilot-runtime/**/copilot-runtime.exe"))
            if found:
                return str(found[-1])
    return None


def _client(cfg: dict, out: Path):
    from copilot import CopilotClient, RuntimeConnection

    path = _runtime_path(cfg)
    conn = RuntimeConnection.for_stdio(path=path) if path else RuntimeConnection.for_stdio()
    env = dict(os.environ)
    if not cfg["ai"]["copilot"].get("runtime_telemetry", False):
        env["COPILOT_TELEMETRY_DISABLED"] = "true"
    if path:
        env["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    conn.env = env
    return CopilotClient(mode="empty", base_directory=str(out / ".copilot"), connection=conn)


def _provider(cfg: dict) -> dict:
    return copilot_provider(cfg["ai"], cfg["ai"]["copilot"].get("wire_api", "responses"))


def ping(cfg: dict, out: Path) -> str:
    """Preflight: start the Copilot runtime and complete one tool call round-trip, as the exploration sessions do."""
    from copilot import ToolSet
    from copilot.session import PermissionHandler
    from copilot.tools import Tool, ToolResult

    called: list[str] = []

    async def report(invocation) -> Any:
        args = invocation.arguments or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        called.append(str(args.get("word", "")))
        return ToolResult(text_result_for_llm="received")

    tool = Tool(name="report_status", description="Report a status word.", skip_permission=True, handler=report,
                parameters={"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"],
                            "additionalProperties": False})

    async def run() -> str:
        async with _client(cfg, out) as client:
            kw: dict[str, Any] = dict(model=cfg["ai"]["model"], provider=_provider(cfg), tools=[tool],
                                      available_tools=ToolSet().add_custom("*"), on_permission_request=PermissionHandler.approve_all)
            if cfg["ai"]["copilot"].get("reasoning_effort"):
                kw["reasoning_effort"] = cfg["ai"]["copilot"]["reasoning_effort"]
            session = await client.create_session(**kw)
            ev = await session.send_and_wait("Call the report_status tool once with word 'OK', then reply with the single word OK.",
                                             timeout=180)
            await session.disconnect()
            if not called:
                raise RuntimeError("the model replied but did not call the test tool; the copilot runtime needs function tools")
            return (ev.data.content.strip() if ev else "") + " (tool call ok)"

    return asyncio.run(run())


class CopilotExplorer:
    def __init__(self, orch) -> None:
        self.o = orch
        self.cfg = orch.cfg
        self.base = orch.base
        self.events_log = orch.out / "copilot-events.jsonl"

    # ================================================================ entry point (browser thread)
    def explore_role(self, b, role: str) -> None:
        self.b, self.role = b, role
        self.bridge = BrowserBridge()
        self.steps = 0
        self.stop_reason = ""
        self.tried: dict[str, set[str]] = {}
        self.seen_labels: set[str] = set()
        self.clicked_labels: set[str] = set()
        self.agent_by_call: dict[str, str] = {}
        obs = b.observe()
        self.home_url = obs["url"]
        new = screen_signature(obs, self.base) not in self.o.state.screens
        self._set_page(obs)
        landing = self._page_text(new)
        done: concurrent.futures.Future = concurrent.futures.Future()

        def runner() -> None:
            try:
                asyncio.run(self._supervise(landing))
                done.set_result(None)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the browser thread
                done.set_exception(exc)

        threading.Thread(target=runner, name=f"copilot-{role}", daemon=True).start()
        self.bridge.serve(done)
        try:
            done.result()
        finally:
            self._analyse_leftovers()
        self.o.state.role_done[role] = True
        log.info("[%s] copilot exploration finished: %d steps, %d screens (%s)", role, self.steps,
                 sum(1 for s in self.o.state.screens.values() if role in s.roles), self.stop_reason or "complete")

    # ================================================================ supervisor session
    async def _supervise(self, landing: str) -> None:
        from copilot import ToolSet
        from copilot.session import PermissionHandler

        cc = self.cfg["ai"]["copilot"]
        app = self.cfg["app"]
        spec = self.cfg["roles"][self.role]
        others = {r: sorted({(s.analysis or {}).get("screen_name", s.title) for s in self.o.state.screens.values() if r in s.roles})
                  for r in self.cfg["roles"] if r != self.role}
        limits = self.cfg["limits"]
        async with _client(self.cfg, self.o.out) as client:
            kw: dict[str, Any] = dict(
                model=self.cfg["ai"]["model"], provider=_provider(self.cfg), tools=self._tools(),
                available_tools=ToolSet().add_custom("*").add_builtin(["task", "read_agent"]),
                custom_agents=self._agents(), hooks={"on_pre_tool_use": self._pre_tool},
                on_permission_request=PermissionHandler.approve_all, on_event=self._on_event,
                allow_multiple_tool_calls=False, system_message={"mode": "append", "content": self._supervisor_rules()})
            if cc.get("reasoning_effort"):
                kw["reasoning_effort"] = cc["reasoning_effort"]
            session = await client.create_session(**kw)
            prompt = (f"Explore '{app['name']}' as role '{self.role}' ({spec.get('description') or 'no description'}).\n"
                      f"Landing page after sign-in:\n{landing}\n\n"
                      f"Declared application paths from code analysis: {json.dumps(self.o.declared[:150])}\n"
                      f"Screens already documented for other roles: {json.dumps(others)[:3000]}\n"
                      f"Budget: {limits['max_steps_per_role']} browser actions. Start with the planner.")
            for turn in range(int(cc.get("max_turns", 12))):
                try:
                    ev = await session.send_and_wait(prompt, timeout=float(cc.get("turn_timeout_s", 1800)))
                except TimeoutError:
                    log.warning("[%s] supervisor turn %d timed out", self.role, turn + 1)
                    abort = getattr(session, "abort", None)
                    if abort:
                        await abort()
                    ev = None
                reply = (ev.data.content or "") if ev else ""
                self._event({"t": "supervisor.reply", "turn": turn + 1, "text": reply[:2000]})
                if DONE_MARKER in reply or self._out_of_budget():
                    break
                prompt = (f"Continue exploring as role '{self.role}'. Current progress: {self._progress()[:6000]}\n"
                          f"If every goal is closed and nothing untried remains, reply with a summary ending with {DONE_MARKER}.")
            await session.disconnect()

    def _supervisor_rules(self) -> str:
        return (f"\nYou supervise the read-only UI analysis of the legacy web application '{self.cfg['app']['name']}' for one user role. "
                "Do not operate the browser yourself; delegate with the task tool to your sub-agents:\n"
                "- planner: reads the page and progress and saves prioritised exploration goals.\n"
                "- navigator: given ONE goal, operates the browser read-only to reach and explore it, then closes the goal.\n"
                "- screen-analyst: documents every newly discovered screen.\n"
                "Workflow: planner first; then for each open goal in priority order run the navigator, and run the screen-analyst "
                "whenever progress shows screens pending analysis; when goals run out but untried links or unreached declared paths "
                "remain, ask the planner for new goals. Actions denied by the safety policy are expected evidence: never retry them "
                f"or ask to bypass them. When nothing remains or the budget is exhausted, reply with a short summary ending with {DONE_MARKER}.")

    def _agents(self) -> list[dict]:
        app = self.cfg["app"]
        ctx = f"Application: '{app['name']}'. {app.get('description') or ''} You are part of a read-only analysis; never change data. "
        modules_hint = f"Business modules: {', '.join(app['modules'])}. " if app.get("modules") else ""
        return [
            {"name": "planner", "display_name": "Planner", "tools": ["progress", "page_observe", "plan_save"],
             "description": "Plans which business functions a role should explore; saves prioritised goals.",
             "prompt": ctx + ("Plan how this role explores the application so every screen and function available to it is visited "
                              "once. Goals are business functions (e.g. 'Customer search', 'Approval queue') each with an entry hint "
                              "(menu item, link or path). Call progress and page_observe, then plan_save with 5-40 NEW goals, priority 1 "
                              "highest, one per menu entry or functional area. Reply with one line.")},
            {"name": "navigator", "display_name": "Navigator",
             "tools": ["page_observe", "page_click", "page_fill_and_click", "page_back", "page_home", "progress", "goal_update"],
             "description": "Operates the browser read-only to reach and explore one goal.",
             "prompt": ctx + ("You receive one goal. Call page_observe first. Act only with element ids from the latest page listing: "
                              "page_click for links/menus/tabs, page_fill_and_click only for search/filter/lookup forms (use known values "
                              "or wildcards so lists and detail screens appear), page_back/page_home to escape dead ends. Every action "
                              "returns the new page. Visit each sub-screen of the goal once (tabs, result rows, detail links); skip "
                              "elements in already_tried_here. Never choose elements that save, submit, approve, reject, delete, cancel, "
                              "pay or log out. Finish with goal_update (done or unreachable) and reply with the screens reached.")},
            {"name": "screen-analyst", "display_name": "Screen Analyst", "tools": ["screen_pending", "screen_inspect", "screen_record"],
             "description": "Documents newly discovered screens from their screenshot and DOM listing.",
             "prompt": ctx + ("Call screen_pending; for each screen call screen_inspect (DOM listing plus screenshot) then screen_record "
                              "with the full analysis. Use the screenshot for layout, mandatory markers, disabled controls and tabs, and "
                              "the DOM listing for exact names and options. visible_rules are rules a user can infer from the screen "
                              "itself (mandatory fields, formats, limits, hints, conditional buttons, warnings); do not guess hidden "
                              f"behaviour. {modules_hint}Reply with the number of screens recorded.")},
        ]

    # ================================================================ tools
    def _tools(self) -> list:
        from copilot.tools import Tool

        modules = self.cfg["app"]["modules"]
        none = {"type": "object", "properties": {}, "additionalProperties": False}
        defs = [
            ("page_observe", "Read the current browser page: screen id, element listing with ids, forms, tables, blocked elements.",
             none, self._t_observe),
            ("page_click", "Click a link, menu entry, tab or button by element id; returns the resulting page.",
             obj({"element_id": STR, "intent": STR}), self._t_click),
            ("page_fill_and_click", "Fill search/filter fields then click the submit element; returns the resulting page.",
             obj({"inputs": arr(obj({"element_id": STR, "value": STR})), "submit_element_id": STR, "intent": STR}), self._t_fill),
            ("page_back", "Browser back; returns the resulting page.", none, self._t_back),
            ("page_home", "Return to the role's landing page; returns it.", none, self._t_home),
            ("progress", "Exploration progress for this role: visited screens, goals, untried links, unreached declared paths, budget.",
             none, self._t_progress),
            ("plan_save", "Save new exploration goals for this role.",
             obj({"goals": arr(obj({"goal_id": STR, "title": STR, "entry_hint": STR, "priority": INT, "why": STR}))}), self._t_plan),
            ("goal_update", "Close a goal as done or unreachable.",
             obj({"goal_id": STR, "status": enum(["done", "unreachable"]), "note": STR}), self._t_goal),
            ("screen_pending", "List discovered screens that still need analysis.", none, self._t_pending),
            ("screen_inspect", "DOM listing and screenshot of one discovered screen.", obj({"screen_id": STR}), self._t_inspect),
            ("screen_record", "Record the analysis of one screen.",
             obj({"screen_id": STR, "analysis": analysis_schema(modules)}), self._t_record),
        ]
        return [Tool(name=n, description=d, parameters=p, handler=self._wrap(n, h)) for n, d, p, h in defs]

    def _wrap(self, name: str, handler: Callable):
        from copilot.tools import ToolResult

        async def run(invocation) -> Any:
            args = invocation.arguments or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            try:
                result = await handler(args)
            except ToolError as exc:
                return ToolResult(text_result_for_llm=str(exc), result_type="failure", error=str(exc))
            except Exception as exc:  # noqa: BLE001 - surfaced to the agent so it can recover
                log.warning("[%s] %s failed: %s", self.role, name, exc)
                self.o.state.errors.append({"role": self.role, "tool": name, "error": str(exc)[:300]})
                return ToolResult(text_result_for_llm=f"{name} failed: {str(exc)[:300]}", result_type="failure", error=str(exc)[:300])
            return result if isinstance(result, ToolResult) else ToolResult(text_result_for_llm=result)

        return run

    async def _on_browser(self, fn: Callable, *args) -> Any:
        return await asyncio.wrap_future(self.bridge.submit(fn, *args))

    async def _t_observe(self, _args: dict) -> str:
        return await self._on_browser(self._observe_sync)

    async def _t_click(self, args: dict) -> str:
        return await self._on_browser(self._act_sync, "click", args["element_id"], [], args.get("intent", ""))

    async def _t_fill(self, args: dict) -> str:
        return await self._on_browser(self._act_sync, "fill_and_click", args["submit_element_id"], args.get("inputs", []), args.get("intent", ""))

    async def _t_back(self, _args: dict) -> str:
        return await self._on_browser(self._act_sync, "back", None, [], "")

    async def _t_home(self, _args: dict) -> str:
        return await self._on_browser(self._act_sync, "home", None, [], "")

    async def _t_progress(self, _args: dict) -> str:
        return self._progress()

    async def _t_plan(self, args: dict) -> str:
        plan = self.o.state.plans.setdefault(self.role, {"goals": [], "done": []})
        known = {g["goal_id"] for g in plan["goals"]}
        new = [g for g in args.get("goals", []) if g["goal_id"] not in known]
        plan["goals"] += new
        self.o._save()
        return f"saved {len(new)} new goals ({len(plan['goals'])} total)"

    async def _t_goal(self, args: dict) -> str:
        plan = self.o.state.plans.setdefault(self.role, {"goals": [], "done": []})
        for g in plan["goals"]:
            if g["goal_id"] == args["goal_id"]:
                g["status"], g["note"] = args["status"], args.get("note", "")
        if args["goal_id"] not in plan["done"]:
            plan["done"].append(args["goal_id"])
        open_goals = [g["goal_id"] for g in plan["goals"] if g["goal_id"] not in plan["done"]]
        return f"goal {args['goal_id']} closed as {args['status']}; open goals: {open_goals}"

    async def _t_pending(self, _args: dict) -> str:
        pending = [{"screen_id": s.sig, "url": s.url_pattern, "title": s.title, "heading": s.heading}
                   for s in list(self.o.state.screens.values()) if s.analysis is None and s.sig in self.o.pages]
        return json.dumps(pending[:30]) if pending else "no screens pending analysis"

    async def _t_inspect(self, args: dict):
        from copilot.tools import ToolBinaryResult, ToolResult

        screen = self.o.state.screens.get(args["screen_id"])
        if screen is None or screen.sig not in self.o.pages:
            raise ToolError("unknown screen_id; call screen_pending")
        shot = self.o.out / screen.screenshot
        images = []
        if shot.exists() and self.cfg["ai"].get("vision", True):
            images.append(ToolBinaryResult(data=base64.b64encode(shot.read_bytes()).decode(), mime_type="image/png", type="image",
                                           description=f"screenshot of {screen.url_pattern}"))
        return ToolResult(text_result_for_llm=f"screen_id={screen.sig} first seen by {screen.first_role}\n{self.o.pages[screen.sig]}",
                          binary_results_for_llm=images or None)

    async def _t_record(self, args: dict) -> str:
        screen = self.o.state.screens.get(args["screen_id"])
        if screen is None:
            raise ToolError("unknown screen_id")
        screen.analysis = args["analysis"]
        self.o._save()
        left = sum(1 for s in list(self.o.state.screens.values()) if s.analysis is None and s.sig in self.o.pages)
        return f"recorded '{screen.analysis.get('screen_name')}'; {left} screens still pending"

    # ================================================================ browser-thread operations
    def _set_page(self, obs: dict) -> None:
        self.obs = obs
        self.screen = self.o._register(self.b, self.role, obs, analyse=False)

    def _page_text(self, new: bool, note: str = "") -> str:
        page, clickable, _ = condense(self.obs, self.base, tables=self.cfg["ai"].get("send_table_samples", True),
                                      mask=self.cfg["safety"].get("mask_patterns_in_prompts", True))
        elements = {e["id"]: e for e in self.obs["elements"]}
        blocked = [i for i in clickable if self.o.policy.check(elements[i])[0] == "deny"]
        seen = self.tried.get(self.screen.sig, set())
        allowed = [i for i in clickable if i not in blocked]
        self.seen_labels.update((elements[i].get("text") or "")[:60] for i in allowed if elements[i].get("text"))
        visits = self.screen.roles[self.role]["visits"]
        header = {"screen_id": self.screen.sig, "screen_name": (self.screen.analysis or {}).get("screen_name"), "new_screen": new,
                  "visits_by_this_role": visits, "actions_left": int(self.cfg["limits"]["max_steps_per_role"]) - self.steps,
                  "blocked_by_policy": [f"{i} {(elements[i].get('text') or '')[:40]}" for i in blocked][:40],
                  "already_tried_here": [i for i in allowed if self.o._key(elements[i]) in seen][:60],
                  "known_values": self.o.state.known_values}
        warn = "\nWARNING: this screen was visited many times; move on to another area." if visits > int(self.cfg["limits"]["max_visits_per_screen"]) * 3 else ""
        return (note + "\n" if note else "") + json.dumps(header, ensure_ascii=False) + warn + "\n" + page

    def _relogin_if_needed(self) -> str:
        if not self.obs.get("has_password"):
            return ""
        spec = self.cfg["roles"][self.role]
        if not self.o.login_agent.login(self.b, self.role, spec.get("username", ""), role_password(spec)):
            self.stop_reason = "session lost and sign-in failed"
            raise ToolError(self.stop_reason)
        self._set_page(self.b.observe())
        return "Session had expired; signed in again (now on the landing page)."

    def _observe_sync(self) -> str:
        obs = self.b.observe()
        new = screen_signature(obs, self.base) not in self.o.state.screens
        self._set_page(obs)
        note = self._relogin_if_needed()
        return self._page_text(new, note)

    def _act_sync(self, action: str, element_id: str | None, inputs: list[dict], intent: str) -> str:
        before = self.screen
        label = ""
        popup = None
        if action == "back":
            self.b.back()
        elif action == "home":
            self.b.goto(self.home_url)
        else:
            elements = {e["id"]: e for e in self.obs["elements"]}
            element = elements.get(element_id or "")
            if element is None:
                raise ToolError(f"element {element_id} is not on the current page; call page_observe")
            if self.o.policy.check(element)[0] == "deny":
                self.o._allowed(self.role, before, self.obs, element)
                raise ToolError(f"blocked by safety policy: {element_id}")
            label = (element.get("text") or element.get("name") or "")[:80]
            self.tried.setdefault(before.sig, set()).add(self.o._key(element))
            self.clicked_labels.add(label[:60])
            if action == "fill_and_click":
                for inp in inputs:
                    target = elements.get(inp.get("element_id", ""))
                    if not target or target["kind"] != "field" or target.get("readonly"):
                        continue
                    if target.get("type") == "select":
                        self.b.select(target["id"], inp["value"])
                    elif target.get("type") in ("checkbox", "radio"):
                        self.b.click(target["id"])
                    else:
                        self.b.fill(target["id"], inp["value"])
            self.b.click(element["id"])
            popup = self.b.handle_popups()
        self.steps += 1
        obs = self.b.observe()
        new = screen_signature(obs, self.base) not in self.o.state.screens
        self._set_page(obs)
        note = self._relogin_if_needed()
        if action in ("click", "fill_and_click"):
            self.o.state.transitions.append({"role": self.role, "from": before.sig, "to": self.screen.sig, "via": label,
                                             "action": action, "popup": bool(popup)})
        self.o.state.steps.append({"step": self.steps, "role": self.role, "goal": "", "screen": before.sig, "action": action,
                                   "element": label, "intent": intent, "runtime": "copilot",
                                   "result": "new screen" if new else ("same screen" if self.screen.sig == before.sig else "known screen")})
        if self.steps % 5 == 0:
            self.o._save()
        return self._page_text(new, note or f"{action} done" + (f"; popup window {popup}" if popup else ""))

    # ================================================================ safety hook, budget, telemetry
    def _out_of_budget(self) -> bool:
        if self.steps >= int(self.cfg["limits"]["max_steps_per_role"]):
            self.stop_reason = "browser action budget exhausted"
        elif time.monotonic() > self.o.deadline:
            self.stop_reason = "time budget exhausted"
        elif self.o.llm.calls >= self.o.llm.max_calls:
            self.stop_reason = "model call budget exhausted"
        return bool(self.stop_reason)

    async def _pre_tool(self, inp: dict, _invocation) -> dict:
        name = inp.get("toolName", "")
        args = inp.get("toolArgs") or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        if name in ACTION_TOOLS | {"page_observe"} and self._out_of_budget():
            return {"permissionDecision": "deny", "permissionDecisionReason": f"{self.stop_reason}: stop exploring and report."}
        if name in ("page_click", "page_fill_and_click"):
            eid = args.get("element_id") if name == "page_click" else args.get("submit_element_id")
            element = next((e for e in self.obs["elements"] if e["id"] == eid), None)
            if element is not None:
                ok, reason = await asyncio.to_thread(self.o._allowed, self.role, self.screen, self.obs, element)
                if not ok:
                    label = (element.get("text") or element.get("name") or eid)[:60]
                    return {"permissionDecision": "deny",
                            "permissionDecisionReason": f"Safety policy blocked '{label}' ({reason}). Recorded as evidence; do not retry."}
        return {"permissionDecision": "allow"}

    def _progress(self) -> str:
        role = self.role
        plan = self.o.state.plans.get(role, {"goals": [], "done": []})
        screens = list(self.o.state.screens.values())
        mine = [s for s in screens if role in s.roles]
        reached = {s.url_pattern.split("?")[0] for s in mine}
        declared = sorted({url_pattern(self.base.rstrip("/") + d, self.base).split("?")[0] for d in self.o.declared} - reached)
        return json.dumps({
            "role": role,
            "screens_visited": [{"screen_id": s.sig, "name": (s.analysis or {}).get("screen_name") or s.title, "url": s.url_pattern} for s in mine][:150],
            "goals": [{"goal_id": g["goal_id"], "title": g["title"], "status": g.get("status") or ("closed" if g["goal_id"] in plan["done"] else "open")}
                      for g in plan["goals"]],
            "untried_links": sorted(self.seen_labels - self.clicked_labels)[:120],
            "declared_paths_not_reached": declared[:80],
            "screens_pending_analysis": sum(1 for s in screens if s.analysis is None and s.sig in self.o.pages),
            "actions_left": int(self.cfg["limits"]["max_steps_per_role"]) - self.steps,
            "model_calls_left": self.o.llm.max_calls - self.o.llm.calls,
            "minutes_left": max(0, int((self.o.deadline - time.monotonic()) / 60)),
        }, ensure_ascii=False)

    def _event(self, record: dict) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "role": self.role, **record}
        if len(self.o.state.agent_events) < 20000:
            self.o.state.agent_events.append(record)
        with self.events_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _on_event(self, e) -> None:
        try:
            self._handle_event(e)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break the session
            log.debug("event handling failed: %s", exc)

    def _handle_event(self, e) -> None:
        kind = e.type.value if hasattr(e.type, "value") else str(e.type)
        d = e.data
        if kind == "assistant.usage":
            agent = self.agent_by_call.get(getattr(d, "parent_tool_call_id", None) or "", "supervisor")
            self.o.llm.calls += 1
            self.o.llm.prompt_tokens += getattr(d, "input_tokens", None) or 0
            self.o.llm.completion_tokens += getattr(d, "output_tokens", None) or 0
            self.o.llm._record(f"copilot/{agent}", 200, _seconds(getattr(d, "duration", None)),
                               {"prompt_tokens": getattr(d, "input_tokens", None), "completion_tokens": getattr(d, "output_tokens", None)})
        elif kind == "subagent.started":
            self.agent_by_call[d.tool_call_id] = d.agent_name
            self._event({"t": kind, "agent": d.agent_name})
        elif kind in ("subagent.completed", "subagent.failed"):
            self._event({"t": kind, "agent": d.agent_name, "tokens": getattr(d, "total_tokens", None),
                         "seconds": _seconds(getattr(d, "duration", None)),
                         "tool_calls": getattr(d, "total_tool_calls", None), "error": str(getattr(d, "error", "") or "")[:300]})
        elif kind == "tool.execution_start":
            agent = self.agent_by_call.get(getattr(d, "parent_tool_call_id", None) or "", "supervisor")
            self._event({"t": "tool", "agent": agent, "tool": d.tool_name, "args": json.dumps(d.arguments, default=str)[:300]})
        elif kind == "session.error":
            log.warning("[%s] copilot session error: %s %s", self.role, getattr(d, "error_type", ""), str(getattr(d, "message", ""))[:300])
            self._event({"t": kind, "error": str(getattr(d, "message", ""))[:500]})

    def _analyse_leftovers(self) -> None:
        """Screens the screen-analyst did not reach are documented by the direct Analyst so the catalogue stays complete."""
        analyst = Analyst(self.o.llm, self.cfg)
        for s in self.o.state.screens.values():
            if s.analysis is None and s.sig in self.o.pages and self.role in s.roles:
                try:
                    shot = self.o.out / s.screenshot
                    s.analysis = analyst.analyse(self.role, self.o.pages[s.sig], shot if shot.exists() else None)
                except Exception as exc:  # noqa: BLE001
                    log.warning("leftover analysis failed for %s: %s", s.sig, exc)
        self.o._save()
