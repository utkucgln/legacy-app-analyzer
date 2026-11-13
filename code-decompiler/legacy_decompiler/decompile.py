"""Decompilation with three independent decompilers (local JDK or Docker), plus fidelity metrics.

Fidelity: per-tool coverage and error markers, cross-tool agreement, independent recompilation
rate and bytecode round-trip similarity of recompiled classes against the originals.
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

from legacy_decompiler.classfile import ClassFormatError, parse_class
from legacy_decompiler.common import Timer, docker_run, ensure_dir, jdk_tool, run_tool, write_csv, write_json

log = logging.getLogger("legacy_decompiler.decompile")

TOOL_URLS = {
    "vineflower": "https://repo1.maven.org/maven2/org/vineflower/vineflower/1.10.1/vineflower-1.10.1.jar",
    "cfr": "https://repo1.maven.org/maven2/org/benf/cfr/0.152/cfr-0.152.jar",
    "procyon": "https://github.com/mstrobel/procyon/releases/download/v0.6.0/procyon-decompiler-0.6.0.jar",
    "servlet-api": "https://repo1.maven.org/maven2/javax/servlet/servlet-api/2.5/servlet-api-2.5.jar",
    "jsp-api": "https://repo1.maven.org/maven2/javax/servlet/jsp/jsp-api/2.1/jsp-api-2.1.jar",
}
ERROR_MARKERS = [
    r"\$FF: Couldn't be decompiled", r"<unrepresentable>", r"This method has failed to decompile",
    r"Exception decompiling", r"/\* Error \*/", r"// Error:", r"Could not load the following classes",
    r"Unable to fully structure code", r"\$VF: Couldn't be decompiled", r"WARNING - Removed try catching",
    r"Decompilation failed", r"// This method could not be decompiled",
]
DECOMPILER_IMAGE = "eclipse-temurin:17-jdk"
JDK8_IMAGE = "maven:3.9-eclipse-temurin-8"


def fetch_tools(tools_dir: Path, names: list[str]) -> dict[str, Path]:
    ensure_dir(tools_dir)
    paths = {}
    for name in names:
        url = TOOL_URLS[name]
        target = tools_dir / f"{name}.jar"
        if not target.exists():
            log.info("downloading %s", url)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(url, context=ctx, timeout=120) as resp:  # noqa: S310 - fixed https URLs
                data = resp.read()
            if "repo1.maven.org" in url:
                with urllib.request.urlopen(url + ".sha1", context=ctx, timeout=60) as resp:  # noqa: S310
                    expected = resp.read().decode().split()[0].strip()
                if hashlib.sha1(data).hexdigest() != expected:  # noqa: S324 - integrity check against Maven Central
                    raise RuntimeError(f"checksum mismatch for {url}")
            target.write_bytes(data)
        paths[name] = target
    write_json(tools_dir / "tools-manifest.json",
               {n: {"url": TOOL_URLS[n], "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for n, p in paths.items()})
    return paths


DECOMPILE_CMDS = {
    "vineflower": "java -Xmx2g -jar /tmp/w/vineflower.jar -dgs=1 -log=WARN $VF_LIBS /tmp/w/app.jar /tmp/w/out/vineflower",
    "cfr": "java -Xmx2g -jar /tmp/w/cfr.jar /tmp/w/app.jar --outputdir /tmp/w/out/cfr --silent true ${CP:+--extraclasspath $CP}",
    "procyon": "java -Xmx2g -jar /tmp/w/procyon.jar -jar /tmp/w/app.jar -o /tmp/w/out/procyon",
}
# Inputs are copied into the container and results tarred back once: per-file I/O over Windows bind mounts is very slow.
STAGE_IN = ("set -u; mkdir -p /tmp/w/lib /tmp/w/out /tmp/w/logs; cp /work/app.jar /tmp/w/app.jar; cp /tools/*.jar /tmp/w/; "
            "cp /applib/*.jar /tmp/w/lib/ 2>/dev/null; "
            "CP=$(ls /tmp/w/lib/*.jar 2>/dev/null | tr '\\n' ':' | sed 's/:$//'); "
            "VF_LIBS=$(for j in /tmp/w/lib/*.jar; do [ -f \"$j\" ] && printf -- '-e=%s ' \"$j\"; done); ")


def decompile(app_jar: Path, classes_dir: Path, lib_dir: Path | None, out_dir: Path, tools_dir: Path,
              decompilers: list[str], recompile: bool, source_level: str, recompile_limit: int = 0,
              runner: str = "local", jdk: dict | None = None) -> dict:
    fetch_tools(tools_dir, decompilers + (["servlet-api", "jsp-api"] if recompile else []))
    unknown = [t for t in decompilers if t not in DECOMPILE_CMDS]
    if unknown:
        raise ValueError(f"unknown decompiler(s) {unknown}")
    for name in [*decompilers, "recompiled", "logs"]:
        if (out_dir / name).exists():
            shutil.rmtree(out_dir / name)
    ensure_dir(out_dir / "logs")
    if runner == "local":
        return _decompile_local(app_jar, classes_dir, lib_dir, out_dir, tools_dir, decompilers, recompile, source_level,
                                recompile_limit, jdk or {})
    staging = ensure_dir(out_dir / "_stage")
    shutil.copy2(app_jar, staging / "app.jar")
    base_mounts = [(staging, "/work", True), (tools_dir, "/tools", True), (out_dir, "/out", False)]
    if lib_dir and lib_dir.exists():
        base_mounts.append((lib_dir, "/applib", True))

    script = STAGE_IN
    for tool in decompilers:
        script += (f"mkdir -p /tmp/w/out/{tool}; s=$(date +%s); {DECOMPILE_CMDS[tool]} > /tmp/w/logs/{tool}.log 2>&1; rc=$?; "
                   f"echo \"{tool} $rc $(( $(date +%s) - s ))\" >> /tmp/w/logs/timings.txt; ")
    script += "cd /tmp/w/out && tar cf /out/decompiled.tar . && cp /tmp/w/logs/* /out/logs/"
    timer = Timer()
    docker_run(DECOMPILER_IMAGE, ["-c", script], base_mounts, entrypoint="/bin/sh")
    total = timer.seconds
    _untar(out_dir / "decompiled.tar", out_dir)
    _unpack_outputs(out_dir, decompilers)
    timings: dict[str, dict] = {}
    tfile = out_dir / "logs" / "timings.txt"
    for line in (tfile.read_text(encoding="utf-8").splitlines() if tfile.exists() else []):
        tool, rc, secs = line.split()
        timings[tool] = {"seconds": float(secs), "exit_code": int(rc)}
    timings["decompile_container_total"] = {"seconds": total}

    recompile_rows: dict[str, list[dict]] = {}
    if recompile:
        script_dir = Path(__file__).parent / "java"
        shutil.copy2(script_dir / "Recompile.java", staging / "Recompile.java")
        with tarfile.open(staging / "classes.tar", "w") as tf:
            tf.add(classes_dir, arcname=".")
        shutil.copy2(out_dir / "decompiled.tar", staging / "decompiled.tar")
        cmd = (STAGE_IN + "mkdir -p /tmp/w/classes /tmp/w/src /tmp/rc && tar xf /work/classes.tar -C /tmp/w/classes && "
               "tar xf /work/decompiled.tar -C /tmp/w/src && javac -d /tmp/rc /work/Recompile.java && "
               "RCP=/tmp/w/classes:${CP:+$CP:}/tmp/w/servlet-api.jar:/tmp/w/jsp-api.jar && "
               f"java -cp /tmp/rc Recompile /tmp/w/src \"$RCP\" {source_level} {int(recompile_limit)} {' '.join(decompilers)}; "
               "cd /tmp/w/src && tar cf /out/recompiled.tar recompiled && cp logs/* /out/logs/")
        timer = Timer()
        docker_run(JDK8_IMAGE, ["-c", cmd], base_mounts, entrypoint="/bin/sh")
        timings["recompile"] = {"seconds": timer.seconds}
        _untar(out_dir / "recompiled.tar", out_dir)
        recompile_rows = _recompile_rows(out_dir, decompilers)
    shutil.rmtree(staging, ignore_errors=True)
    for tar in ("decompiled.tar", "recompiled.tar"):
        (out_dir / tar).unlink(missing_ok=True)
    return {"timings": timings, "recompile": recompile_rows}


def _local_cmd(tool: str, java: str, tools_dir: Path, app_jar: Path, dest: Path, libs: list[str]) -> list[str]:
    jar = str(tools_dir / f"{tool}.jar")
    if tool == "vineflower":
        return [java, "-Xmx2g", "-jar", jar, "-dgs=1", "-log=WARN", *[f"-e={lib}" for lib in libs], str(app_jar), str(dest)]
    if tool == "cfr":
        return [java, "-Xmx2g", "-jar", jar, str(app_jar), "--outputdir", str(dest), "--silent", "true",
                *(["--extraclasspath", os.pathsep.join(libs)] if libs else [])]
    return [java, "-Xmx2g", "-jar", jar, "-jar", str(app_jar), "-o", str(dest)]


def _decompile_local(app_jar: Path, classes_dir: Path, lib_dir: Path | None, out_dir: Path, tools_dir: Path, decompilers: list[str],
                     recompile: bool, source_level: str, recompile_limit: int, jdk: dict) -> dict:
    """Same steps as the Docker runner, using locally installed JDKs (Windows Server friendly, no containers)."""
    java = jdk_tool(jdk.get("home"), "java")
    libs = sorted(str(p) for p in lib_dir.glob("*.jar")) if lib_dir and lib_dir.exists() else []
    timeout = int(jdk.get("timeout_s", 7200))
    timings: dict[str, dict] = {}
    total = Timer()
    for tool in decompilers:
        dest = ensure_dir(out_dir / tool)
        timer = Timer()
        rc = 0
        try:
            run_tool(_local_cmd(tool, java, tools_dir, app_jar, dest, libs), timeout=timeout, log_file=out_dir / "logs" / f"{tool}.log")
        except Exception as exc:  # noqa: BLE001 - one failing decompiler must not stop the others
            log.warning("%s failed: %s", tool, str(exc)[:300])
            rc = 1
        timings[tool] = {"seconds": timer.seconds, "exit_code": rc}
    timings["decompile_total"] = {"seconds": total.seconds}
    _unpack_outputs(out_dir, decompilers)
    recompile_rows: dict[str, list[dict]] = {}
    if recompile:
        # JDK 9+ cannot compile -source 1.5..1.7; without a JDK 8 the sources are recompiled at level 8.
        home8 = jdk.get("recompile_home")
        level = source_level if home8 else "8"
        javac, java_rc = jdk_tool(home8 or jdk.get("home"), "javac"), jdk_tool(home8 or jdk.get("home"), "java")
        classpath = os.pathsep.join([str(classes_dir), *libs, str(tools_dir / "servlet-api.jar"), str(tools_dir / "jsp-api.jar")])
        timer = Timer()
        with tempfile.TemporaryDirectory(prefix="recompile-") as tmp:
            run_tool([javac, "-d", tmp, str(Path(__file__).parent / "java" / "Recompile.java")], timeout=600)
            run_tool([java_rc, "-cp", tmp, "Recompile", str(out_dir), classpath, level, str(int(recompile_limit)), *decompilers],
                     timeout=timeout, log_file=out_dir / "logs" / "recompile.log")
        timings["recompile"] = {"seconds": timer.seconds, "source_level": level}
        recompile_rows = _recompile_rows(out_dir, decompilers)
    return {"timings": timings, "recompile": recompile_rows}


def _unpack_outputs(out_dir: Path, decompilers: list[str]) -> None:
    for tool in decompilers:
        for archive in list((out_dir / tool).glob("*.jar")) + list((out_dir / tool).glob("*.zip")):
            with zipfile.ZipFile(archive) as zf:
                _safe_extract(zf, out_dir / tool)
            archive.unlink()


def _recompile_rows(out_dir: Path, decompilers: list[str]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for tool in decompilers:
        tsv = out_dir / "logs" / f"recompile-{tool}.tsv"
        rows = []
        if tsv.exists():
            for line in tsv.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    rows.append({"tool": tool, "file": parts[0], "result": parts[1], "first_error": parts[2] if len(parts) > 2 else ""})
        result[tool] = rows
    return result


def _untar(path: Path, dest: Path) -> None:
    if not path.exists():
        return
    root = dest.resolve()
    with tarfile.open(path) as tf:
        members = []
        for m in tf.getmembers():
            target = (dest / m.name).resolve()
            if (root != target and root not in target.parents) or m.issym() or m.islnk():
                raise RuntimeError(f"unsafe archive entry {m.name}")
            members.append(m)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:
            tf.extractall(dest, members=members)  # noqa: S202 - members validated above


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f"unsafe archive entry {member.filename}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))


# ---------------------------------------------------------------- fidelity

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_TOKEN = re.compile(r"[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|\"(?:[^\"\\]|\\.)*\"|\S")


def _tokens(src: str) -> Counter:
    body = _COMMENT.sub(" ", src)
    body = re.sub(r"^\s*(package|import)\s+[^;]+;", " ", body, flags=re.M)
    return Counter(_TOKEN.findall(body))


def _similarity(a: Counter, b: Counter) -> float:
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 1.0


def fidelity(out_dir: Path, decompilers: list[str], app_classes: dict[str, bytes], recompile_rows: dict[str, list[dict]]) -> dict:
    top_level = sorted({n.split("$")[0] for n in app_classes})
    per_tool, sources = {}, {}
    marker_rows = []
    for tool in decompilers:
        root = out_dir / tool
        files = {p.relative_to(root).as_posix()[:-5]: p for p in root.rglob("*.java")} if root.exists() else {}
        sources[tool] = files
        errors = 0
        for cls, path in files.items():
            text = path.read_text(encoding="utf-8", errors="replace")
            hits = sum(len(re.findall(m, text)) for m in ERROR_MARKERS)
            if hits:
                errors += 1
                marker_rows.append({"tool": tool, "class": cls.replace("/", "."), "error_markers": hits})
        rows = recompile_rows.get(tool, [])
        ok = sum(1 for r in rows if r["result"] == "OK")
        per_tool[tool] = {
            "classes_expected": len(top_level), "files_produced": len(files),
            "coverage_pct": round(100 * len(set(files) & set(top_level)) / max(1, len(top_level)), 1),
            "files_with_error_markers": errors,
            "recompiled_attempted": len(rows), "recompiled_ok": ok,
            "recompile_rate_pct": round(100 * ok / len(rows), 1) if rows else None,
        }
    agreement_rows = []
    for cls in top_level:
        texts = {t: _tokens(sources[t][cls].read_text(encoding="utf-8", errors="replace")) for t in decompilers if cls in sources[t]}
        if len(texts) < 2:
            agreement_rows.append({"class": cls.replace("/", "."), "tools": ",".join(texts), "min_similarity": ""})
            continue
        names = list(texts)
        sims = [_similarity(texts[a], texts[b]) for i, a in enumerate(names) for b in names[i + 1:]]
        agreement_rows.append({"class": cls.replace("/", "."), "tools": ",".join(names), "min_similarity": round(min(sims), 3)})
    scored = [r["min_similarity"] for r in agreement_rows if r["min_similarity"] != ""]
    roundtrip_rows = []
    for tool in decompilers:
        recomp = out_dir / "recompiled" / tool
        if not recomp.exists():
            continue
        for cls_file in recomp.rglob("*.class"):
            name = cls_file.relative_to(recomp).as_posix()[:-6]
            if name not in app_classes:
                continue
            try:
                roundtrip_rows.append({"tool": tool, "class": name.replace("/", "."),
                                       "bytecode_similarity": round(_bytecode_similarity(app_classes[name], cls_file.read_bytes()), 3)})
            except (ClassFormatError, IndexError, KeyError) as exc:
                roundtrip_rows.append({"tool": tool, "class": name.replace("/", "."), "bytecode_similarity": "", "error": str(exc)})
    for tool in decompilers:
        sims = [r["bytecode_similarity"] for r in roundtrip_rows if r["tool"] == tool and r["bytecode_similarity"] != ""]
        per_tool[tool]["roundtrip_classes"] = len(sims)
        per_tool[tool]["roundtrip_mean_similarity"] = round(sum(sims) / len(sims), 3) if sims else None
        per_tool[tool]["roundtrip_equivalent_pct"] = round(100 * sum(1 for s in sims if s >= 0.95) / len(sims), 1) if sims else None
    summary = {
        "per_tool": per_tool,
        "agreement": {"classes_compared": len(scored),
                      "mean_min_similarity": round(sum(scored) / len(scored), 3) if scored else None,
                      "classes_below_0_8": sum(1 for s in scored if s < 0.8)},
        "recommended_primary": max(per_tool, key=lambda t: (per_tool[t]["recompile_rate_pct"] or 0, -per_tool[t]["files_with_error_markers"], per_tool[t]["coverage_pct"])) if per_tool else None,
    }
    write_csv(out_dir / "fidelity-agreement.csv", agreement_rows, ["class", "tools", "min_similarity"])
    write_csv(out_dir / "fidelity-error-markers.csv", marker_rows, ["tool", "class", "error_markers"])
    write_csv(out_dir / "fidelity-recompile.csv", [r for rows in recompile_rows.values() for r in rows], ["tool", "file", "result", "first_error"])
    write_csv(out_dir / "fidelity-roundtrip.csv", roundtrip_rows, ["tool", "class", "bytecode_similarity", "error"])
    write_json(out_dir / "fidelity-summary.json", summary)
    return summary


def _bytecode_similarity(original: bytes, recompiled: bytes) -> float:
    a, b = parse_class(original), parse_class(recompiled)
    ma = {(m.name, m.descriptor): m for m in a.methods if m.code}
    mb = {(m.name, m.descriptor): m for m in b.methods if m.code}
    keys = set(ma) | set(mb)
    if not keys:
        return 1.0
    total = 0.0
    for key in keys:
        if key in ma and key in mb:
            total += difflib.SequenceMatcher(None, a.analyse(ma[key]).opcodes, b.analyse(mb[key]).opcodes, autojunk=False).ratio()
    return total / len(keys)
