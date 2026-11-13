"""Artifact analysis: everything recoverable from the deployed artifact (WAR/EAR/JAR) without source code."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from legacy_decompiler import descriptors as D
from legacy_decompiler.classfile import ACC_FINAL, ACC_STATIC, ClassFormatError, ClassInfo, parse_class
from legacy_decompiler.common import (
    SECRET_NAME, FactSink, Timer, ToolError, docker_available, docker_run, ensure_dir, jdk_tool, looks_secret_value,
    mask, norm_path, run_tool, sha256_file, split_words, write_csv, write_json,
)
from legacy_decompiler.decompile import JDK8_IMAGE, decompile, fidelity

log = logging.getLogger("legacy_decompiler.code")

BUSINESS_NAME = re.compile(
    r"(MIN|MAX|LIMIT|DAYS|MONTHS|YEARS|HOURS|MINUTES|RATE|PCT|PERCENT|THRESHOLD|WINDOW|RETRY|RETRIES|ATTEMPT|"
    r"TIMEOUT|INTERVAL|GRACE|PERIOD|AMOUNT|FEE|CAP|FLOOR|CEILING|AGE|TERM|DELAY|COUNT|STATUS|STATE|TYPE|CODE|DEFAULT)", re.I)
SQL_RE = re.compile(r"^\s*(select|insert|update|delete|merge|call|\{\s*\??=?\s*call|with|from\s+[A-Z]\w*)\b", re.I)
INTEGRATION_RE = re.compile(r"(://|^[A-Z][A-Z0-9.]{2,},[^,\s]*/|<\?xml|<[A-Za-z][\w:-]*>|^\{\s*\"|soapaction|wsdl|ofs|swift|iso8583)", re.I)
ENTRYPOINT_BASES = {
    "struts_action": ("org/apache/struts/action/Action", "org/apache/struts/actions/"),
    "servlet": ("javax/servlet/http/HttpServlet", "javax/servlet/GenericServlet"),
    "job": ("org/quartz/Job", "org/quartz/StatefulJob", "java/util/TimerTask"),
    "listener": ("javax/servlet/ServletContextListener", "javax/servlet/http/HttpSessionListener"),
    "filter": ("javax/servlet/Filter",),
}
EOL_LIBRARIES = [  # (artifact regex, max affected version exclusive or None for all, note) - generic public knowledge
    (r"^struts(-core)?$", None, "Apache Struts 1.x is end-of-life since 2013 (e.g. CVE-2014-0114, CVE-2016-1181)."),
    (r"^log4j$", (2,), "log4j 1.x is end-of-life since 2015 (CVE-2019-17571, CVE-2021-4104, CVE-2022-23305)."),
    (r"^commons-collections$", (3, 2, 2), "commons-collections < 3.2.2 allows deserialization gadget chains (CVE-2015-7501)."),
    (r"^commons-fileupload$", (1, 3, 3), "commons-fileupload < 1.3.3 (CVE-2016-1000031)."),
    (r"^commons-beanutils$", (1, 9, 4), "commons-beanutils < 1.9.4 (CVE-2019-10086, CVE-2014-0114)."),
    (r"^hibernate(-core|3)?$", (5,), "Hibernate 3.x/4.x are end-of-life."),
    (r"^quartz(-all)?$", (2, 3, 2), "Quartz < 2.3.2 (CVE-2019-13990 XXE)."),
    (r"^mysql-connector-java$", (8,), "MySQL Connector/J 5.1 is end-of-life (multiple CVEs)."),
    (r"^dom4j$", (2, 1, 3), "dom4j < 2.1.3 (CVE-2020-10683 XXE)."),
    (r"^xstream$", (1, 4, 18), "XStream < 1.4.18 (multiple RCE CVEs)."),
    (r"^jackson-databind$", (2, 9, 10), "jackson-databind < 2.9.10 (polymorphic deserialization CVEs)."),
    (r"^spring(-core|-web|-webmvc)?$", (5, 3), "Spring Framework < 5.3 is end-of-life."),
    (r"^(jstl|standard)$", (1, 2), "JSTL 1.1 (old Jakarta Taglibs, CVE-2015-0254 for < 1.2.3)."),
    (r"^c3p0$", (0, 9, 5, 4), "c3p0 < 0.9.5.4 (CVE-2019-5427 XXE)."),
]
# XML namespace / parser-feature URIs look like URLs but are not integrations
NAMESPACE_HOSTS = re.compile(r"^endpoint:([\w.-]+\.)?(w3\.org|apache\.org|xml\.org|sun\.com|oracle\.com|jcp\.org|xmlsoap\.org|purl\.org|openxmlformats\.org|springframework\.org|hibernate\.org)(/|$)", re.I)
VALUE_DESCRIPTORS = ("I", "J", "D", "F", "S", "B", "Z", "C", "Ljava/lang/String;", "Ljava/math/BigDecimal;", "Ljava/math/BigInteger;",
                     "Ljava/lang/Integer;", "Ljava/lang/Long;", "Ljava/util/Set;", "Ljava/util/List;", "Ljava/util/Collection;")
STATIC_EXT = {".css", ".js", ".png", ".gif", ".jpg", ".jpeg", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".eot", ".map"}
TEMPLATE_EXT = {".txt", ".vm", ".ftl", ".html", ".htm", ".tmpl", ".tpl", ".mustache", ".xsl", ".xslt", ".st", ".eml"}


def run(cfg: dict, out: Path, tools_dir: Path) -> dict:
    timer = Timer()
    artifact = Path(cfg["artifact"])
    ensure_dir(out)
    sink = FactSink("code")
    unpacked = out / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    _extract_archive(artifact, unpacked)
    for nested in list(unpacked.rglob("*.war")):
        _extract_archive(nested, nested.with_suffix(""))

    webroots = sorted({p.parent.parent for p in unpacked.rglob("WEB-INF/web.xml")}) or [unpacked]
    inventory = _inventory(unpacked)
    write_csv(out / "inventory.csv", inventory, ["path", "category", "size", "sha256"])
    sink.add("finding", f"Artifact {artifact.name} (sha256 {sha256_file(artifact)[:16]}...) contains {len(inventory)} files: "
             + ", ".join(f"{n} {c}" for c, n in Counter(r['category'] for r in inventory).most_common()),
             ["artifact:" + artifact.name], "inventory.csv")

    classes_dirs = [w / "WEB-INF" / "classes" for w in webroots if (w / "WEB-INF" / "classes").exists()]
    lib_dirs = [w / "WEB-INF" / "lib" for w in webroots if (w / "WEB-INF" / "lib").exists()]
    app_classes: dict[str, bytes] = {}
    for cdir in classes_dirs:
        for f in cdir.rglob("*.class"):
            app_classes[f.relative_to(cdir).as_posix()[:-6]] = f.read_bytes()
    prefix = cfg.get("app_package_prefix") or _detect_prefix(app_classes)
    libraries, app_jar_classes = _libraries(lib_dirs, prefix)
    app_classes.update(app_jar_classes)
    write_csv(out / "libraries.csv", libraries,
              ["jar", "group", "artifact", "version", "classes", "max_class_version", "sha256", "app_jar", "eol_note"])
    for lib in libraries:
        if lib["eol_note"]:
            sink.add("finding", f"Library {lib['jar']} ({lib['artifact']} {lib['version']}): {lib['eol_note']}",
                     ["library:" + lib["artifact"]], "libraries.csv", lib["jar"])

    # ---------------------------------------------------- classes
    infos: dict[str, ClassInfo] = {}
    parse_errors = []
    for name, data in sorted(app_classes.items()):
        try:
            infos[name] = parse_class(data)
        except (ClassFormatError, IndexError, KeyError, UnicodeDecodeError) as exc:
            parse_errors.append({"class": name, "error": str(exc)})
    _class_metrics(infos, out, sink, parse_errors)

    # ---------------------------------------------------- descriptors & resources
    messages: dict[str, str] = {}
    desc: dict[str, Any] = {"web": [], "struts": [], "validation": [], "tiles": [], "hbm": [], "hibernate_cfg": [],
                            "spring": [], "quartz": [], "jpa": [], "other_xml": []}
    props_files: dict[str, dict[str, str]] = {}
    for webroot in webroots:
        for xml in sorted(webroot.rglob("*.xml")):
            rel = xml.relative_to(unpacked).as_posix()
            tag = D.root_tag(xml)
            try:
                if tag == "web-app":
                    desc["web"].append({"file": rel, **D.parse_web_xml(xml)})
                elif tag == "struts-config":
                    desc["struts"].append({"file": rel, **D.parse_struts_config(xml)})
                elif tag == "form-validation":
                    desc["validation"].append({"file": rel, **D.parse_validation_xml(xml)})
                elif tag == "tiles-definitions":
                    desc["tiles"].append({"file": rel, "definitions": D.parse_tiles(xml)})
                elif tag == "hibernate-mapping":
                    desc["hbm"] += [{**e, "file": rel} for e in D.parse_hbm(xml)]
                elif tag == "hibernate-configuration":
                    desc["hibernate_cfg"].append({"file": rel, **D.parse_hibernate_cfg(xml)})
                elif tag == "beans":
                    desc["spring"].append({"file": rel, "beans": D.parse_spring(xml)})
                elif tag in ("job-scheduling-data", "quartz"):
                    desc["quartz"].append({"file": rel, "jobs": D.parse_quartz_jobs(xml)})
                elif tag == "persistence":
                    desc["jpa"].append({"file": rel, "units": D.parse_persistence(xml)})
                elif "META-INF/maven" not in rel:
                    desc["other_xml"].append({"file": rel, "root": tag})
            except Exception as exc:  # noqa: BLE001 - keep going, report
                desc["other_xml"].append({"file": rel, "root": tag, "error": str(exc)})
        message_bundles = {m.replace(".", "/") for s in desc["struts"] for m in s["message_resources"] if m}
        for pf in sorted(webroot.rglob("*.properties")):
            rel = pf.relative_to(unpacked).as_posix()
            if "META-INF/maven" in rel:
                continue
            parsed = D.parse_properties(pf.read_text(encoding="latin-1"))
            bundle = re.sub(r"(_[a-z]{2}(_[A-Z]{2})?)?\.properties$", "", rel.split("WEB-INF/classes/")[-1])
            if bundle in message_bundles or re.search(r"(resources|messages)[^/]*$", bundle, re.I):
                messages.update(parsed)
                props_files[rel] = {"_kind": "messages"}
            else:
                props_files[rel] = parsed
    write_json(out / "webapp.json", {"web": desc["web"], "tiles": desc["tiles"], "spring": desc["spring"], "jpa": desc["jpa"],
                                     "other_xml": desc["other_xml"]})
    write_json(out / "actions.json", desc["struts"])
    write_json(out / "validation-rules.json", desc["validation"])
    write_json(out / "orm-map.json", {"entities": [e for e in desc["hbm"] if "class" in e],
                                      "named_queries": [e for e in desc["hbm"] if "named_query" in e],
                                      "hibernate_cfg": desc["hibernate_cfg"], "jpa": desc["jpa"]})
    write_json(out / "messages.json", messages)

    config_rows = _config_facts(props_files, sink)
    write_json(out / "config.json", {"files": {f: {k: (mask(v) if SECRET_NAME.search(k) else v) for k, v in p.items() if k != "_kind"}
                                                for f, p in props_files.items() if p.get("_kind") != "messages"},
                                      "classified": config_rows})
    write_csv(out / "config.csv", config_rows, ["file", "key", "value", "class"])

    jsps = _jsp_inventory(webroots, unpacked, messages)
    write_json(out / "jsp-inventory.json", jsps)
    templates = _templates(webroots, unpacked, infos)
    write_json(out / "templates.json", templates)

    # ---------------------------------------------------- bytecode content
    analysis = _bytecode(infos, out, sink, messages)
    _descriptor_facts(desc, jsps, messages, sink, analysis)
    _template_facts(templates, sink)
    entry = _entrypoints(infos, desc, props_files, analysis, out, sink)

    summary: dict[str, Any] = {"artifact": str(artifact), "app_package_prefix": prefix, "app_classes": len(infos),
                               "libraries": len(libraries), "jsps": len(jsps), "entrypoints": entry}
    # ---------------------------------------------------- external tools (local JDK / executables, or Docker)
    runner = cfg.get("runner", "local")
    if cfg.get("decompile", True) or cfg.get("jdeps", True) or cfg.get("gitleaks", True) or cfg.get("sbom", True):
        if runner == "docker" and not docker_available():
            log.warning("docker not available - skipping decompilers, jdeps, gitleaks and syft")
            sink.add("finding", "Docker was not available: decompilation, jdeps, gitleaks and SBOM steps were skipped.",
                     ["tooling:docker"], "run")
        else:
            summary.update(_tool_steps(cfg, runner, out, tools_dir, classes_dirs, lib_dirs, app_classes, infos, artifact, sink))
    summary["seconds"] = timer.seconds
    write_json(out / "summary.json", summary)
    sink.write(out)
    return summary


# ================================================================ archive & inventory

def _extract_archive(path: Path, dest: Path) -> None:
    root = dest.resolve()
    ensure_dir(dest)
    with zipfile.ZipFile(path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"unsafe archive entry {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))


def _inventory(root: Path) -> list[dict]:
    rows = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        ext = f.suffix.lower()
        if ext == ".class":
            cat = "app_class" if "/classes/" in "/" + rel else "class"
        elif ext == ".jar":
            cat = "library"
        elif ext in (".jsp", ".jspf", ".tag", ".tagx"):
            cat = "jsp"
        elif ext in (".xml", ".tld"):
            cat = "descriptor"
        elif ext == ".properties":
            cat = "properties"
        elif ext in STATIC_EXT:
            cat = "static"
        elif "/classes/" in "/" + rel or ext in TEMPLATE_EXT:
            cat = "resource"
        else:
            cat = "other"
        rows.append({"path": rel, "category": cat, "size": f.stat().st_size, "sha256": sha256_file(f)})
    return rows


def _detect_prefix(classes: dict[str, bytes]) -> str:
    counts = Counter("/".join(n.split("/")[:2]) for n in classes if n.count("/") >= 2)
    return counts.most_common(1)[0][0] if counts else ""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:4])


def _libraries(lib_dirs: list[Path], prefix: str) -> tuple[list[dict], dict[str, bytes]]:
    rows, app_classes = [], {}
    for lib_dir in lib_dirs:
        for jar in sorted(lib_dir.glob("*.jar")):
            group = artifact = version = ""
            classes, max_major, packages = 0, 0, Counter()
            jar_classes: dict[str, bytes] = {}
            try:
                with zipfile.ZipFile(jar) as zf:
                    for info in zf.infolist():
                        if info.filename.startswith("META-INF/maven/") and info.filename.endswith("pom.properties") and not artifact:
                            pom = D.parse_properties(zf.read(info).decode("latin-1"))
                            group, artifact, version = pom.get("groupId", ""), pom.get("artifactId", ""), pom.get("version", "")
                        elif info.filename == "META-INF/MANIFEST.MF" and not version:
                            mf = zf.read(info).decode("latin-1")
                            m = re.search(r"^(?:Implementation-Version|Bundle-Version):\s*(\S+)", mf, re.M)
                            version = version or (m.group(1) if m else "")
                        elif info.filename.endswith(".class"):
                            classes += 1
                            head = zf.read(info)
                            if len(head) > 8:
                                max_major = max(max_major, int.from_bytes(head[6:8], "big"))
                            packages["/".join(info.filename.split("/")[:2])] += 1
                            if prefix and info.filename.startswith(prefix + "/"):
                                jar_classes[info.filename[:-6]] = head
            except zipfile.BadZipFile:
                rows.append({"jar": jar.name, "eol_note": "unreadable archive"})
                continue
            m = re.match(r"(.+?)-(\d[\w.\-]*)\.jar$", jar.name)
            if not artifact:
                artifact = m.group(1) if m else jar.stem
            if not version and m:
                version = m.group(2)
            is_app = bool(prefix) and packages and packages.most_common(1)[0][0] == prefix
            if is_app:
                app_classes.update(jar_classes)
            note = ""
            for pattern, limit, text in EOL_LIBRARIES:
                if re.search(pattern, artifact, re.I) and (limit is None or _version_tuple(version) < limit):
                    note = text
                    break
            rows.append({"jar": jar.name, "group": group, "artifact": artifact, "version": version, "classes": classes,
                         "max_class_version": max_major, "sha256": sha256_file(jar), "app_jar": is_app, "eol_note": note})
    return rows, app_classes


# ================================================================ class metrics

def _class_metrics(infos: dict[str, ClassInfo], out: Path, sink: FactSink, parse_errors: list[dict]) -> None:
    rows, debug_rows = [], []
    short_names = total_names = 0
    for name, ci in infos.items():
        rows.append({"class": ci.dotted, "major": ci.major, "java": ci.java_version, "super": (ci.super_name or "").replace("/", "."),
                     "interfaces": [i.replace("/", ".") for i in ci.interfaces], "methods": len(ci.methods), "fields": len(ci.fields)})
        debug_rows.append({"class": ci.dotted, "source_file": ci.source_file or "", "line_numbers": ci.has_line_numbers(),
                           "local_variables": ci.has_local_vars()})
        for n in [ci.simple_name.split("$")[-1]] + [m.name for m in ci.methods if not m.name.startswith("<")] + [f.name for f in ci.fields]:
            total_names += 1
            short_names += len(n) <= 2
    write_csv(out / "class-versions.csv", rows, ["class", "major", "java", "super", "interfaces", "methods", "fields"])
    write_csv(out / "debug-info.csv", debug_rows, ["class", "source_file", "line_numbers", "local_variables"])
    if parse_errors:
        write_csv(out / "class-parse-errors.csv", parse_errors)
    if not infos:
        return
    versions = Counter(r["java"] for r in rows)
    with_lines = sum(1 for r in debug_rows if r["line_numbers"])
    with_src = sum(1 for r in debug_rows if r["source_file"])
    with_locals = sum(1 for r in debug_rows if r["local_variables"])
    short_pct = 100 * short_names / max(1, total_names)
    score = round(min(100, short_pct * 1.5 + (100 - 100 * with_src / len(rows)) * 0.3 + (100 - 100 * with_lines / len(rows)) * 0.2))
    write_json(out / "obfuscation.json", {"score_0_100": score, "short_identifier_pct": round(short_pct, 1),
                                          "classes_with_source_file_pct": round(100 * with_src / len(rows), 1),
                                          "classes_with_line_numbers_pct": round(100 * with_lines / len(rows), 1),
                                          "classes_with_local_variables_pct": round(100 * with_locals / len(rows), 1)})
    sink.add("finding", f"{len(rows)} application classes compiled for Java " + ", ".join(f"{v} ({n})" for v, n in versions.most_common())
             + f"; debug info: SourceFile {with_src}/{len(rows)}, line numbers {with_lines}/{len(rows)}, local variable names {with_locals}/{len(rows)}; "
             + f"obfuscation score {score}/100.", ["code:class-metrics"], "class-versions.csv; debug-info.csv; obfuscation.json")


# ================================================================ config / jsp / templates

def _config_facts(props_files: dict[str, dict[str, str]], sink: FactSink) -> list[dict]:
    rows = []
    for file, props in props_files.items():
        if props.get("_kind") == "messages":
            continue
        for key, value in props.items():
            cls = D.classify_property(key, value)
            shown = mask(value) if cls == "secret" else value
            rows.append({"file": file, "key": key, "value": shown, "class": cls})
            keys = [f"config:{key}"]
            if cls == "secret":
                if value and not value.startswith("${"):
                    sink.add("finding", f"Plain-text credential in configuration: {file} key '{key}' (value masked {shown}).",
                             keys + ["secret:" + key], file, key, confidence="Confirmed")
                continue
            if cls == "schedule":
                sink.add("job", f"Schedule configured in {file}: {key} = cron '{value}'.", [f"cron:{value.strip()}", *keys], file, key,
                         cron=value.strip())
            elif cls == "integration":
                ep = _endpoint_key(value)
                sink.add("integration", f"Integration setting {key} = {value} ({file}).", ([ep] if ep else []) + keys, file, key)
            else:
                ftype = "rule" if cls == "parameter" else "config"
                sink.add(ftype, f"Configuration {key} = {value} ({file}).", keys, file, key, value=value)
    return rows


def _endpoint_key(value: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/\s?#]+)(/[^\s?#]*)?", value.strip(), re.I)
    if not m:
        return ""
    return f"endpoint:{m.group(1).lower()}{(m.group(2) or '/').rstrip('/') or '/'}"


def _jsp_inventory(webroots: list[Path], unpacked: Path, messages: dict[str, str]) -> list[dict]:
    jsps = []
    for webroot in webroots:
        for f in sorted(list(webroot.rglob("*.jsp")) + list(webroot.rglob("*.jspf"))):
            text = f.read_text(encoding="utf-8", errors="replace")
            info = D.parse_jsp(text, messages)
            jsps.append({"path": "/" + f.relative_to(webroot).as_posix(), "file": f.relative_to(unpacked).as_posix(), **info})
    return jsps


def _templates(webroots: list[Path], unpacked: Path, infos: dict[str, ClassInfo]) -> list[dict]:
    all_strings = set()
    for ci in infos.values():
        all_strings |= {e[1] for e in ci.cp if e and e[0] == "String"}
    out = []
    for webroot in webroots:
        for f in sorted(webroot.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in TEMPLATE_EXT or "WEB-INF" not in f.as_posix():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            rel = f.relative_to(webroot).as_posix()
            res_path = rel.split("WEB-INF/classes/")[-1]
            referenced_by = sorted({ci.dotted for ci in infos.values()
                                    if any(s.endswith(res_path) or s.endswith(f.name) for s in
                                           (e[1] for e in ci.cp if e and e[0] == "String"))})
            out.append({"path": rel, "file": f.relative_to(unpacked).as_posix(), "placeholders": D.template_placeholders(text),
                        "referenced_by": referenced_by, "preview": text[:600]})
    return out


def _template_facts(templates: list[dict], sink: FactSink) -> None:
    for t in templates:
        name = Path(t["path"]).name
        kind = "email" if re.search(r"(mail|letter|notif|activation|welcome|reminder|statement)", t["path"] + t["preview"][:200], re.I) else "config"
        sink.add(kind, f"Template {t['path']} with placeholders {', '.join(t['placeholders']) or 'none'}"
                 + (f"; loaded by {', '.join(t['referenced_by'])}" if t["referenced_by"] else "; no class references it by name") + ".",
                 [f"template:{name}"] + [f"placeholder:{p}" for p in t["placeholders"]], "templates.json", t["path"],
                 placeholders=t["placeholders"])


# ================================================================ bytecode facts

def _bytecode(infos: dict[str, ClassInfo], out: Path, sink: FactSink, messages: dict[str, str]) -> dict:
    const_rows, literal_rows, sql_rows, integ_rows, special_rows, method_rows, string_arg_rows = [], [], [], [], [], [], []
    enums: dict[str, list] = {}
    role_checks: list[dict] = []
    config_keys_used: set[str] = set()
    message_keys_used: dict[str, list[str]] = defaultdict(list)
    constant_values: dict[str, Any] = {}
    for name, ci in infos.items():
        for f in ci.fields:
            if f.constant_value is not None and f.access & ACC_STATIC:
                constant_values[f"{name}.{f.name}"] = f.constant_value
                const_rows.append({"class": ci.dotted, "field": f.name, "descriptor": f.descriptor, "value": _shown(f.name, f.constant_value),
                                   "origin": "ConstantValue", "business_hint": bool(BUSINESS_NAME.search(f.name))})
        for m in ci.methods:
            if not m.code:
                continue
            mf = ci.analyse(m)
            loc = f"{ci.dotted}.{m.name}{m.descriptor}" + (f" lines {m.line_range[0]}-{m.line_range[1]}" if m.line_range else "")
            method_rows.append({"class": ci.dotted, "method": m.name, "descriptor": m.descriptor, "lines": f"{m.line_range[0]}-{m.line_range[1]}" if m.line_range else "",
                                "bytes": len(m.code), "strings": len(mf.strings), "numbers": len(mf.numbers),
                                "string_compares": len(mf.string_compares), "number_compares": len(mf.number_compares),
                                "constant_compares": len(mf.constant_compares),
                                "throws_messages": sum(1 for _, _, o, n in mf.string_args if n == "<init>" and o.endswith(("Exception", "Error"))),
                                "calls": len(mf.invokes)})
            if m.name == "<clinit>":
                for field_name, values in mf.static_assignments:
                    fld = next((f for f in ci.fields if f.name == field_name), None)
                    if fld is None or "$" in field_name:
                        continue
                    strings = [v for v in values if isinstance(v, str)]
                    if ci.is_enum and fld.descriptor == f"L{ci.name};":
                        ordinal = len(enums.get(ci.dotted, []))
                        args = list(values)
                        if args and args[0] == field_name:
                            args = args[1:]
                        if args and args[0] == ordinal:
                            args = args[1:]
                        enums.setdefault(ci.dotted, []).append({"name": field_name, "args": args})
                        continue
                    if not (fld.descriptor in VALUE_DESCRIPTORS or fld.descriptor.startswith("[")):
                        continue
                    value: Any = strings if len(strings) > 1 else (strings[0] if strings else (values[-1] if len(values) == 1 else values))
                    constant_values[f"{name}.{field_name}"] = value
                    const_rows.append({"class": ci.dotted, "field": field_name, "descriptor": fld.descriptor, "value": _shown(field_name, value),
                                       "origin": "static-initialiser", "business_hint": bool(BUSINESS_NAME.search(field_name))})
            for pc, s in mf.strings:
                literal_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "kind": "string", "value": _shown("", s)})
                if SQL_RE.search(s):
                    sql_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "sql": re.sub(r"\s+", " ", s).strip()})
                elif INTEGRATION_RE.search(s) and len(s) > 3:
                    integ_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "string": s})
            for pc, n in mf.numbers:
                literal_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "kind": "number", "value": n})
            for pc, s, owner, callee in mf.string_args:
                string_arg_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "literal": _shown("", s), "callee": f"{owner.replace('/', '.')}.{callee}"})
                if callee == "isUserInRole":
                    role_checks.append({"class": ci.dotted, "method": m.name, "role": s, "locator": loc})
                elif callee in ("getProperty", "getString", "getInt", "getLong", "getBoolean", "getInitParameter", "getValue") and "." in s:
                    config_keys_used.add(s)
                elif s in messages:
                    message_keys_used[s].append(f"{ci.dotted}.{m.name}")
                if callee == "<init>" and owner.endswith(("Exception", "Error")) and not owner.startswith("java/lang/Null") and " " in s:
                    sink.add("rule", f"{ci.simple_name}.{m.name} raises {owner.rsplit('/', 1)[-1]}: \"{s}\".",
                             [f"class:{ci.dotted}", f"method:{ci.dotted}.{m.name}"], "bytecode", loc, confidence="Probable", message=s)
            for pc, lit, how in mf.string_compares:
                special_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "kind": "string-literal", "value": lit, "via": how, "locator": loc})
            for pc, value in mf.number_compares:
                special_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "kind": "number-literal", "value": value, "via": "if", "locator": loc})
            for pc, ref, how in mf.constant_compares:
                special_rows.append({"class": ci.dotted, "method": m.name, "pc": pc, "kind": "constant", "value": ref.replace("/", "."),
                                     "via": how, "locator": loc, "ref": ref})
    # facts from constants: named business constants
    for row in const_rows:
        if row["business_hint"] and not SECRET_NAME.search(row["field"]):
            sink.add("rule", f"Constant {row['class'].rsplit('.', 1)[-1]}.{row['field']} = {row['value']} (compiled into bytecode).",
                     [f"const:{row['class']}.{row['field']}", f"class:{row['class']}"], "constants.csv", f"{row['class']}.{row['field']}",
                     value=row["value"])
        elif SECRET_NAME.search(row["field"]) and isinstance(row["value"], str) and row["value"]:
            sink.add("finding", f"Hard-coded credential-like constant {row['class']}.{row['field']} (value masked {row['value']}).",
                     [f"const:{row['class']}.{row['field']}", "secret:" + row["field"]], "constants.csv", f"{row['class']}.{row['field']}",
                     confidence="Confirmed")
    for row in literal_rows:
        if row["kind"] == "string" and isinstance(row["value"], str) and looks_secret_value(row["value"]):
            row["value"] = mask(row["value"])
    for cls, constants in enums.items():
        names = [c["name"] for c in constants]
        sink.add("code_value", f"Enum {cls.rsplit('.', 1)[-1]} defines values {', '.join(names)}.",
                 [f"enum:{cls}", *[f"literal:{n}" for n in names]], "enums.json", cls, values=names)
        transitions = {}
        for c in constants:
            listed = [x.strip() for a in c["args"] if isinstance(a, str) for x in a.split(",") if x.strip()]
            if listed and all(x in names for x in listed):
                transitions[c["name"]] = listed
        if transitions and len(transitions) >= len(constants) / 2:
            terminal = [n for n in names if n not in transitions or not transitions[n]]
            sink.add("rule", f"Enum {cls.rsplit('.', 1)[-1]} encodes a state machine (constructor lists other constants): "
                     + "; ".join(f"{k} -> {', '.join(v)}" for k, v in transitions.items() if v)
                     + (f"; terminal: {', '.join(terminal)}" if terminal else "") + ".",
                     [f"enum:{cls}", *[f"literal:{n}" for n in names]], "enums.json", cls, confidence="Confirmed", transitions=transitions)
            continue
        for c in constants:
            if c["args"]:
                sink.add("rule", f"Enum constant {cls.rsplit('.', 1)[-1]}.{c['name']} carries metadata {c['args']}.",
                         [f"enum:{cls}", f"literal:{c['name']}"], "enums.json", f"{cls}.{c['name']}", args=c["args"])
    # special-case comparisons grouped per method
    grouped: dict[tuple, list] = defaultdict(list)
    for row in special_rows:
        grouped[(row["class"], row["method"], row["kind"])].append(row)
    for (cls, meth, kind), rows in grouped.items():
        values = sorted({str(r["value"]) for r in rows})
        if kind == "string-literal":
            sink.add("rule", f"{cls.rsplit('.', 1)[-1]}.{meth} branches on literal value(s) {', '.join(values)}.",
                     [f"method:{cls}.{meth}", *[f"literal:{v}" for v in values]], "special-cases.csv", rows[0]["locator"],
                     confidence="Probable", literals=values)
        elif kind == "number-literal":
            big = [v for v in values if _is_meaningful_number(v)]
            if big:
                sink.add("rule", f"{cls.rsplit('.', 1)[-1]}.{meth} compares against numeric literal(s) {', '.join(big)}.",
                         [f"method:{cls}.{meth}", *[f"number:{v}" for v in big]], "special-cases.csv", rows[0]["locator"],
                         confidence="To verify", numbers=big)
        else:
            refs = sorted({r["ref"] for r in rows})
            resolved = [f"{r.replace('/', '.')} (= {_shown(r, constant_values.get(r, '?'))})" for r in refs]
            sink.add("rule", f"{cls.rsplit('.', 1)[-1]}.{meth} compares against constant(s) {', '.join(resolved)}.",
                     [f"method:{cls}.{meth}", *[f"const:{r.replace('/', '.')}" for r in refs]], "special-cases.csv", rows[0]["locator"],
                     confidence="Probable")
    for rc in role_checks:
        sink.add("role", f"{rc['class'].rsplit('.', 1)[-1]}.{rc['method']} checks role '{rc['role']}' programmatically.",
                 [f"class:{rc['class']}", f"role:{rc['role']}"], "bytecode", rc["locator"], confidence="Confirmed")
    for key, users in message_keys_used.items():
        sink.add("rule", f"Message '{messages[key]}' (key {key}) is raised by {', '.join(sorted(set(users)))}.",
                 [f"msgkey:{key}", *[f"method:{u}" for u in sorted(set(users))]], "bytecode; messages.json", key, message=messages[key])
    for row in sql_rows:
        tables = _sql_tables(row["sql"])
        sink.add("entity", f"Embedded SQL/HQL in {row['class'].rsplit('.', 1)[-1]}.{row['method']}: {row['sql'][:300]}",
                 [f"method:{row['class']}.{row['method']}", *[f"table:{t}" for t in tables]], "sql-strings.csv", f"{row['class']}.{row['method']}@{row['pc']}",
                 sql=row["sql"])
    for row in integ_rows:
        ep = _endpoint_key(row["string"])
        if ep and NAMESPACE_HOSTS.search(ep):
            continue
        sink.add("integration", f"Integration literal in {row['class'].rsplit('.', 1)[-1]}.{row['method']}: {row['string'][:200]}",
                 ([ep] if ep else []) + [f"method:{row['class']}.{row['method']}"], "integration-strings.csv",
                 f"{row['class']}.{row['method']}@{row['pc']}")
    write_csv(out / "constants.csv", const_rows, ["class", "field", "descriptor", "value", "origin", "business_hint"])
    write_csv(out / "literals.csv", literal_rows, ["class", "method", "pc", "kind", "value"])
    write_csv(out / "sql-strings.csv", sql_rows, ["class", "method", "pc", "sql"])
    write_csv(out / "integration-strings.csv", integ_rows, ["class", "method", "pc", "string"])
    write_csv(out / "special-cases.csv", special_rows, ["class", "method", "pc", "kind", "value", "via", "locator"])
    write_csv(out / "string-arguments.csv", string_arg_rows, ["class", "method", "pc", "literal", "callee"])
    write_csv(out / "methods.csv", method_rows)
    write_json(out / "enums.json", enums)
    return {"enums": enums, "config_keys_used": sorted(config_keys_used), "constant_values": constant_values,
            "methods": method_rows, "role_checks": role_checks}


def _shown(name: str, value: Any) -> Any:
    if isinstance(value, str) and (SECRET_NAME.search(name or "") and value):
        return mask(value)
    return value


def _is_meaningful_number(v: str) -> bool:
    try:
        n = float(v)
    except ValueError:
        return False
    return abs(n) >= 2 and n not in (10, 16, 100, 1000, 1024, 255, 256)


def _sql_tables(sql: str) -> list[str]:
    names = re.findall(r"\b(?:from|join|into|update)\s+`?([A-Za-z_][\w$]*)`?", sql, re.I)
    return list(dict.fromkeys(n.lower() for n in names if n.lower() not in ("select", "dual", "set")))


# ================================================================ descriptor facts

def _descriptor_facts(desc: dict, jsps: list[dict], messages: dict[str, str], sink: FactSink, analysis: dict) -> None:
    form_beans = {fb["name"]: fb for s in desc["struts"] for fb in s["form_beans"]}
    tiles_jsp: dict[str, list[str]] = {}
    for t in desc["tiles"]:
        defs = {d["name"]: d for d in t["definitions"]}
        for name, d in defs.items():
            jsps_for, cur, guard = [], d, 0
            while cur and guard < 10:
                jsps_for += [v for v in cur["puts"].values() if v and v.endswith(".jsp") and "layout" not in v.lower()]
                cur, guard = defs.get(cur["extends"] or ""), guard + 1
            tiles_jsp[name] = list(dict.fromkeys(jsps_for))[:3]
    rules_by_form: dict[str, list] = defaultdict(list)
    for v in desc["validation"]:
        for r in v["rules"]:
            rules_by_form[r["form"]].append(r)
    for s in desc["struts"]:
        for a in s["actions"]:
            path = a["path"] or ""
            key = f"path:{norm_path(path)}"
            fwd = ", ".join(f"{f['name']}->{f['path']}" for f in a["forwards"])
            sink.add("screen", f"Action {path} handled by {a['type'] or a['forward_attr'] or a['include']}"
                     + (f"; form {a['form']}" if a["form"] else "") + (f"; roles {','.join(a['roles'])}" if a["roles"] else "; no declarative role")
                     + (f"; dispatch parameter '{a['parameter']}'" if a["parameter"] else "") + (f"; forwards {fwd}" if fwd else "") + ".",
                     [key, f"class:{a['type']}" if a["type"] else ""], s["file"], path,
                     roles=a["roles"], form=a["form"], forwards=a["forwards"], validate=a["validate"], parameter=a["parameter"])
            if a["roles"]:
                sink.add("role", f"Path {norm_path(path)} is restricted to roles {', '.join(a['roles'])} (struts-config).",
                         [key, *[f"role:{r}" for r in a["roles"]]], s["file"], path, confidence="Confirmed", roles=a["roles"])
            for f in a["forwards"]:
                targets = [f["path"]] if f["path"] and f["path"].endswith(".jsp") else tiles_jsp.get(f["path"] or "", [])
                for jsp_path in targets:
                    sink.add("screen", f"Action {path} renders {jsp_path} on outcome '{f['name']}'" + (f" (tiles {f['path']})" if jsp_path != f["path"] else "") + ".",
                             [key, f"jsp:{jsp_path}"], s["file"], path)
            fb = form_beans.get(a["form"] or "")
            if fb:
                for p in fb["properties"]:
                    sink.add("control", f"Field '{p['name']}' ({(p['type'] or '').rsplit('.', 1)[-1]}) on {path} (form {fb['name']}).",
                             [f"field:{norm_path(path)}#{p['name']}", f"formbean:{fb['name']}#{p['name']}"], s["file"], path)
            if a["form"] and a["validate"] == "false" and rules_by_form.get(a["form"]):
                sink.add("finding", f"Action {path} declares validate=\"false\" while form {a['form']} has validation.xml rules; validation depends on code invoking it.",
                         [key, f"formbean:{a['form']}"], s["file"], path, confidence="To verify")
        for e in s["global_exceptions"]:
            sink.add("config", f"Global exception handler {e['type']} -> {e['path']} (key {e['key']}).", [f"class:{e['type']}"], s["file"])
    for v in desc["validation"]:
        for r in v["rules"]:
            msg_texts = [messages.get(k, k) for k in r["msgs"].values()]
            label = messages.get(r["args"][0], "") if r["args"] and r["args"][0] else ""
            stmt = f"Form {r['form']} field '{r['field']}'" + (f" ({label})" if label else "") + ": " + ", ".join(r["depends"])
            if r["vars"]:
                stmt += " [" + ", ".join(f"{k}={x}" for k, x in r["vars"].items()) + "]"
            if msg_texts:
                stmt += f"; messages: {' | '.join(msg_texts)}"
            keys = [f"formbean:{r['form']}#{r['field']}"]
            for s in desc["struts"]:
                keys += [f"field:{norm_path(a['path'])}#{r['field']}" for a in s["actions"] if a["form"] == r["form"]]
            numbers = [str(x) for x in r["vars"].values() if re.fullmatch(r"-?\d+(\.\d+)?", str(x))]
            sink.add("rule", stmt + ".", keys + [f"number:{n}" for n in numbers], v["file"], f"{r['form']}.{r['field']}",
                     confidence="Confirmed", depends=r["depends"], vars=r["vars"])
    for entity in desc["hbm"]:
        if "named_query" in entity:
            sink.add("rule", f"Named query {entity['named_query']}: {(entity.get('hql') or entity.get('sql') or '')[:300]}",
                     [f"query:{entity['named_query']}"], entity["file"])
            continue
        table = (entity["table"] or "").lower()
        cols = [p["column"] for p in entity["properties"]]
        sink.add("entity", f"Entity {entity['class']} maps table {table} (id {entity['id']['column'] if entity['id'] else '?'}"
                 + (f", generator {entity['id']['generator']}" if entity['id'] and entity['id']['generator'] else "") + f"; columns {', '.join(cols)}).",
                 [f"table:{table}", f"class:{entity['class']}", *[f"column:{table}.{(c or '').lower()}" for c in cols]], entity["file"], entity["class"],
                 confidence="Confirmed", columns=cols)
        for r in entity["relations"]:
            sink.add("relationship", f"Entity {entity['class']} {r['kind']} {r['class'] or r['name']} via column {r['column']}.",
                     [f"table:{table}", f"column:{table}.{(r['column'] or '').lower()}"], entity["file"], entity["class"], confidence="Confirmed")
        if not entity["relations"]:
            sink.add("finding", f"Entity {entity['class']} declares no ORM associations; relationships to other tables are held as plain columns.",
                     [f"table:{table}"], entity["file"], entity["class"], confidence="Confirmed")
    for w in desc["web"]:
        for sc in w["security_constraints"]:
            sink.add("role", f"web.xml security constraint on {', '.join(sc['resources'])} for roles {', '.join(sc['roles']) or 'none'}.",
                     [*[f"path:{norm_path(p)}" for p in sc["resources"]], *[f"role:{r}" for r in sc["roles"]]], w["file"], confidence="Confirmed")
        for flt in w["filters"]:
            sink.add("config", f"Servlet filter {flt['name']} ({flt['class']}) init {flt['init']}.", [f"class:{flt['class']}"], w["file"], flt["name"])
        if w["session_timeout_min"]:
            sink.add("rule", f"HTTP session timeout is {w['session_timeout_min']} minutes (web.xml).", ["config:session-timeout",
                     f"number:{w['session_timeout_min']}"], w["file"], confidence="Confirmed")
        for lst in w["listeners"]:
            sink.add("config", f"Startup listener {lst} registered in web.xml.", [f"class:{lst}"], w["file"])
        for name, value in w["context_params"].items():
            sink.add("config", f"Context parameter {name} = {value}.", [f"config:{name}"], w["file"], name)
    for q in desc["quartz"]:
        for j in q["jobs"]:
            if j.get("cron"):
                sink.add("job", f"Quartz trigger {j.get('trigger')} for job {j.get('job')} cron '{j['cron']}'.",
                         [f"cron:{j['cron']}", f"job:{j.get('job')}"], q["file"], cron=j["cron"])
    for jsp in jsps:
        for cond in jsp["el_literal_conditions"]:
            literals = re.findall(r"'([^']+)'", cond)
            sink.add("rule", f"Screen {jsp['path']} shows content conditionally on {cond}.",
                     [f"jsp:{jsp['path']}", *[f"literal:{x}" for x in literals]], jsp["file"], confidence="Probable")
        for role in jsp["role_checks"]:
            sink.add("role", f"Screen {jsp['path']} renders content only for role {role}.", [f"jsp:{jsp['path']}", f"role:{role}"], jsp["file"])
        for form in jsp["forms"]:
            action = norm_path(form["action"]) if form["action"] else ""
            for fld in jsp["fields"]:
                if action:
                    sink.add("control", f"{jsp['path']} form {action} has {fld['type']} '{fld['name']}'"
                             + (f" maxlength {fld['maxlength']}" if fld["maxlength"] else "") + (" (read-only)" if fld["readonly"] else "") + ".",
                             [f"field:{action}#{fld['name']}", f"jsp:{jsp['path']}"], jsp["file"])
        if jsp["options"]:
            sink.add("code_value", f"Screen {jsp['path']} offers fixed option values {', '.join(jsp['options'])}.",
                     [f"jsp:{jsp['path']}", *[f"literal:{o}" for o in jsp["options"]]], jsp["file"], values=jsp["options"])
        if jsp["buttons"]:
            sink.add("control", f"Screen {jsp['path']} buttons: {', '.join(b['text'] for b in jsp['buttons'] if b['text'])}.",
                     [f"jsp:{jsp['path']}"], jsp["file"])


# ================================================================ entry points / dead code

def _ancestors(name: str, infos: dict[str, ClassInfo]) -> list[str]:
    chain, seen, todo = [], set(), [name]
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        ci = infos.get(cur)
        if not ci:
            continue
        for parent in ([ci.super_name] if ci.super_name else []) + ci.interfaces:
            chain.append(parent)
            todo.append(parent)
    return chain


def _entrypoints(infos: dict[str, ClassInfo], desc: dict, props: dict, analysis: dict, out: Path, sink: FactSink) -> dict:
    declared_text = repr(desc) + repr(props)
    mapped_actions = {a["type"] for s in desc["struts"] for a in s["actions"] if a["type"]}
    inbound: dict[str, set[str]] = defaultdict(set)
    for name, ci in infos.items():
        for ref in ci.class_refs:
            if ref in infos and ref.split("$")[0] != name.split("$")[0]:
                inbound[ref.split("$")[0]].add(name)
    rows = []
    for name, ci in infos.items():
        if "$" in name:
            continue
        chain = _ancestors(name, infos)
        kinds = [k for k, bases in ENTRYPOINT_BASES.items()
                 if any(c == b or (b.endswith("/") and c.startswith(b)) for c in chain for b in bases)]
        dotted = ci.dotted
        declared = dotted in declared_text
        abstract = bool(ci.access & 0x400) or bool(ci.access & 0x200)
        row = {"class": dotted, "kinds": kinds, "declared_in_descriptor_or_config": declared, "inbound_refs": len(inbound.get(name, ())),
               "abstract_or_interface": abstract, "status": "ok"}
        if "struts_action" in kinds and not abstract and dotted not in mapped_actions:
            row["status"] = "unmapped_action"
            sink.add("dead_feature", f"Action class {dotted} is not mapped to any path in struts-config (unreachable through the UI).",
                     [f"class:{dotted}", *[f"token:{t}" for t in split_words(ci.simple_name) if t not in ('action',)]], "actions.json; class-versions.csv",
                     dotted, confidence="Probable", tokens=split_words(ci.simple_name))
        elif "job" in kinds and not abstract:
            sink.add("job", f"Scheduled job implementation {dotted}" + (" (registered by name in configuration)" if declared else "") + ".",
                     [f"job:{ci.simple_name}", f"class:{dotted}"], "class-versions.csv", dotted, confidence="Confirmed")
        elif not kinds and not declared and not inbound.get(name) and not abstract:
            row["status"] = "unreferenced"
            sink.add("dead_feature", f"Class {dotted} is never referenced by other application classes or descriptors.",
                     [f"class:{dotted}", *[f"token:{t}" for t in split_words(ci.simple_name)]], "class-versions.csv", dotted,
                     confidence="To verify", tokens=split_words(ci.simple_name))
        rows.append(row)
    write_csv(out / "entrypoints.csv", rows, ["class", "kinds", "declared_in_descriptor_or_config", "inbound_refs", "abstract_or_interface", "status"])
    write_csv(out / "dead-code.csv", [r for r in rows if r["status"] != "ok"])
    used_keys = set(analysis["config_keys_used"])
    unused = [{"file": f, "key": k} for f, p in props.items() if p.get("_kind") != "messages" for k in p if k not in used_keys]
    write_csv(out / "config-keys-not-read-by-literal.csv", unused, ["file", "key"])
    return {"classes": len(rows), "unmapped_actions": sum(r["status"] == "unmapped_action" for r in rows),
            "unreferenced": sum(r["status"] == "unreferenced" for r in rows), "config_keys_not_read_by_literal": len(unused)}


# ================================================================ external tool steps

def _executable(cfg: dict, key: str, name: str) -> str:
    path = cfg.get(key) or shutil.which(name)
    if not path or not Path(path).exists():
        raise ToolError(f"{name} not found: set {key} in the config or add it to PATH")
    return str(path)


def _tool_steps(cfg: dict, runner: str, out: Path, tools_dir: Path, classes_dirs: list[Path], lib_dirs: list[Path],
                app_classes: dict[str, bytes], infos: dict[str, ClassInfo], artifact: Path, sink: FactSink) -> dict:
    result: dict[str, Any] = {}
    local = runner == "local"
    jdk = cfg.get("jdk", {})
    work = ensure_dir(out / "work")
    app_jar = work / "app-classes.jar"
    with zipfile.ZipFile(app_jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(app_classes.items()):
            zf.writestr(name + ".class", data)
    classes_dir = classes_dirs[0] if classes_dirs else work
    lib_dir = lib_dirs[0] if lib_dirs else None
    majors = Counter(ci.major for ci in infos.values())
    major = majors.most_common(1)[0][0] if majors else 52
    source_level = "1.5" if major <= 49 else f"1.{min(major - 44, 8)}"
    decompilers = cfg.get("decompilers", ["vineflower", "cfr", "procyon"])
    if cfg.get("decompile", True) and app_classes:
        try:
            dres = decompile(app_jar, classes_dir, lib_dir, out / "decompiled", tools_dir, decompilers,
                             cfg.get("recompile", True) and major <= 52, source_level, cfg.get("recompile_limit", 0), runner, jdk)
            fid = fidelity(out / "decompiled", decompilers, app_classes, dres["recompile"])
            result["decompile"] = {"timings": dres["timings"], "fidelity": fid}
            pt = fid["per_tool"]
            sink.add("finding", "Decompilation fidelity: " + "; ".join(
                f"{t}: coverage {v['coverage_pct']}%, files with error markers {v['files_with_error_markers']}, recompile {v['recompile_rate_pct']}%"
                f", bytecode round-trip mean {v['roundtrip_mean_similarity']}" for t, v in pt.items())
                + f"; cross-tool agreement mean {fid['agreement']['mean_min_similarity']} ({fid['agreement']['classes_below_0_8']} classes below 0.8)."
                + f" Recommended primary decompiler: {fid['recommended_primary']}.",
                ["code:decompile-fidelity"], "decompiled/fidelity-summary.json", confidence="Confirmed")
        except (ToolError, OSError, RuntimeError) as exc:
            log.warning("decompilation failed: %s", exc)
            result["decompile_error"] = str(exc)
    if cfg.get("jdeps", True) and app_classes:
        try:
            if local:
                libs = sorted(str(p) for p in lib_dir.glob("*.jar")) if lib_dir else []
                cmd = [jdk_tool(jdk.get("home"), "jdeps"), "-verbose:package", *(["-cp", os.pathsep.join(libs)] if libs else []), str(app_jar)]
                proc = run_tool(cmd, ok_codes=(0, 1))
            else:
                cmd = "CP=$(ls /applib/*.jar 2>/dev/null | tr '\\n' ':'); jdeps -verbose:package ${CP:+-cp \"$CP\"} /work/app-classes.jar"
                mounts = [(work, "/work", True)] + ([(lib_dir, "/applib", True)] if lib_dir else [])
                proc = docker_run(JDK8_IMAGE, ["-c", cmd], mounts, entrypoint="/bin/sh", ok_codes=(0, 1))
            (out / "jdeps.txt").write_text(proc.stdout, encoding="utf-8")
            deps, current = [], ""
            for line in proc.stdout.splitlines():
                head = re.match(r"^\s{1,4}(\S+) \((.+)\)\s*$", line)  # JDK 8: package header, then edges
                edge = re.match(r"^\s+->\s+(\S+)\s*(.*)$", line)
                flat = re.match(r"^\s+(\S+)\s+->\s+(\S+)\s*(.*)$", line)  # JDK 9+: one line per edge
                if head:
                    current = head.group(1)
                elif edge and current:
                    deps.append({"from_package": current, "to_package": edge.group(1), "provider": edge.group(2).strip() or "JDK"})
                elif flat:
                    deps.append({"from_package": flat.group(1), "to_package": flat.group(2), "provider": flat.group(3).strip() or "JDK"})
            write_csv(out / "deps.csv", deps, ["from_package", "to_package", "provider"])
            missing = sorted({d["to_package"] for d in deps if "not found" in d["provider"]})
            result["jdeps"] = {"edges": len(deps), "packages_not_found": missing}
            if missing:
                sink.add("finding", f"jdeps: {len(missing)} referenced packages are not in the artifact (provided by the container/JDK): {', '.join(missing[:15])}.",
                         ["code:jdeps"], "deps.csv", confidence="Confirmed")
        except (ToolError, OSError) as exc:
            result["jdeps_error"] = str(exc)
    if cfg.get("sbom", True):
        try:
            if local:
                proc = run_tool([_executable(cfg, "syft_path", "syft"), "scan", f"file:{artifact}", "-o", "cyclonedx-json", "-q"])
            else:
                proc = docker_run(cfg.get("syft_image", "anchore/syft:latest"), ["scan", f"file:/in/{artifact.name}", "-o", "cyclonedx-json", "-q"],
                                  [(artifact.parent, "/in", True)])
            (out / "sbom.cdx.json").write_text(proc.stdout, encoding="utf-8")
            result["sbom"] = "sbom.cdx.json"
        except (ToolError, OSError) as exc:
            result["sbom_error"] = str(exc)
    if cfg.get("gitleaks", True):
        hits = []
        try:
            scans = ["unpacked"] + [f"decompiled/{t}" for t in decompilers[:1] if (out / "decompiled" / t).exists()]
            for i, scan in enumerate(scans):
                report = out / f"gitleaks-{i}.json"
                if local:
                    run_tool([_executable(cfg, "gitleaks_path", "gitleaks"), "detect", "--no-git", "--redact", "--no-banner",
                              "--source", str(out / scan), "--report-format", "json", "--report-path", str(report)], ok_codes=(0, 1))
                else:
                    docker_run(cfg.get("gitleaks_image", "zricethezav/gitleaks:latest"),
                               ["detect", "--no-git", "--redact", "--no-banner", "--source", f"/scan/{scan}", "--report-format", "json",
                                "--report-path", f"/scan/{report.name}"],
                               [(out, "/scan", False)], ok_codes=(0, 1))
                if report.exists():
                    found = json.loads(report.read_text(encoding="utf-8") or "[]")
                    for h in found:
                        h["File"] = _relative(h.get("File", ""), out, scan)
                    hits += found
                    report.unlink()
            write_json(out / "gitleaks.json", hits)
            for h in hits:
                file = h["File"]
                sink.add("finding", f"gitleaks rule '{h.get('RuleID')}' matched in {file} line {h.get('StartLine')} (secret redacted).",
                         [f"secret:{h.get('RuleID')}", f"file:{file}"], "gitleaks.json", f"{file}:{h.get('StartLine')}", confidence="Probable")
            result["gitleaks"] = {"hits": len(hits)}
        except (ToolError, OSError, ValueError) as exc:
            result["gitleaks_error"] = str(exc)
    return result


def _relative(file: str, out: Path, scan: str) -> str:
    """gitleaks reports container paths (/scan/...), absolute paths, or paths relative to the scanned folder."""
    if file.startswith("/scan/"):
        return file[len("/scan/"):]
    path = Path(file)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(out.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
    return f"{scan}/{path.as_posix()}"
