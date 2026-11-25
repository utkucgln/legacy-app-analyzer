"""Command line for the AI UI Analyst (also the entry point of ui-analyst.exe)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from uiagent import __version__
from uiagent import config as config_mod


def _logging(out_dir: Path | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(out_dir / "run.log", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("urllib3", "selenium"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_init(path: Path) -> int:
    if path.exists():
        print(f"{path} already exists - not overwritten")
        return 1
    path.write_text(json.dumps(config_mod.SAMPLE, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path}. Set passwords in the environment variables named by roles.*.password_env, then run:\n"
          f"  ui-analyst check --config {path}\n  ui-analyst run --config {path}")
    return 0


def gateway_hint(error: str) -> str:
    """Most likely configuration fix for a failed model call through Azure OpenAI or API Management."""
    if "reasoning_effort" in error or "Function tools" in error:
        return ("The deployment is a reasoning model, which supports function tools only through the Responses API: use "
                "ai.copilot.wire_api 'responses' (the gateway must expose POST /responses), or ai.runtime 'direct'. "
                "'completions' works only with non-reasoning deployments such as gpt-4.1 / gpt-4o.")
    if "401" in error or "403" in error:
        return ("HTTP 401/403: wrong key, or the key is sent in the wrong header. Behind API Management set ai.auth_header to the "
                "API's subscription key header (APIM default 'Ocp-Apim-Subscription-Key'; Azure OpenAI imports often use 'api-key'), "
                "and put the APIM subscription key in the API key variable.")
    if "DeploymentNotFound" in error:
        return "The gateway accepted the key and the route; the deployment name (ai.model) does not exist behind it."
    if "404" in error:
        return ("HTTP 404: the gateway does not expose this operation or deployment. Check ai.api_style ('deployments' for "
                "/openai/deployments/<name>, 'v1' for /openai/v1), the endpoint path (APIM gateway URL + API URL suffix), the "
                "deployment name, and ai.copilot.wire_api ('completions' if the API has no /responses operation).")
    if "certificate" in error.lower() or "CERTIFICATE" in error:
        return ("TLS: add the gateway's root CA to the Windows certificate store (direct runtime). The Copilot runtime (Node.js) "
                "does not use that store: export the CA to a PEM file and set NODE_EXTRA_CA_CERTS to its path.")
    return "Check network access to the endpoint (firewall, proxy HTTPS_PROXY, DNS) and run with -v for details."


def cmd_check(cfg: dict) -> int:
    from uiagent import browser as browser_factory
    from uiagent.agents.login import LoginAgent
    from uiagent.llm import LLM

    ok = True
    logging.getLogger("copilot").setLevel(logging.ERROR)  # the SDK logs every failed JSON-RPC call with a traceback
    print(f"[1/3] model {cfg['ai']['model']} at {config_mod.describe_endpoint(cfg['ai'])}")
    llm = LLM(cfg["ai"])
    try:
        reply = llm.structured("check", "Reply tersely.", "Say OK.", "ping",
                               {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}},
                               max_tokens=100)
        print(f"      ok ({reply.get('answer')})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"      FAILED: {exc}")
        print("      " + gateway_hint(str(exc)))
    if cfg["ai"]["runtime"] == "copilot":
        print("[1b]  GitHub Copilot SDK runtime (BYOK to the same deployment)")
        try:
            from uiagent import copilot_runtime

            print(f"      ok ({copilot_runtime.ping(cfg, Path(cfg['output_dir']))})")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"      FAILED: {type(exc).__name__}: {exc}")
            print("      " + gateway_hint(str(exc)))
    print(f"[2/3] browser engine '{cfg['browser']['engine']}'")
    for role, spec in cfg["roles"].items():
        b = browser_factory.create(cfg)
        try:
            b.start()
            print(f"[3/3] login as {role} ...", end=" ")
            success = LoginAgent(llm, cfg).login(b, role, spec.get("username", ""), config_mod.role_password(spec))
            print("ok" if success else "FAILED")
            ok = ok and success
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"FAILED: {exc}")
            if cfg["browser"]["engine"] == "ie":
                print("      IE mode checklist: Edge installed at browser.edge_path; IE mode allowed by policy; "
                      "IEDriverServer reachable (iedriver_path or Selenium Manager download); Protected Mode identical "
                      "for all zones (or ignore_protected_mode_settings); zoom 100%.")
        finally:
            try:
                b.stop()
            except Exception:  # noqa: BLE001
                pass
    print("READY" if ok else "NOT READY")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ui-analyst", description=f"AI UI Analyst {__version__}: multi-agent exploration of legacy web applications")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("init", help="write a sample configuration file")
    s.add_argument("path", type=Path, nargs="?", default=Path("ui-agent.json"))
    for name, text in (("check", "preflight: model access, browser start and login for every role"),
                       ("run", "explore all roles, check access, compare roles and write the report"),
                       ("design", "UI design analysis of the explored screens: design system, components, accessibility, usability"),
                       ("report", "rebuild the report from state.json")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--config", required=True, type=Path)
        if name in ("run", "design"):
            s.add_argument("--target", help="target design system for the modernisation mapping (default: Fluent UI React v9)")
        if name == "design":
            s.add_argument("--redo", action="store_true", help="re-measure the screens and redo the audits and synthesis")
        if name in ("check", "run"):
            s.add_argument("--runtime", choices=["direct", "copilot"], help="override ai.runtime")
        if name == "run":
            s.add_argument("--resume", action="store_true", help="continue from state.json")
            s.add_argument("--roles", default="", help="comma-separated subset of roles")
        if name == "report":
            s.add_argument("--no-ai", action="store_true", help="skip the AI narrative")
    args = p.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.path)
    cfg = config_mod.load(args.config)
    if getattr(args, "runtime", None):
        cfg["ai"]["runtime"] = args.runtime
    if getattr(args, "target", None):
        cfg["design"]["target_design_system"] = args.target
    _logging(Path(cfg["output_dir"]) if args.command != "check" else None, args.verbose)
    if args.command == "check":
        return cmd_check(cfg)
    from uiagent.orchestrator import Orchestrator

    if args.command == "design":
        from uiagent.design import DesignAnalysis

        orch = Orchestrator(cfg, resume=True)
        if not orch.state.screens:
            print("no explored screens in state.json - run `ui-analyst run` first")
            return 1
        if args.redo:
            orch.state.design_raw, orch.state.design_audits, orch.state.design_system = {}, {}, None
        summary = DesignAnalysis(orch).run()
        orch.write_report()
        print(json.dumps(summary, indent=2))
        return 0
    if args.command == "run":
        if args.roles:
            keep = {r.strip() for r in args.roles.split(",")}
            cfg["roles"] = {k: v for k, v in cfg["roles"].items() if k in keep}
        summary = Orchestrator(cfg, resume=args.resume).run()
    else:
        orch = Orchestrator(cfg, resume=True)
        if args.no_ai:
            from uiagent import report

            summary = report.write(orch.out, cfg, orch.state, orch.matrix(), None, orch.coverage())
        else:
            summary = orch.write_report()
    print(json.dumps({k: summary.get(k) for k in ("screens_total", "screens_per_role", "blocked_actions", "errors", "llm_calls", "facts")}, indent=2))
    print(f"report: {Path(cfg['output_dir']) / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
