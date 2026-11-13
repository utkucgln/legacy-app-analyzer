"""Command line: legacy-decompiler check|fetch-tools|run --config decompiler.json"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from legacy_decompiler import __version__
from legacy_decompiler.common import Timer, ToolError, docker_available, ensure_dir, jdk_tool, write_json

log = logging.getLogger("legacy_decompiler")
ALL_TOOLS = ["vineflower", "cfr", "procyon", "servlet-api", "jsp-api"]


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    base = path.resolve().parent

    def rel(value: str | None) -> str | None:
        if not value:
            return value
        p = Path(value)
        return str(p if p.is_absolute() else (base / p).resolve())

    if not cfg.get("artifact"):
        raise SystemExit("config: 'artifact' (path to the deployed WAR/EAR/JAR) is required")
    for key in ("artifact", "syft_path", "gitleaks_path"):
        cfg[key] = rel(cfg.get(key))
    cfg["output_dir"] = rel(cfg.get("output_dir", "decompiler-out"))
    cfg["tools_dir"] = rel(cfg.get("tools_dir", "tools"))
    cfg.setdefault("runner", "local")
    cfg.setdefault("jdk", {})
    if cfg["runner"] not in ("local", "docker"):
        raise SystemExit("config: runner must be 'local' (installed JDK, recommended on Windows Server) or 'docker'")
    return cfg


def _java_major(exe: str) -> int | None:
    proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=60, check=False)
    m = re.search(r'version "(\d+)(?:\.(\d+))?', proc.stderr + proc.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def cmd_check(cfg: dict) -> int:
    ok = True

    def line(status: str, text: str) -> None:
        print(f"[{status:<4}] {text}")

    artifact = Path(cfg["artifact"])
    if artifact.is_file() and zipfile.is_zipfile(artifact):
        line("ok", f"artifact {artifact} ({artifact.stat().st_size // 1024} KB)")
    else:
        ok = False
        line("FAIL", f"artifact {artifact} is missing or not a WAR/EAR/JAR (zip) file")
    if cfg["runner"] == "docker":
        if docker_available():
            line("ok", "docker engine reachable (Linux containers)")
        else:
            ok = False
            line("FAIL", "runner=docker but no Docker engine with Linux containers is reachable; use runner=local")
    else:
        jdk = cfg["jdk"]
        try:
            java = jdk_tool(jdk.get("home"), "java")
            major = _java_major(java)
            if major and major >= 11:
                line("ok", f"java {major} at {java}")
            else:
                ok = False
                line("FAIL", f"java at {java} reports version {major}; decompilers need JDK 11+ (JDK 17 or 21 LTS recommended)")
            line("ok", f"jdeps at {jdk_tool(jdk.get('home'), 'jdeps')}")
        except ToolError as exc:
            ok = False
            line("FAIL", str(exc))
        if cfg.get("recompile", True):
            home8 = jdk.get("recompile_home")
            if home8:
                try:
                    javac8 = jdk_tool(home8, "javac")
                    major = _java_major(jdk_tool(home8, "java"))
                    line("ok" if major == 8 else "WARN", f"recompile JDK {major} at {javac8}" + ("" if major == 8 else " (JDK 8 expected)"))
                except ToolError as exc:
                    ok = False
                    line("FAIL", str(exc))
            else:
                line("WARN", "jdk.recompile_home not set: sources are recompiled at level 8 with the main JDK (Java 5-7 code may differ)")
        for key, name, enabled in (("syft_path", "syft", cfg.get("sbom", True)), ("gitleaks_path", "gitleaks", cfg.get("gitleaks", True))):
            if not enabled:
                continue
            found = cfg.get(key) or shutil.which(name)
            if found and Path(found).exists():
                line("ok", f"{name} at {found}")
            else:
                line("WARN", f"{name} not found ({key} or PATH): that step will be skipped and reported in summary.json")
    tools = Path(cfg["tools_dir"])
    missing = [t for t in ALL_TOOLS if not (tools / f"{t}.jar").exists()]
    if missing:
        line("WARN", f"tool jars not staged in {tools}: {', '.join(missing)} - `run` downloads them (Maven Central / GitHub) "
                     "or stage them with `fetch-tools` on a connected machine")
    else:
        line("ok", f"tool jars staged in {tools}")
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
                enabled = winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
        except OSError:
            enabled = False
        line("ok" if enabled else "WARN", "Windows long paths " + ("enabled" if enabled else
                                                                   "disabled: deep package trees may exceed 260 characters; keep output_dir short"))
    print("READY" if ok else "NOT READY")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="legacy-decompiler", description=f"Legacy Decompiler {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name, text in (("check", "preflight: artifact, JDKs, tool jars, optional scanners"),
                       ("fetch-tools", "download and verify the decompiler jars into tools_dir (for offline staging)"),
                       ("run", "analyse the artifact, decompile with three decompilers and measure fidelity")):
        sub.add_parser(name, help=text).add_argument("--config", required=True, type=Path)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "fetch-tools":
        from legacy_decompiler.decompile import fetch_tools

        paths = fetch_tools(ensure_dir(Path(cfg["tools_dir"])), ALL_TOOLS)
        print(json.dumps({k: str(v) for k, v in paths.items()}, indent=2))
        return 0
    from legacy_decompiler import code_pillar

    timer = Timer()
    out = ensure_dir(Path(cfg["output_dir"]))
    summary = code_pillar.run(cfg, out, Path(cfg["tools_dir"]))
    write_json(out / "run-manifest.json", {"tool": f"legacy-decompiler {__version__}", "seconds": timer.seconds,
                                           "config": {k: v for k, v in cfg.items() if k != "jdk"}})
    print(json.dumps({k: summary.get(k) for k in ("app_package_prefix", "app_classes", "libraries", "jsps")}, indent=2))
    dec = summary.get("decompile", {}).get("fidelity", {})
    if dec:
        print(f"recommended primary decompiler: {dec.get('recommended_primary')}")
    for key in ("decompile_error", "jdeps_error", "sbom_error", "gitleaks_error"):
        if summary.get(key):
            print(f"{key}: {summary[key][:300]}")
    print(f"output: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
