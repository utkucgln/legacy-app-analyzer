"""Command line: code-analyst check|run --config code-analysis.json"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from code_analyst import __version__
from code_analyst.common import describe_endpoint, ensure_dir, resolve_ai


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    base = path.resolve().parent

    def rel(value: str | None) -> str | None:
        if not value:
            return value
        p = Path(value)
        return str(p if p.is_absolute() else (base / p).resolve())

    cfg["output_dir"] = rel(cfg.get("output_dir", "code-analysis-out"))
    inp = cfg.setdefault("input", {})
    inp["decompiler_output"] = rel(inp.get("decompiler_output"))
    inp["source_dirs"] = [rel(p) for p in inp.get("source_dirs", [])]
    inp["web_dirs"] = [rel(p) for p in inp.get("web_dirs", [])]
    analysis = cfg.setdefault("analysis", {})
    analysis["cli_path"] = rel(analysis.get("cli_path"))
    cfg.setdefault("ai", {})
    if not inp["decompiler_output"] and not inp["source_dirs"]:
        raise SystemExit("config: set input.decompiler_output (Legacy Decompiler output folder) and/or input.source_dirs")
    return cfg

def gateway_hint(error: str) -> str:
    """Most likely configuration fix for a failed model call through Azure OpenAI or API Management."""
    if "reasoning_effort" in error or "Function tools" in error:
        return ("The deployment is a reasoning model, which supports function tools only through the Responses API: use "
                "analysis.wire_api 'responses' (the gateway must expose POST /responses). 'completions' works only with "
                "non-reasoning deployments such as gpt-4.1 / gpt-4o.")
    if "401" in error or "403" in error:
        return ("HTTP 401/403: wrong key, or the key is sent in the wrong header. Behind API Management set ai.auth_header to the "
                "API's subscription key header (APIM default 'Ocp-Apim-Subscription-Key'; Azure OpenAI imports often use 'api-key'), "
                "and put the APIM subscription key in the API key variable.")
    if "DeploymentNotFound" in error:
        return "The gateway accepted the key and the route; the deployment name (ai.model) does not exist behind it."
    if "404" in error:
        return ("HTTP 404: the gateway does not expose this operation or deployment. Check ai.api_style ('deployments' for "
                "/openai/deployments/<name>, 'v1' for /openai/v1), the endpoint path (APIM gateway URL + API URL suffix), the "
                "deployment name, and set analysis.wire_api to 'completions' if the API has no /responses operation.")
    if "certificate" in error.lower() or "self signed" in error.lower():
        return ("TLS: the Copilot runtime (Node.js) does not use the Windows certificate store; export the gateway's root CA to a "
                "PEM file and set NODE_EXTRA_CA_CERTS to its path.")
    return "Check network access to the endpoint (firewall, proxy HTTPS_PROXY, DNS) and run with -v for details."


def cmd_check(cfg: dict) -> int:
    from code_analyst.analysis import CodeAnalysis

    ok = True
    inp = cfg["input"]
    if inp["decompiler_output"]:
        dec = Path(inp["decompiler_output"]) / "decompiled"
        good = dec.is_dir() and any(dec.iterdir())
        ok = ok and (good or bool(inp["source_dirs"]))
        print(f"[{'ok  ' if good else 'FAIL'}] decompiled sources in {dec}")
    for d in inp["source_dirs"] + inp["web_dirs"]:
        good = Path(d).is_dir()
        ok = ok and good
        print(f"[{'ok  ' if good else 'FAIL'}] input folder {d}")
    analysis = CodeAnalysis(cfg, ensure_dir(Path(cfg["output_dir"])))
    try:
        endpoint, model = analysis._endpoint_model()
        print(f"[ok  ] deployment {model} at {describe_endpoint(cfg['ai'], analysis.c['wire_api'])}, API key from {cfg['ai']['api_key_env']}")
        print(f"[ok  ] Copilot runtime: {analysis.runtime_path() or 'SDK default (downloaded by install.ps1)'}")
    except SystemExit as exc:
        print(f"[FAIL] {exc}")
        print("NOT READY")
        return 2
    try:
        print(f"[ok  ] GitHub Copilot runtime -> {model}: {analysis.ping()[:40]}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] GitHub Copilot runtime / model call: {type(exc).__name__}: {str(exc)[:300]}")
        print("       " + gateway_hint(str(exc)))
    print("READY" if ok else "NOT READY")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code-analyst", description=f"Source Code Analysis Agents {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="preflight: inputs, Azure OpenAI settings, Copilot runtime and model reply").add_argument("--config", required=True, type=Path)
    s = sub.add_parser("run", help="build the workspace and run the architect, component, cross-cutting and planner agents")
    s.add_argument("--config", required=True, type=Path)
    s.add_argument("--prepare-only", action="store_true", help="build the sanitised workspace only, no model calls")
    s.add_argument("--only", default="", help="comma-separated packages/components to (re)analyse")
    s.add_argument("--fresh", action="store_true", help="discard earlier results in output_dir/state.json")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("urllib3",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.command == "check" and not args.verbose:
        logging.getLogger("copilot").setLevel(logging.ERROR)  # the SDK logs every failed JSON-RPC call with a traceback
    cfg = load_config(args.config)
    if args.command == "check" or not args.prepare_only:
        resolve_ai(cfg["ai"])
    if args.command == "check":
        return cmd_check(cfg)
    from code_analyst import analysis

    out = ensure_dir(Path(cfg["output_dir"]))
    handler = logging.FileHandler(out / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    summary = analysis.run(cfg, out, args.prepare_only, {s.strip() for s in args.only.split(",") if s.strip()}, args.fresh)
    print(json.dumps({k: v for k, v in summary.items() if k != "tool_calls"}, indent=2))
    print(f"report: {out / 'modernization-report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
