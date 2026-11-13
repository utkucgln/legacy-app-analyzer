"""Configuration loading, defaults and the sample config written by `ui-analyst init`."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "Legacy application",
        "base_url": "",
        "login_url": "",
        "scope_prefixes": [],
        "start_paths": [],
        "declared_paths_file": None,
        "sample_values": {},
        "modules": ["Master Management", "Policy Management", "Financial Functions", "Compliance Workflow", "Integrations"],
        "description": "",
    },
    "browser": {
        "engine": "ie",
        "edge_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "iedriver_path": None,
        "ignore_protected_mode_settings": False,
        "proxy": "direct",
        "headless": False,
        "page_timeout_s": 60,
        "window": [1600, 1000],
    },
    "login": {
        "mode": "form",
        "username_selector": None,
        "password_selector": None,
        "submit_selector": None,
        "success_text": None,
    },
    "roles": {},
    "safety": {
        "mode": "read_only",
        "deny_words": ["approve", "reject", "decline", "delete", "remove", "cancel", "submit", "save", "execute", "run", "post",
                       "pay", "confirm", "activate", "deactivate", "suspend", "terminate", "reverse", "refund", "lapse", "reinstate",
                       "authorise", "authorize", "release", "unlock", "lock", "reset", "close", "send", "purge", "archive", "upload",
                       "import", "commit", "update", "logout", "logoff", "sign out", "signout", "exit",
                       "اعتماد", "رفض", "حذف", "إلغاء", "حفظ", "إرسال", "تنفيذ", "تأكيد", "خروج"],
        "query_words": ["search", "find", "view", "show", "display", "go", "filter", "list", "enquiry", "inquiry", "lookup", "refresh",
                        "next", "previous", "page", "بحث", "عرض", "استعلام"],
        "deny_url_patterns": ["logout", "logoff", "signout", "delete", "remove"],
        "dismiss_confirm_dialogs": True,
        "mask_patterns_in_prompts": True,
    },
    "limits": {"max_steps_per_role": 150, "max_llm_calls": 2000, "max_minutes": 240, "max_visits_per_screen": 4,
               "max_probe_cases_per_form": 4},
    "phases": {"explore": True, "access_check": True, "probe": False, "design": True, "report": True},
    "design": {"target_design_system": "Fluent UI React v9", "max_screens": 200},
    "ai": {"endpoint": "", "model": "", "endpoint_env": "AZURE_OPENAI_ENDPOINT", "model_env": "AZURE_OPENAI_DEPLOYMENT",
           "api_key_env": "AZURE_OPENAI_API_KEY", "vision": True, "send_table_samples": True,
           "runtime": "direct",
           "copilot": {"cli_path": None, "wire_api": "responses", "reasoning_effort": None, "max_turns": 12,
                       "turn_timeout_s": 1800, "runtime_telemetry": False}},
    "output_dir": "ui-analysis-out",
}

SAMPLE = {
    "app": {
        "name": "BANCA",
        "base_url": "https://banca.example.internal/banca",
        "login_url": "https://banca.example.internal/banca/login.do",
        "scope_prefixes": ["/banca/"],
        "start_paths": [],
        "declared_paths_file": None,
        "description": "Bancassurance servicing and recording platform used by branch operations.",
    },
    "browser": {"engine": "ie", "edge_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "iedriver_path": None},
    "login": {"mode": "form"},
    "roles": {
        "maker": {"username": "ops.maker.test", "password_env": "UIA_PWD_MAKER", "description": "Branch operations maker"},
        "checker": {"username": "ops.checker.test", "password_env": "UIA_PWD_CHECKER", "description": "Operations checker / approver"},
    },
    "safety": {"mode": "read_only"},
    "limits": {"max_steps_per_role": 150, "max_minutes": 240},
    "ai": {"endpoint": "", "model": ""},
    "output_dir": "ui-analysis-out",
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "roles":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path) -> dict:
    cfg = _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
    base = path.resolve().parent
    out = Path(cfg["output_dir"])
    cfg["output_dir"] = str(out if out.is_absolute() else base / out)
    dp = cfg["app"].get("declared_paths_file")
    if dp and not Path(dp).is_absolute():
        cfg["app"]["declared_paths_file"] = str(base / dp)
    ai = cfg["ai"]
    ai["endpoint"] = normalize_endpoint(_value(os.environ.get(ai["endpoint_env"]), ai["endpoint"]) or
                                        _ask("Azure OpenAI endpoint (e.g. https://<resource>.openai.azure.com)"))
    ai["model"] = _value(os.environ.get(ai["model_env"]), ai["model"]) or _ask("Azure OpenAI deployment name")
    validate(cfg)
    return cfg


def _value(*candidates: str | None) -> str:
    """First real value; template placeholders such as <resource> count as unset."""
    return next((c.strip() for c in candidates if c and c.strip() and "<" not in c), "")


def _ask(prompt: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    return input(f"{prompt}: ").strip()


def normalize_endpoint(endpoint: str) -> str:
    """Resource root URL; a pasted .../openai or .../openai/v1 suffix is removed."""
    base = endpoint.strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return base


def api_key(ai: dict) -> str:
    return os.environ.get(ai.get("api_key_env") or "AZURE_OPENAI_API_KEY", "").strip()


def validate(cfg: dict) -> None:
    problems = []
    if not cfg["app"]["base_url"]:
        problems.append("app.base_url is required")
    if not cfg["roles"]:
        problems.append("at least one role is required")
    if cfg["browser"]["engine"] not in ("ie", "edge"):
        problems.append("browser.engine must be 'ie' (Edge IE mode) or 'edge'")
    if cfg["ai"]["runtime"] not in ("direct", "copilot"):
        problems.append("ai.runtime must be 'direct' (built-in orchestrator) or 'copilot' (GitHub Copilot SDK runtime)")
    if cfg["safety"]["mode"] not in ("read_only", "probe", "full"):
        problems.append("safety.mode must be read_only, probe or full")
    if not cfg["ai"]["endpoint"] or not cfg["ai"]["model"]:
        problems.append("Azure OpenAI endpoint and deployment are required (ai.endpoint/ai.model, the env vars named by "
                        "endpoint_env/model_env, or the prompt)")
    elif not cfg["ai"]["endpoint"].lower().startswith("https://"):
        problems.append("ai.endpoint must be an https:// URL")
    if not api_key(cfg["ai"]):
        problems.append(f"set the Azure OpenAI API key in the environment variable {cfg['ai'].get('api_key_env')}")
    for name, role in cfg["roles"].items():
        if cfg["login"]["mode"] == "form" and not role.get("username"):
            problems.append(f"roles.{name}.username is required for form login")
    if problems:
        raise SystemExit("configuration errors:\n  - " + "\n  - ".join(problems))


def role_password(role: dict) -> str:
    env = role.get("password_env")
    return os.environ.get(env, "") if env else ""
