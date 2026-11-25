"""Deterministic safety policy. The Guard agent is only consulted when this policy says 'ask'."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _has_word(text: str, words: list[str]) -> str:
    low = (text or "").lower()
    for w in words:
        w = w.lower()
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) if w.isascii() else w in low:
            return w
    return ""


class Policy:
    def __init__(self, cfg: dict) -> None:
        self.s = cfg["safety"]
        self.mode = self.s["mode"]
        self.base = urlsplit(cfg["app"]["base_url"])
        self.prefixes = cfg["app"].get("scope_prefixes") or [self.base.path.rstrip("/") + "/"]

    def in_scope(self, url: str) -> bool:
        if not url or url.lower().startswith(("javascript:", "#")):
            return True
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or p.netloc.lower() != self.base.netloc.lower():
            return False
        return any(p.path.startswith(pref) for pref in self.prefixes)

    def check(self, element: dict, form: dict | None = None) -> tuple[str, str]:
        """Return ('allow' | 'deny' | 'ask', reason) for clicking an element."""
        label = " ".join(str(element.get(k) or "") for k in ("text", "name", "onclick"))
        href = element.get("href") or ""
        if href and not self.in_scope(href):
            return "deny", "out of scope"
        for pat in self.s["deny_url_patterns"]:
            if re.search(pat, href, re.I):
                return "deny", f"url matches {pat}"
        destructive = _has_word(label + " " + href, ["delete", "remove", "purge", "terminate", "حذف"])
        if destructive and self.mode != "full":
            return "deny", f"destructive word '{destructive}'"
        if self.mode == "full":
            return ("deny", f"destructive word '{destructive}'") if destructive else ("allow", "full mode")
        hit = _has_word(label + " " + href, self.s["deny_words"])
        script_submit = bool(re.search(r"submit\s*\(|\.action\s*=", element.get("onclick") or "", re.I))
        submits = element["kind"] == "button" or script_submit
        if hit and not (self.mode == "probe" and submits):
            return "deny", f"action word '{hit}'"
        if element["kind"] in ("link", "clickable") and not script_submit:
            return "allow", "navigation"
        if submits:
            if form and form.get("method") == "get":
                return "allow", "GET form (query)"
            if _has_word(label, self.s["query_words"]):
                return "allow", "query button"
            if element.get("type") == "reset":
                return "deny", "reset button has no analysis value"
            return ("allow", "probe mode submission") if self.mode == "probe" else ("ask", "form submission with unclear effect")
        return "allow", "default"
