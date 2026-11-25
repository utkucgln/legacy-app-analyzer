"""Parsers for deployment descriptors, configuration files, JSPs and templates found in a WAR/EAR."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

from legacy_decompiler.common import SECRET_NAME, mask

CRON_RE = re.compile(r"^\s*(?:[0-9*?/,\-LW#A-Z]+\s+){5,6}[0-9*?/,\-LW#A-Z]+\s*$")


def is_cron(text: str) -> bool:
    return bool(CRON_RE.match(text or "")) and any(c in text for c in "*?") and len(text.split()) in (6, 7)


# ---------------------------------------------------------------- xml helpers

def _strip_ns(elem) -> None:
    for el in elem.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def load_xml(path: Path):
    root = ET.parse(str(path)).getroot()
    _strip_ns(root)
    return root


def _text(elem, tag: str) -> str:
    found = elem.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def root_tag(path: Path) -> str:
    try:
        return load_xml(path).tag
    except Exception:  # noqa: BLE001 - unparseable XML is reported as unknown
        return ""


# ---------------------------------------------------------------- web.xml

def parse_web_xml(path: Path) -> dict:
    root = load_xml(path)
    servlets = [{"name": _text(s, "servlet-name"), "class": _text(s, "servlet-class"),
                 "init": {_text(p, "param-name"): _text(p, "param-value") for p in s.findall("init-param")},
                 "load_on_startup": _text(s, "load-on-startup")} for s in root.findall("servlet")]
    mappings = [{"servlet": _text(m, "servlet-name"), "patterns": [u.text.strip() for u in m.findall("url-pattern") if u.text]}
                for m in root.findall("servlet-mapping")]
    filters = [{"name": _text(f, "filter-name"), "class": _text(f, "filter-class"),
                "init": {_text(p, "param-name"): _text(p, "param-value") for p in f.findall("init-param")}}
               for f in root.findall("filter")]
    filter_maps = [{"filter": _text(m, "filter-name"), "patterns": [u.text.strip() for u in m.findall("url-pattern") if u.text],
                    "servlets": [s.text.strip() for s in m.findall("servlet-name") if s.text]} for m in root.findall("filter-mapping")]
    constraints = []
    for sc in root.findall("security-constraint"):
        constraints.append({
            "resources": [u.text.strip() for w in sc.findall("web-resource-collection") for u in w.findall("url-pattern") if u.text],
            "methods": [m.text.strip() for w in sc.findall("web-resource-collection") for m in w.findall("http-method") if m.text],
            "roles": [r.text.strip() for a in sc.findall("auth-constraint") for r in a.findall("role-name") if r.text],
            "transport": _text(sc.find("user-data-constraint"), "transport-guarantee") if sc.find("user-data-constraint") is not None else "",
        })
    login = root.find("login-config")
    return {
        "display_name": _text(root, "display-name"),
        "context_params": {_text(p, "param-name"): _mask_if_secret(_text(p, "param-name"), _text(p, "param-value"))
                           for p in root.findall("context-param")},
        "listeners": [_text(lst, "listener-class") for lst in root.findall("listener")],
        "servlets": servlets, "servlet_mappings": mappings, "filters": filters, "filter_mappings": filter_maps,
        "security_constraints": constraints,
        "security_roles": [_text(r, "role-name") for r in root.findall("security-role")],
        "login_config": {"auth_method": _text(login, "auth-method"), "realm": _text(login, "realm-name")} if login is not None else {},
        "session_timeout_min": _text(root.find("session-config"), "session-timeout") if root.find("session-config") is not None else "",
        "error_pages": [{"code": _text(e, "error-code"), "exception": _text(e, "exception-type"), "location": _text(e, "location")}
                        for e in root.findall("error-page")],
        "welcome_files": [w.text.strip() for wl in root.findall("welcome-file-list") for w in wl.findall("welcome-file") if w.text],
    }


def _mask_if_secret(name: str, value: str) -> str:
    return mask(value) if SECRET_NAME.search(name or "") and value else value


# ---------------------------------------------------------------- struts 1

def parse_struts_config(path: Path) -> dict:
    root = load_xml(path)
    forms = []
    for fb in root.iter("form-bean"):
        forms.append({"name": fb.get("name"), "type": fb.get("type"),
                      "properties": [{"name": p.get("name"), "type": p.get("type"), "initial": p.get("initial")}
                                     for p in fb.findall("form-property")]})
    actions = []
    for a in root.iter("action"):
        actions.append({
            "path": a.get("path"), "type": a.get("type"), "form": a.get("name"), "scope": a.get("scope"),
            "roles": [r.strip() for r in (a.get("roles") or "").split(",") if r.strip()],
            "validate": a.get("validate"), "input": a.get("input"), "parameter": a.get("parameter"),
            "forward_attr": a.get("forward"), "include": a.get("include"),
            "forwards": [{"name": f.get("name"), "path": f.get("path"), "redirect": f.get("redirect")} for f in a.findall("forward")],
            "exceptions": [{"type": e.get("type"), "key": e.get("key"), "path": e.get("path")} for e in a.findall("exception")],
        })
    gforwards = [{"name": f.get("name"), "path": f.get("path")} for g in root.findall("global-forwards") for f in g.findall("forward")]
    gexc = [{"type": e.get("type"), "key": e.get("key"), "path": e.get("path")} for g in root.findall("global-exceptions") for e in g.findall("exception")]
    plugins = [{"class": p.get("className"), "properties": {s.get("property"): s.get("value") for s in p.findall("set-property")}}
               for p in root.iter("plug-in")]
    controller = root.find("controller")
    return {"form_beans": forms, "actions": actions, "global_forwards": gforwards, "global_exceptions": gexc,
            "message_resources": [m.get("parameter") for m in root.iter("message-resources")],
            "plugins": plugins, "controller": dict(controller.attrib) if controller is not None else {}}


def parse_validation_xml(path: Path) -> dict:
    root = load_xml(path)
    constants = {_text(c, "constant-name"): _text(c, "constant-value") for g in root.iter("global") for c in g.findall("constant")}
    rules = []
    for form in root.iter("form"):
        for fld in form.findall("field"):
            vars_ = {}
            for v in fld.findall("var"):
                value = _text(v, "var-value")
                ref = re.fullmatch(r"\$\{(\w+)\}", value)
                vars_[_text(v, "var-name")] = constants.get(ref.group(1), value) if ref else value
            rules.append({"form": form.get("name"), "field": fld.get("property"),
                          "depends": [d.strip() for d in (fld.get("depends") or "").split(",") if d.strip()],
                          "vars": vars_,
                          "msgs": {m.get("name"): m.get("key") for m in fld.findall("msg")},
                          "args": [a.get("key") for a in fld if a.tag.startswith("arg")]})
    validators = [{"name": v.get("name"), "class": v.get("classname"), "method": v.get("method"), "msg": v.get("msg")}
                  for v in root.iter("validator")]
    return {"constants": constants, "rules": rules, "validators": validators}


def parse_tiles(path: Path) -> list[dict]:
    root = load_xml(path)
    return [{"name": d.get("name"), "path": d.get("path") or d.get("template"), "extends": d.get("extends"),
             "puts": {p.get("name"): p.get("value") for p in d.findall("put")}} for d in root.iter("definition")]


# ---------------------------------------------------------------- hibernate / jpa / spring / quartz

def parse_hbm(path: Path) -> list[dict]:
    root = load_xml(path)
    pkg = root.get("package") or ""
    out = []
    for cls in root.iter("class"):
        name = cls.get("name") or ""
        entity = {"class": f"{pkg}.{name}" if pkg and "." not in name else name, "table": cls.get("table") or name.rsplit(".", 1)[-1],
                  "id": None, "properties": [], "relations": [], "version": None, "file": path.name}
        idel = cls.find("id")
        if idel is not None:
            gen = idel.find("generator")
            entity["id"] = {"name": idel.get("name"), "column": idel.get("column") or (_col(idel) or idel.get("name")),
                            "type": idel.get("type"), "generator": gen.get("class") if gen is not None else None}
        for p in cls.findall("property"):
            entity["properties"].append({"name": p.get("name"), "column": p.get("column") or _col(p) or p.get("name"),
                                         "type": p.get("type"), "length": p.get("length"), "not_null": p.get("not-null"),
                                         "unique": p.get("unique"), "update": p.get("update"), "insert": p.get("insert"),
                                         "formula": p.get("formula")})
        for tag in ("many-to-one", "one-to-one", "set", "bag", "list", "map", "one-to-many", "many-to-many", "component"):
            for r in cls.iter(tag):
                entity["relations"].append({"kind": tag, "name": r.get("name"), "class": r.get("class"),
                                            "column": r.get("column") or _col(r), "table": r.get("table")})
        ver = cls.find("version") if cls.find("version") is not None else cls.find("timestamp")
        if ver is not None:
            entity["version"] = ver.get("column") or ver.get("name")
        out.append(entity)
    for q in root.iter("query"):
        out.append({"named_query": q.get("name"), "hql": (q.text or "").strip(), "file": path.name})
    for q in root.iter("sql-query"):
        out.append({"named_query": q.get("name"), "sql": (q.text or "").strip(), "file": path.name})
    return out


def _col(elem) -> str | None:
    c = elem.find("column")
    return c.get("name") if c is not None else None


def parse_hibernate_cfg(path: Path) -> dict:
    root = load_xml(path)
    props = {}
    for p in root.iter("property"):
        name = p.get("name") or ""
        props[name] = _mask_if_secret(name, (p.text or "").strip())
    return {"properties": props, "mappings": [m.get("resource") or m.get("class") for m in root.iter("mapping")]}


def parse_spring(path: Path) -> list[dict]:
    root = load_xml(path)
    beans = []
    for b in root.iter("bean"):
        beans.append({"id": b.get("id") or b.get("name"), "class": b.get("class"),
                      "properties": {p.get("name"): _mask_if_secret(p.get("name") or "", p.get("value") or p.get("ref") or "")
                                     for p in b.findall("property")}})
    return beans


def parse_quartz_jobs(path: Path) -> list[dict]:
    root = load_xml(path)
    jobs = []
    for job in root.iter("job-detail"):
        jobs.append({"name": _text(job, "name"), "class": _text(job, "job-class")})
    for job in root.iter("job"):
        jobs.append({"name": _text(job, "name"), "class": _text(job, "job-class")})
    for trig in root.iter("cron"):
        jobs.append({"trigger": _text(trig, "name"), "job": _text(trig, "job-name"), "cron": _text(trig, "cron-expression")})
    return jobs


def parse_persistence(path: Path) -> list[dict]:
    root = load_xml(path)
    return [{"unit": u.get("name"), "classes": [c.text for c in u.findall("class") if c.text]} for u in root.iter("persistence-unit")]


# ---------------------------------------------------------------- properties

def parse_properties(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    logical = ""
    for raw in text.splitlines():
        line = raw.strip() if not logical else raw.lstrip()
        if not logical and (not line or line[0] in "#!"):
            continue
        if line.endswith("\\") and not line.endswith("\\\\"):
            logical += line[:-1]
            continue
        logical += line
        m = re.match(r"((?:\\.|[^=:\s])+)\s*[=:\s]\s*(.*)$", logical)
        if m:
            key = _unescape(m.group(1))
            out[key] = _unescape(m.group(2))
        elif logical:
            out[_unescape(logical)] = ""
        logical = ""
    return out


def _unescape(text: str) -> str:
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), text)


def classify_property(key: str, value: str) -> str:
    k = key.lower()
    tokens = set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()))
    if SECRET_NAME.search(k):
        return "secret"
    if is_cron(value):
        return "schedule"
    if k.startswith(("log4j.", "logback.", "java.util.logging")):
        return "setting"
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) or value.startswith("jdbc:") or tokens & {"url", "uri", "endpoint", "host", "port", "wsdl", "queue", "topic"}:
        return "integration"
    if any(t.startswith(("timeout", "retry", "retries", "attempt", "interval", "delay", "threshold", "window", "limit", "percent")) for t in tokens) or tokens & {
            "max", "min", "days", "months", "hours", "minutes", "seconds", "rate", "pct", "amount", "fee", "ttl", "grace", "period", "ms"} \
            or any(t.endswith(("ms", "days", "pct", "rate", "limit", "max", "min")) and len(t) > 3 for t in tokens):
        return "parameter"
    if tokens & {"mail", "smtp", "from", "to", "cc", "subject", "template"}:
        return "messaging"
    return "setting"


# ---------------------------------------------------------------- JSP

_ATTR = r"""(\w[\w:-]*)\s*=\s*("([^"]*)"|'([^']*)')"""


def _attrs(tag_text: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in re.finditer(_ATTR, tag_text)}


def parse_jsp(text: str, messages: dict[str, str]) -> dict:
    result: dict[str, Any] = {"forms": [], "fields": [], "buttons": [], "links": [], "message_keys": [], "labels": [],
                              "role_checks": [], "el_literal_conditions": [], "options": [], "includes": [], "taglibs": []}
    for m in re.finditer(r"<%@\s*taglib\s+([^%]*)%>", text):
        a = _attrs(m.group(1))
        result["taglibs"].append(a.get("prefix", "") + "=" + a.get("uri", ""))
    for m in re.finditer(r"<(html:form|form)\b([^>]*)>", text, re.I):
        a = _attrs(m.group(2))
        result["forms"].append({"action": a.get("action", ""), "method": (a.get("method") or ("post" if m.group(1).lower() == "html:form" else "get")).lower(),
                                "name": a.get("name") or a.get("id") or ""})
    for m in re.finditer(r"<(html:(text|password|select|textarea|checkbox|multibox|radio|hidden|file)|input|select|textarea)\b([^>]*)>", text, re.I):
        a = _attrs(m.group(3))
        tag = m.group(1).lower()
        if tag == "input" and (a.get("type") or "text").lower() in ("submit", "button", "reset", "image"):
            result["buttons"].append({"text": a.get("value", ""), "name": a.get("name", ""), "type": a.get("type")})
            continue
        name = a.get("property") or a.get("name") or ""
        if name:
            result["fields"].append({"tag": tag, "name": name, "type": (a.get("type") or m.group(2) or tag).lower(),
                                     "maxlength": a.get("maxlength", ""), "readonly": "readonly" in a or "disabled" in a})
    for m in re.finditer(r"<html:(submit|cancel|button|reset)\b([^>]*?)(/>|>(.*?)</html:\1>)", text, re.I | re.S):
        a = _attrs(m.group(2))
        body = re.sub(r"<[^>]+>", "", m.group(4) or "").strip()
        key_m = re.search(r'key="([^"]+)"', m.group(4) or "")
        label = a.get("value") or (messages.get(key_m.group(1)) if key_m else "") or body or m.group(1)
        result["buttons"].append({"text": label, "name": a.get("property", ""), "type": m.group(1).lower()})
    for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", text, re.I | re.S):
        result["buttons"].append({"text": re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip(), "name": _attrs(m.group(1)).get("name", ""), "type": "button"})
    for m in re.finditer(r"""(?:href|action|page|src)\s*=\s*["']([^"'<>]*?\.(?:do|jsp|action)\b[^"'<>]*)["']""", text, re.I):
        result["links"].append(m.group(1))
    for m in re.finditer(r"<html:link\b([^>]*)>(.*?)</html:link>", text, re.I | re.S):
        a = _attrs(m.group(1))
        target = a.get("action") or a.get("page") or a.get("forward") or a.get("href") or ""
        result["links"].append(target + "  [" + re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip() + "]")
    for m in re.finditer(r"""<(?:bean|fmt):message\b[^>]*\bkey\s*=\s*["']([^"']+)["']""", text, re.I):
        result["message_keys"].append(m.group(1))
    for m in re.finditer(r"<(th|label|legend|h1|h2|h3|dt)\b[^>]*>(.*?)</\1>", text, re.I | re.S):
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        label = re.sub(r"\s+", " ", label).strip()
        if label and "${" not in label and "<%" not in label:
            result["labels"].append(label)
    for m in re.finditer(r"""(?:isUserInRole\(\s*["']([^"']+)|\brole\s*=\s*["']([^"']+)["'])""", text):
        result["role_checks"].append(m.group(1) or m.group(2))
    for m in re.finditer(r"\$\{([^}]*?(?:==|!=|\beq\b|\bne\b)\s*'[^']*'[^}]*)\}", text):
        result["el_literal_conditions"].append(m.group(1).strip())
    for m in re.finditer(r"""<(?:html:option|option)\b[^>]*\bvalue\s*=\s*["']([^"'$<]+)["']""", text, re.I):
        result["options"].append(m.group(1))
    for m in re.finditer(r"""<(?:jsp:include|tiles:insert|%@\s*include)\b[^>]*(?:page|file|definition|attribute)\s*=\s*["']([^"']+)["']""", text, re.I):
        result["includes"].append(m.group(1))
    result["labels"] += [messages[k] for k in result["message_keys"] if k in messages]
    for key in result:
        if isinstance(result[key], list) and result[key] and isinstance(result[key][0], str):
            result[key] = list(dict.fromkeys(result[key]))
    return result


# ---------------------------------------------------------------- templates

PLACEHOLDER_RE = re.compile(r"\$\{([\w.]+)\}|\{(\d+)\}|\{\{\s*([\w.]+)\s*\}\}|\$([a-zA-Z_][\w.]*)|%\(?(\w+)\)?s|#\{([\w.]+)\}")


def template_placeholders(text: str) -> list[str]:
    found = []
    for m in PLACEHOLDER_RE.finditer(text):
        found.append(next(g for g in m.groups() if g))
    return list(dict.fromkeys(found))
