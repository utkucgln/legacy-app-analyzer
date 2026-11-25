"""Browser abstraction shared by the Edge IE-mode (Selenium) and Edge (Playwright) engines."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

# ES3-only so it runs in IE5/7/8 document modes inside Edge IE mode: no querySelector, map/filter, JSON or arrow functions.
OBSERVE_JS = r"""
function (prefix) {
  var out = {title: '' + (document.title || ''), url: '' + location.href, headings: [], messages: [], elements: [], tables: [],
             forms: [], text: '', has_password: false, frames: 0};
  var n = 0, i, j, el;
  function clean(s) { return ('' + (s || '')).replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, ''); }
  function txt(e) { var t = e.innerText; if (t === undefined || t === null) { t = e.textContent; } if (!t) { t = e.value || e.title || e.alt || ''; } return clean(t).substring(0, 120); }
  function vis(e) { return !!(e.offsetWidth || e.offsetHeight); }
  function attr(e, a) { var v = null; try { v = e.getAttribute(a); } catch (x) {} return v === null || v === undefined ? '' : '' + v; }
  function navLike(e) {
    var p = e, d = 0;
    while (p && d < 10) {
      var tn = (p.tagName || '').toLowerCase(), cid = ((p.id || '') + ' ' + (p.className || '')).toLowerCase();
      if (tn == 'nav' || /menu|nav|sidebar|topbar|header|tab/.test(cid)) { return true; }
      p = p.parentNode; d++;
    }
    return false;
  }
  function formIndex(e) { var f = e.form; if (!f) { return -1; } for (var k = 0; k < document.forms.length; k++) { if (document.forms[k] == f) { return k; } } return -1; }
  function labelFor(e) {
    var lbls = document.getElementsByTagName('label'), k;
    if (e.id) { for (k = 0; k < lbls.length; k++) { if (lbls[k].htmlFor == e.id) { return txt(lbls[k]); } } }
    var p = e.parentNode;
    while (p && p.tagName && p.tagName.toLowerCase() != 'td' && p.tagName.toLowerCase() != 'label' && p.tagName.toLowerCase() != 'body') { p = p.parentNode; }
    if (p && p.tagName && p.tagName.toLowerCase() == 'label') { return txt(p); }
    if (p && p.tagName && p.tagName.toLowerCase() == 'td') {
      var prev = p.previousSibling;
      while (prev && prev.nodeType != 1) { prev = prev.previousSibling; }
      if (prev) { return txt(prev); }
    }
    return attr(e, 'placeholder') || attr(e, 'title');
  }
  function add(e, kind, info) {
    n++; var id = prefix + 'e' + n;
    try { e.setAttribute('data-uia', id); } catch (x) {}
    info.id = id; info.kind = kind; info.tag = (e.tagName || '').toLowerCase(); info.visible = vis(e); info.nav = navLike(e);
    info.form = formIndex(e); info.disabled = !!e.disabled; out.elements.push(info);
  }
  var links = document.getElementsByTagName('a');
  for (i = 0; i < links.length && n < 600; i++) {
    el = links[i]; var href = attr(el, 'href'), oc = attr(el, 'onclick');
    if (!href && !oc) { continue; }
    add(el, 'link', {text: txt(el) || attr(el, 'title'), href: href ? '' + el.href : '', onclick: oc.substring(0, 160), target: attr(el, 'target')});
  }
  var inputs = document.getElementsByTagName('input');
  for (i = 0; i < inputs.length && n < 900; i++) {
    el = inputs[i]; var t = (attr(el, 'type') || 'text').toLowerCase();
    if (t == 'submit' || t == 'button' || t == 'image' || t == 'reset') {
      add(el, 'button', {text: clean(el.value || attr(el, 'alt') || attr(el, 'title')), name: attr(el, 'name'), type: t, onclick: attr(el, 'onclick').substring(0, 160)});
    } else if (t == 'hidden') {
      add(el, 'hidden', {name: attr(el, 'name'), type: t});
    } else {
      if (t == 'password') { out.has_password = out.has_password || vis(el); }
      add(el, 'field', {name: attr(el, 'name'), type: t, label: labelFor(el), value: t == 'password' ? '' : clean(el.value).substring(0, 60),
                        checked: !!el.checked, readonly: !!el.readOnly, maxlength: el.maxLength > 0 && el.maxLength < 100000 ? el.maxLength : null});
    }
  }
  var selects = document.getElementsByTagName('select');
  for (i = 0; i < selects.length && n < 1000; i++) {
    el = selects[i]; var opts = [];
    for (j = 0; j < el.options.length && j < 40; j++) { opts.push({value: '' + el.options[j].value, text: clean(el.options[j].text).substring(0, 60)}); }
    add(el, 'field', {name: attr(el, 'name'), type: 'select', label: labelFor(el), value: el.selectedIndex >= 0 && el.options.length ? clean(el.options[el.selectedIndex].text) : '', options: opts});
  }
  var areas = document.getElementsByTagName('textarea');
  for (i = 0; i < areas.length; i++) { el = areas[i]; add(el, 'field', {name: attr(el, 'name'), type: 'textarea', label: labelFor(el), value: clean(el.value).substring(0, 60)}); }
  var buttons = document.getElementsByTagName('button');
  for (i = 0; i < buttons.length; i++) { el = buttons[i]; add(el, 'button', {text: txt(el), name: attr(el, 'name'), type: (attr(el, 'type') || 'submit').toLowerCase(), onclick: attr(el, 'onclick').substring(0, 160)}); }
  var tags = ['td', 'div', 'span', 'img', 'li', 'tr'];
  for (j = 0; j < tags.length; j++) {
    var col = document.getElementsByTagName(tags[j]);
    for (i = 0; i < col.length && n < 1200; i++) {
      el = col[i]; var c = attr(el, 'onclick');
      if (c) { add(el, 'clickable', {text: txt(el) || attr(el, 'alt') || attr(el, 'title'), onclick: c.substring(0, 160)}); }
    }
  }
  for (i = 0; i < document.forms.length; i++) {
    var f = document.forms[i];
    out.forms.push({index: i, name: attr(f, 'name') || attr(f, 'id'), action: '' + (f.action || ''), method: (attr(f, 'method') || 'get').toLowerCase()});
  }
  var hs = ['h1', 'h2', 'h3', 'legend', 'caption'];
  for (j = 0; j < hs.length; j++) { var hc = document.getElementsByTagName(hs[j]); for (i = 0; i < hc.length && i < 10; i++) { var ht = txt(hc[i]); if (ht) { out.headings.push(ht); } } }
  var mt = ['div', 'span', 'td', 'li', 'p', 'font', 'b'];
  for (j = 0; j < mt.length && out.messages.length < 20; j++) {
    var mc = document.getElementsByTagName(mt[j]);
    for (i = 0; i < mc.length && i < 3000 && out.messages.length < 20; i++) {
      el = mc[i]; var cls = ((el.className || '') + ' ' + (el.id || '')).toLowerCase(), color = (attr(el, 'color') || '').toLowerCase();
      if ((/error|message|msg|alert|warn|info|success|notice|valid/.test(cls) || color == 'red' || color == '#ff0000') && vis(el)) {
        var m = txt(el); if (m && m.length > 2) { out.messages.push(m); }
      }
    }
  }
  var tables = document.getElementsByTagName('table');
  for (i = 0; i < tables.length && out.tables.length < 12; i++) {
    var tb = tables[i], rows = tb.rows; if (!rows || rows.length < 2) { continue; }
    var head = [], ths = tb.getElementsByTagName('th');
    for (j = 0; j < ths.length && j < 30; j++) { head.push(txt(ths[j])); }
    var sample = [];
    for (j = (ths.length ? 1 : 0); j < rows.length && sample.length < 3; j++) {
      var cells = rows[j].cells, r = [];
      for (var k = 0; k < cells.length && k < 12; k++) { r.push(txt(cells[k]).substring(0, 40)); }
      if (r.length > 1) { sample.push(r); }
    }
    if (head.length || sample.length) { out.tables.push({headers: head, rows: rows.length, sample: sample}); }
  }
  out.frames = window.frames ? window.frames.length : 0;
  var body = document.body;
  out.text = body ? clean(body.innerText || body.textContent || '').substring(0, 3000) : '';
  return out;
}
"""

FRAME_ID = re.compile(r"^f([\d.]+)\.(e\d+)$")


def split_element_id(element_id: str) -> tuple[list[int], str]:
    m = FRAME_ID.match(element_id)
    if not m:
        return [], element_id
    return [int(x) for x in m.group(1).split(".")], m.group(2)


class Browser(ABC):
    """Engine-independent operations used by the Executor agent."""

    engine = "base"
    dialogs: list[dict]

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def goto(self, url: str) -> None: ...

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def observe_frame(self, path: list[int]) -> dict | None: ...

    @abstractmethod
    def frame_count(self, path: list[int]) -> int: ...

    @abstractmethod
    def click(self, element_id: str) -> None: ...

    @abstractmethod
    def fill(self, element_id: str, value: str) -> None: ...

    @abstractmethod
    def select(self, element_id: str, value: str) -> None: ...

    @abstractmethod
    def screenshot(self, path: Path) -> None: ...

    @abstractmethod
    def back(self) -> None: ...

    @abstractmethod
    def session_ids(self) -> list[str]: ...

    @abstractmethod
    def handle_popups(self) -> str | None:
        """Switch to a newly opened window if any; returns its URL."""

    @abstractmethod
    def close_popup(self) -> bool: ...

    @abstractmethod
    def eval_frame(self, path: list[int], source: str, arg) -> dict | None:
        """Run `source` (a JS function expression) with `arg` in the frame at `path`."""

    def extract_design(self) -> dict:
        """Design extract of the top document and up to two frame levels (framesets are common in legacy apps)."""
        from uiagent.browser.design_js import DESIGN_JS

        parts = []
        for path in [[]] + [[i] for i in range(min(self.frame_count([]), 8))]:
            got = self.eval_frame(path, DESIGN_JS, {})
            if got:
                parts.append(got)
            if path:
                for j in range(min(self.frame_count(path), 6)):
                    sub = self.eval_frame(path + [j], DESIGN_JS, {})
                    if sub:
                        parts.append(sub)
        return {"frames": parts}

    def observe(self) -> dict:
        """Merge the top document and up to two levels of frames into one observation."""
        top = self.observe_frame([]) or {"title": "", "url": self.current_url(), "headings": [], "messages": [], "elements": [],
                                         "tables": [], "forms": [], "text": "", "has_password": False}
        frames: list[dict] = []
        for i in range(min(self.frame_count([]), 8)):
            sub = self.observe_frame([i])
            if sub:
                frames.append({"path": [i], **sub})
                for j in range(min(self.frame_count([i]), 6)):
                    sub2 = self.observe_frame([i, j])
                    if sub2:
                        frames.append({"path": [i, j], **sub2})
        merged = dict(top)
        merged["frame_urls"] = [f["url"] for f in frames]
        for f in frames:
            merged["elements"] += f["elements"]
            merged["headings"] += f["headings"]
            merged["messages"] += f["messages"]
            merged["tables"] += f["tables"]
            merged["has_password"] = merged["has_password"] or f["has_password"]
            merged["text"] = (merged["text"] + " | " + f["text"])[:5000]
        return merged

    @staticmethod
    def frame_prefix(path: list[int]) -> str:
        return "f" + ".".join(str(p) for p in path) + "." if path else ""


def create(cfg: dict) -> Browser:
    engine = cfg["browser"]["engine"]
    cfg["browser"].setdefault("initial_url", cfg["app"].get("login_url") or cfg["app"]["base_url"])
    if engine == "ie":
        from uiagent.browser.iemode import IEModeBrowser

        return IEModeBrowser(cfg["browser"])
    from uiagent.browser.edge import EdgeBrowser

    return EdgeBrowser(cfg["browser"])
