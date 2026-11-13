"""Login agent: signs a role in, using configured selectors or letting the model pick the login controls."""
from __future__ import annotations

import logging

from uiagent.agents import STR, Agent, enum, obj
from uiagent.browser import Browser
from uiagent.state import condense

log = logging.getLogger("uiagent.login")


class LoginAgent(Agent):
    name = "Login Agent"
    role_text = "You identify the username field, password field and sign-in button on a login page."

    def login(self, browser: Browser, role: str, username: str, password: str) -> bool:
        cfg_login = self.cfg["login"]
        url = self.cfg["app"].get("login_url") or self.cfg["app"]["base_url"]
        browser.goto(url)
        if cfg_login["mode"] in ("sso", "none"):
            return not browser.observe().get("has_password")
        obs = browser.observe()
        if not obs.get("has_password"):
            log.info("%s: no login form at %s (already signed in?)", role, url)
            return True
        page, clickable, fillable = condense(obs, self.cfg["app"]["base_url"])
        by_selector = self._by_selectors(obs)
        if by_selector:
            user_id, pass_id, submit_id = by_selector
        else:
            choice = self.ask(f"LOGIN PAGE\n{page}", "login_controls", obj({
                "username_id": enum(fillable + ["none"]), "password_id": enum(fillable + ["none"]),
                "submit_id": enum(clickable + ["none"]), "notes": STR}), max_tokens=400)
            user_id, pass_id, submit_id = choice["username_id"], choice["password_id"], choice["submit_id"]
        if "none" in (user_id, pass_id):
            log.error("%s: could not identify login fields", role)
            return False
        browser.fill(user_id, username)
        browser.fill(pass_id, password)
        if submit_id != "none":
            browser.click(submit_id)
        after = browser.observe()
        ok = not after.get("has_password")
        success_text = cfg_login.get("success_text")
        if success_text:
            ok = ok and success_text.lower() in after.get("text", "").lower()
        log.info("login %s: %s", role, "ok" if ok else "FAILED")
        return ok

    def _by_selectors(self, obs: dict) -> tuple[str, str, str] | None:
        c = self.cfg["login"]
        if not (c.get("username_selector") and c.get("password_selector")):
            return None

        def find(name: str | None) -> str:
            for e in obs["elements"]:
                if name and name in (e.get("name"), e.get("id"), e.get("text")):
                    return e["id"]
            return "none"
        return find(c["username_selector"]), find(c["password_selector"]), find(c.get("submit_selector"))
