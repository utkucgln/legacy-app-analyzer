"""Orchestrator (supervisor): runs the agent team role by role, then access check, optional probing, comparison and report."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from uiagent import browser as browser_factory
from uiagent import design, report
from uiagent.agents.analyst import Analyst
from uiagent.agents.comparator import Comparator
from uiagent.agents.guard import Guard
from uiagent.agents.judge import Judge
from uiagent.agents.login import LoginAgent
from uiagent.agents.navigator import Navigator
from uiagent.agents.planner import Planner
from uiagent.agents.prober import Prober
from uiagent.agents.reporter import Reporter
from uiagent.config import api_key, role_password
from uiagent.llm import LLM, BudgetExceeded
from uiagent.safety import Policy, _has_word
from uiagent.state import RunState, Screen, condense, screen_signature, url_pattern

log = logging.getLogger("uiagent")
SIGN_OUT = ["logout", "logoff", "log out", "sign out", "signout", "exit", "خروج"]


class Orchestrator:
    def __init__(self, cfg: dict, resume: bool = False) -> None:
        self.cfg = cfg
        self.out = Path(cfg["output_dir"])
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "state.json"
        self.state = RunState.load(self.state_path) if resume and self.state_path.exists() else RunState(
            started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.llm = LLM(cfg["ai"]["endpoint"], cfg["ai"]["model"], api_key(cfg["ai"]),
                       max_calls=int(cfg["limits"]["max_llm_calls"]), calls_log=self.out / "llm-calls.jsonl")
        self.policy = Policy(cfg)
        self.planner, self.navigator, self.guard = Planner(self.llm, cfg), Navigator(self.llm, cfg), Guard(self.llm, cfg)
        self.analyst, self.prober, self.judge = Analyst(self.llm, cfg), Prober(self.llm, cfg), Judge(self.llm, cfg)
        self.comparator, self.reporter, self.login_agent = Comparator(self.llm, cfg), Reporter(self.llm, cfg), LoginAgent(self.llm, cfg)
        self.base = cfg["app"]["base_url"]
        self.deadline = time.monotonic() + 60 * int(cfg["limits"]["max_minutes"])
        self.declared = self._declared_paths()
        self.pages: dict[str, str] = {}
        self.runtime = cfg["ai"].get("runtime", "direct")
        self._blocked_keys = {(x["role"], x["screen"], x["element"]) for x in self.state.blocked}
        for name, values in (cfg["app"].get("sample_values") or {}).items():
            bucket = self.state.known_values.setdefault(name, [])
            bucket += [str(v) for v in values if str(v) not in bucket]

    # ================================================================ entry point
    def run(self) -> dict:
        phases = self.cfg["phases"]
        try:
            if phases.get("explore", True):
                explore = self.explore_role
                if self.runtime == "copilot":
                    from uiagent.copilot_runtime import CopilotExplorer

                    explore = CopilotExplorer(self).explore_role
                for role in self.cfg["roles"]:
                    if not self.state.role_done.get(role):
                        self._with_browser(role, explore)
            if phases.get("access_check", True) and not self.state.access:
                for role in self.cfg["roles"]:
                    self._with_browser(role, self.access_check)
            if phases.get("probe") and self.cfg["safety"]["mode"] != "read_only":
                for role in self.cfg["roles"]:
                    self._with_browser(role, self.probe)
            if self.state.comparison is None:
                self.state.comparison = self.comparator.compare(self.matrix(), self.screen_summaries())
                self._save()
            if phases.get("design", True):
                self.design_summary = design.DesignAnalysis(self).run()
        except BudgetExceeded as exc:
            log.warning("%s - writing report with what was collected", exc)
            self.state.errors.append({"phase": "budget", "error": str(exc)})
        finally:
            self._save()
        if phases.get("report", True):
            return self.write_report()
        return {}

    def write_report(self) -> dict:
        narrative = None
        try:
            narrative = self.reporter.summarise(self.screen_summaries(), self.state.comparison, self.coverage(), self.state.probes)
        except (BudgetExceeded, RuntimeError) as exc:
            log.warning("reporter failed: %s", exc)
        return report.write(self.out, self.cfg, self.state, self.matrix(), narrative, self.coverage())

    # ================================================================ helpers
    def _save(self) -> None:
        self.state.save(self.state_path)

    def _declared_paths(self) -> list[str]:
        path = self.cfg["app"].get("declared_paths_file")
        if not path or not Path(path).exists():
            return []
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        urls = []
        if isinstance(data, list) and data and isinstance(data[0], dict) and "actions" in data[0]:  # Legacy Decompiler actions.json
            for cfg in data:
                urls += [a["path"] + ".do" for a in cfg["actions"] if a.get("path")]
        elif isinstance(data, list):
            urls = [str(x) for x in data]
        return sorted(set(urls))

    def _with_browser(self, role: str, fn) -> None:
        b = browser_factory.create(self.cfg)
        b.cfg["_dismiss_confirm"] = self.cfg["safety"].get("dismiss_confirm_dialogs", True) or self.cfg["safety"]["mode"] == "read_only"
        b.start()
        try:
            spec = self.cfg["roles"][role]
            if not self.login_agent.login(b, role, spec.get("username", ""), role_password(spec)):
                self.state.errors.append({"role": role, "phase": getattr(fn, "__name__", "phase"), "error": "login failed"})
                return
            fn(b, role)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - keep other roles running
            log.exception("%s failed for %s", getattr(fn, "__name__", "phase"), role)
            self.state.errors.append({"role": role, "phase": getattr(fn, "__name__", "phase"), "error": str(exc)[:500]})
        finally:
            self.state.sessions = sorted(set(self.state.sessions) | set(b.session_ids()))
            self.state.dialogs += [{**d, "role": role} for d in b.dialogs]
            try:
                b.stop()
            finally:
                self._save()

    def _register(self, b, role: str, obs: dict, analyse: bool = True) -> Screen:
        sig = screen_signature(obs, self.base)
        page, _, _ = condense(obs, self.base, tables=self.cfg["ai"].get("send_table_samples", True),
                              mask=self.cfg["safety"].get("mask_patterns_in_prompts", True))
        shot = self.out / "screenshots" / role / f"{sig}.png"
        screen = self.state.screens.get(sig)
        if role not in (screen.roles if screen else {}):
            try:
                b.screenshot(shot)
            except Exception as exc:  # noqa: BLE001
                log.warning("screenshot failed: %s", exc)
        if screen is None:
            screen = Screen(sig=sig, url_pattern=url_pattern(obs["url"], self.base), title=obs.get("title", ""),
                            heading=(obs.get("headings") or [""])[0], screenshot=str(shot.relative_to(self.out)), first_role=role,
                            sample_url=obs["url"])
            self.state.screens[sig] = screen
            self.pages[sig] = page
            log.info("[%s] new screen %s: %s", role, sig, screen.url_pattern)
            design.capture(self, b, sig)
            if analyse:
                try:
                    screen.analysis = self.analyst.analyse(role, page, shot if shot.exists() else None)
                except RuntimeError as exc:
                    log.warning("analysis failed for %s: %s", sig, exc)
        entry = screen.roles.setdefault(role, {"visits": 0, "url": obs["url"], "screenshot": str(shot.relative_to(self.out)),
                                               "actions_visible": [], "menu_links": []})
        entry["visits"] += 1
        entry["actions_visible"] = sorted({(e.get("text") or e.get("name") or "")[:60] for e in obs["elements"]
                                           if e["kind"] in ("button", "link", "clickable") and e.get("visible") and (e.get("text") or e.get("name"))})[:80]
        entry["menu_links"] = sorted({(e.get("text") or "")[:60] for e in obs["elements"] if e.get("nav") and e["kind"] == "link" and e.get("visible")})[:80]
        for e in obs["elements"]:
            if e["kind"] in ("button", "link", "clickable") and e.get("visible") and not e.get("disabled"):
                verdict, reason = self.policy.check(e)
                if verdict == "deny" and reason != "out of scope" and not _has_word(
                        f"{e.get('text') or ''} {e.get('href') or ''}", SIGN_OUT):
                    self._record_blocked(role, screen, e, f"visible, not executed: {reason}")
        self.state.remember_values(obs)
        return screen

    def _record_blocked(self, role: str, screen: Screen, element: dict, reason: str) -> None:
        label = (element.get("text") or element.get("name") or "")[:80]
        key = (role, screen.sig, label)
        if not label or key in self._blocked_keys:
            return
        self._blocked_keys.add(key)
        self.state.blocked.append({"role": role, "screen": screen.sig, "screen_name": (screen.analysis or {}).get("screen_name", screen.title),
                                   "element": label, "kind": element["kind"], "reason": reason})

    @staticmethod
    def _element(obs: dict, element_id: str) -> dict | None:
        return next((e for e in obs["elements"] if e["id"] == element_id), None)

    @staticmethod
    def _key(e: dict) -> str:
        return f"{e['kind']}|{(e.get('text') or e.get('name') or '')[:60]}|{(e.get('href') or '')[-80:]}"

    def _allowed(self, role: str, screen: Screen, obs: dict, element: dict) -> tuple[bool, str]:
        form = obs["forms"][element["form"]] if 0 <= element.get("form", -1) < len(obs.get("forms", [])) else None
        verdict, reason = self.policy.check(element, form)
        if verdict == "ask":
            review = self.guard.review({k: element.get(k) for k in ("kind", "text", "name", "type", "href", "onclick")}, form,
                                       obs.get("title", ""), self.cfg["safety"]["mode"])
            ok = review["read_only"] and review["category"] in ("navigation", "query")
            verdict, reason = ("allow" if ok else "deny"), f"guard: {review['category']} - {review['reason']}"
        if verdict == "deny":
            self._record_blocked(role, screen, element, reason)
        return verdict == "allow", reason

    # ================================================================ phase 1: exploration
    def explore_role(self, b, role: str) -> None:
        limits = self.cfg["limits"]
        landing_obs = b.observe()
        home_url = landing_obs["url"]
        landing = self._register(b, role, landing_obs)
        page, _, _ = condense(landing_obs, self.base)
        others = {r: sorted({(s.analysis or {}).get("screen_name", s.title) for s in self.state.screens.values() if r in s.roles})
                  for r in self.cfg["roles"] if r != role}
        plan = self.state.plans.get(role) or self.planner.plan(role, self.cfg["roles"][role].get("description", ""), page,
                                                                  self.declared, others)
        plan.setdefault("done", [])
        self.state.plans[role] = plan
        goals = [g for g in sorted(plan["goals"], key=lambda g: g["priority"]) if g["goal_id"] not in plan["done"]]
        tried: dict[str, set[str]] = {}
        seen_labels: set[str] = set()
        clicked_labels: set[str] = set()
        history: list[dict] = []
        steps = errors = same = 0
        last_sig = landing.sig
        replans = 0
        while steps < int(limits["max_steps_per_role"]) and time.monotonic() < self.deadline:
            if not goals:
                if replans >= 3:
                    break
                replans += 1
                visited = [(s.analysis or {}).get("screen_name", s.title) for s in self.state.screens.values() if role in s.roles]
                untried = sorted(seen_labels - clicked_labels)[:150]
                new = self.planner.replan(role, [{"goal_id": g} for g in plan["done"]], visited, untried,
                                          [d for d in self.declared if not any(url_pattern(s.sample_url, self.base).startswith(d.split("?")[0])
                                                                               for s in self.state.screens.values() if role in s.roles)])
                goals = sorted(new["goals"], key=lambda g: g["priority"])
                plan["goals"] += goals
                if not goals:
                    break
            goal = goals[0]
            steps += 1
            obs = b.observe()
            if obs.get("has_password"):
                log.info("[%s] session lost - signing in again", role)
                spec = self.cfg["roles"][role]
                if not self.login_agent.login(b, role, spec.get("username", ""), role_password(spec)):
                    break
                continue
            screen = self._register(b, role, obs)
            same = same + 1 if screen.sig == last_sig else 0
            last_sig = screen.sig
            page, clickable, fillable = condense(obs, self.base, tables=self.cfg["ai"].get("send_table_samples", True),
                                                 mask=self.cfg["safety"].get("mask_patterns_in_prompts", True))
            seen = tried.setdefault(screen.sig, set())
            elements = {e["id"]: e for e in obs["elements"]}
            blocked_ids = [i for i in clickable if self.policy.check(elements[i])[0] == "deny"]
            allowed_click = [i for i in clickable if i not in blocked_ids]
            tried_ids = [i for i in allowed_click if self._key(elements[i]) in seen]
            seen_labels.update((elements[i].get("text") or "")[:60] for i in allowed_click if elements[i].get("text"))
            if same >= 4 or screen.roles[role]["visits"] > int(limits["max_visits_per_screen"]) * 3:
                b.goto(home_url)
                history.append({"step": steps, "action": "home", "reason": "loop guard"})
                same = 0
                continue
            try:
                d = self.navigator.decide(role, goal, page, allowed_click, fillable, history,
                                          sorted({(s.analysis or {}).get("screen_name", s.title) for s in self.state.screens.values() if role in s.roles}),
                                          tried_ids, self.state.known_values,
                                          [(elements[i].get("text") or "")[:40] for i in blocked_ids][:40])
            except RuntimeError as exc:
                errors += 1
                self.state.errors.append({"role": role, "step": steps, "error": str(exc)[:300]})
                if errors > 10:
                    break
                continue
            record = {"step": steps, "role": role, "goal": goal["goal_id"], "screen": screen.sig, "action": d["action"],
                      "element": (elements.get(d["element_id"]) or {}).get("text", ""), "intent": d["intent"], "result": ""}
            try:
                if d["action"] in ("goal_done", "goal_unreachable"):
                    plan["done"].append(goal["goal_id"])
                    goal["status"] = d["action"]
                    goals.pop(0)
                    record["result"] = d["action"]
                elif d["action"] == "role_done":
                    goals = []
                    replans = 3
                    record["result"] = "role done"
                elif d["action"] == "back":
                    b.back()
                elif d["action"] == "home":
                    b.goto(home_url)
                else:
                    element = elements.get(d["element_id"])
                    if element is None:
                        record["result"] = "invalid element"
                    else:
                        seen.add(self._key(element))
                        clicked_labels.add((element.get("text") or "")[:60])
                        ok, reason = self._allowed(role, screen, obs, element)
                        if not ok:
                            record["result"] = f"blocked: {reason}"
                        else:
                            if d["action"] == "fill_and_click":
                                for inp in d["inputs"]:
                                    target = elements.get(inp["element_id"])
                                    if not target or target["kind"] != "field" or target.get("readonly"):
                                        continue
                                    if target.get("type") == "select":
                                        b.select(target["id"], inp["value"])
                                    elif target.get("type") in ("checkbox", "radio"):
                                        b.click(target["id"])
                                    else:
                                        b.fill(target["id"], inp["value"])
                            b.click(element["id"])
                            popup = b.handle_popups()
                            new_obs = b.observe()
                            new_sig = screen_signature(new_obs, self.base)
                            self.state.transitions.append({"role": role, "from": screen.sig, "to": new_sig, "via": record["element"][:80],
                                                           "action": d["action"], "popup": bool(popup)})
                            record["result"] = "new screen" if new_sig not in self.state.screens else (
                                "same screen" if new_sig == screen.sig else "known screen")
                errors = 0
            except Exception as exc:  # noqa: BLE001 - element vanished, timeout, navigation error
                errors += 1
                record["result"] = f"error: {str(exc)[:160]}"
                if errors > 5:
                    b.goto(home_url)
                if errors > 15:
                    break
            history.append({k: record[k] for k in ("step", "action", "element", "result")})
            self.state.steps.append(record)
            if steps % 5 == 0:
                self._save()
        self.state.role_done[role] = True
        log.info("[%s] exploration finished: %d steps, %d screens", role, steps,
                 sum(1 for s in self.state.screens.values() if role in s.roles))

    # ================================================================ phase 2: access check
    def access_check(self, b, role: str) -> None:
        targets: dict[str, str] = {}
        for s in self.state.screens.values():
            if (s.analysis or {}).get("screen_type") in ("login", "error"):
                continue
            targets.setdefault(s.url_pattern, s.sample_url)
        root = self.base.rstrip("/")
        for d in self.declared:
            pattern = url_pattern(root + d, self.base)
            if pattern not in targets and self.policy.check({"kind": "link", "text": "", "href": root + d})[0] == "allow":
                targets[pattern] = root + d
        for pattern, url in sorted(targets.items()):
            if time.monotonic() > self.deadline:
                break
            reached = any(role in s.roles for s in self.state.screens.values() if s.url_pattern == pattern)
            if reached:
                self.state.access.setdefault(pattern, {})[role] = {"outcome": "allowed", "evidence": "reached by exploration", "via": "crawl"}
                continue
            try:
                b.goto(url)
                b.close_popup()
                obs = b.observe()
                page, _, _ = condense(obs, self.base)
                verdict = self.judge.judge(role, pattern, url_pattern(obs["url"], self.base), pattern, obs, page)
                if verdict["outcome"] == "allowed":
                    shot = self.out / "screenshots" / role / f"direct_{hashlib.sha1(pattern.encode()).hexdigest()[:10]}.png"
                    b.screenshot(shot)
                    verdict["screenshot"] = str(shot.relative_to(self.out))
                if obs.get("has_password"):
                    spec = self.cfg["roles"][role]
                    self.login_agent.login(b, role, spec.get("username", ""), role_password(spec))
            except Exception as exc:  # noqa: BLE001
                verdict = {"outcome": "error", "evidence": str(exc)[:200]}
            self.state.access.setdefault(pattern, {})[role] = {**verdict, "via": "direct"}
        self._save()

    # ================================================================ phase 3: validation probing (opt-in)
    def probe(self, b, role: str) -> None:
        max_cases = int(self.cfg["limits"]["max_probe_cases_per_form"])
        for s in list(self.state.screens.values()):
            if s.first_role != role or (s.analysis or {}).get("screen_type") not in ("form_create", "form_edit", "search"):
                continue
            try:
                b.goto(s.sample_url)
                obs = b.observe()
                page, clickable, fillable = condense(obs, self.base)
                elements = {e["id"]: e for e in obs["elements"]}
                buttons = [i for i in clickable if elements[i]["kind"] == "button"]
                cases = self.prober.design(page, fillable, buttons, max_cases)["cases"]
            except Exception as exc:  # noqa: BLE001
                self.state.errors.append({"role": role, "phase": "probe", "screen": s.sig, "error": str(exc)[:200]})
                continue
            for case in cases[:max_cases]:
                try:
                    b.goto(s.sample_url)
                    obs = b.observe()
                    elements = {e["id"]: e for e in obs["elements"]}
                    submit = elements.get(case["submit_element_id"])
                    if not submit or not self._allowed(role, s, obs, submit)[0]:
                        continue
                    for inp in case["inputs"]:
                        target = elements.get(inp["element_id"])
                        if target and target["kind"] == "field" and not target.get("readonly"):
                            (b.select if target.get("type") == "select" else b.fill)(target["id"], inp["value"])
                    before_dialogs = len(b.dialogs)
                    b.click(submit["id"])
                    after = b.observe()
                    self.state.probes.append({"role": role, "screen": (s.analysis or {}).get("screen_name", s.title), "case": case["name"],
                                              "inputs": case["inputs"], "expectation": case["expectation"],
                                              "messages": after.get("messages", [])[:10],
                                              "dialogs": [d["text"] for d in b.dialogs[before_dialogs:]],
                                              "landed": url_pattern(after["url"], self.base)})
                except Exception as exc:  # noqa: BLE001
                    self.state.errors.append({"role": role, "phase": "probe", "case": case.get("name"), "error": str(exc)[:200]})
            self._save()

    # ================================================================ comparison & report inputs
    def matrix(self) -> list[dict]:
        rows = []
        menus = {r: set() for r in self.cfg["roles"]}
        for s in self.state.screens.values():
            for r, entry in s.roles.items():
                menus.setdefault(r, set()).update(entry.get("menu_links", []))
        for s in sorted(self.state.screens.values(), key=lambda x: x.url_pattern):
            name = (s.analysis or {}).get("screen_name", s.title)
            row = {"screen": name, "url": s.url_pattern, "module": (s.analysis or {}).get("business_module", ""),
                   "type": (s.analysis or {}).get("screen_type", "")}
            for r in self.cfg["roles"]:
                direct = self.state.access.get(s.url_pattern, {}).get(r, {})
                row[r] = {"crawl": r in s.roles, "direct": direct.get("outcome", ""),
                          "actions": s.roles.get(r, {}).get("actions_visible", [])[:25],
                          "blocked": sorted({x["element"] for x in self.state.blocked if x["role"] == r and x["screen"] == s.sig})[:20]}
            rows.append(row)
        for pattern, by_role in self.state.access.items():
            if not any(s.url_pattern == pattern for s in self.state.screens.values()):
                rows.append({"screen": "(declared, not reached by exploration)", "url": pattern, "module": "", "type": "",
                             **{r: {"crawl": False, "direct": by_role.get(r, {}).get("outcome", ""), "actions": [], "blocked": []}
                                for r in self.cfg["roles"]}})
        return rows

    def screen_summaries(self) -> list[dict]:
        return [{"screen": (s.analysis or {}).get("screen_name", s.title), "url": s.url_pattern, "roles": sorted(s.roles),
                 **{k: (s.analysis or {}).get(k) for k in ("purpose", "business_module", "screen_type", "visible_rules", "messages")},
                 "fields": [f["label"] or f["name"] for f in (s.analysis or {}).get("fields", [])][:30],
                 "actions": [a["label"] for a in (s.analysis or {}).get("actions", [])][:20]}
                for s in self.state.screens.values()]

    def coverage(self) -> dict:
        per_role = {r: sum(1 for s in self.state.screens.values() if r in s.roles) for r in self.cfg["roles"]}
        reached_patterns = {s.url_pattern.split("?")[0] for s in self.state.screens.values()}
        declared = [url_pattern(self.base.rstrip("/") + d, self.base).split("?")[0] for d in self.declared]
        missed = sorted({d for d in declared if d not in reached_patterns})
        return {"screens_total": len(self.state.screens), "screens_per_role": per_role,
                "declared_paths": len(declared), "declared_not_reached": missed,
                "steps": dict(Counter(s["role"] for s in self.state.steps)),
                "blocked_actions": len(self.state.blocked), "errors": len(self.state.errors),
                "llm_calls": self.llm.calls, "prompt_tokens": self.llm.prompt_tokens, "completion_tokens": self.llm.completion_tokens}
