"""Configuration loading, defaults and the sample config written by `ui-analyst init`."""
from __future__ import annotations

import copy
import json
import os
import sys
import urllib.parse
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
        "modules": [],
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
           "api_key_env": "AZURE_OPENAI_API_KEY", "api_style": "v1", "api_version": "2025-04-01-preview", "auth_header": "api-key",
           "headers": {}, "vision": True, "send_table_samples": True,
           "runtime": "direct",
           "copilot": {"cli_path": None, "wire_api": "responses", "reasoning_effort": None, "max_turns": 12,
                       "turn_timeout_s": 1800, "runtime_telemetry": False}},
    "output_dir": "ui-analysis-out",
}

SAMPLE = {
    "app": {
        "name": "Legacy application",
        "base_url": "https://legacy-app-uat.example.internal/app",
        "login_url": "https://legacy-app-uat.example.internal/app/login.do",
        "scope_prefixes": ["/app/"],
        "start_paths": [],
        "declared_paths_file": None,
        "modules": [],
        "description": "One sentence on what the application is used for and by whom.",
    },
    "browser": {"engine": "edge", "edge_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "iedriver_path": None},
    "login": {"mode": "form"},
    "roles": {
        "operator": {"username": "operator.test", "password_env": "UIA_PWD_OPERATOR", "description": "Day-to-day user who records data"},
        "approver": {"username": "approver.test", "password_env": "UIA_PWD_APPROVER", "description": "Reviews and approves records"},
    },
    "safety": {"mode": "read_only"},
    "limits": {"max_steps_per_role": 150, "max_minutes": 240},
    "ai": {"endpoint": "", "model": "", "api_style": "v1", "api_version": "2025-04-01-preview", "auth_header": "api-key"},
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
    for section, key in (("browser", "iedriver_path"), ("browser", "edge_path")):
        value = cfg[section].get(key)
        if value and not Path(value).is_absolute():
            cfg[section][key] = str(base / value)
    cli_path = cfg["ai"]["copilot"].get("cli_path")
    if cli_path and not Path(cli_path).is_absolute():
        cfg["ai"]["copilot"]["cli_path"] = str(base / cli_path)
    ai = cfg["ai"]
    ai["endpoint"] = normalize_endpoint(_value(os.environ.get(ai["endpoint_env"]), ai["endpoint"]) or
                                        _ask("Azure OpenAI or APIM endpoint (e.g. https://<resource>.openai.azure.com)"),
                                        ai.get("api_style", "v1"))
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


API_STYLES = ("v1", "deployments", "base_url")


def normalize_endpoint(endpoint: str, api_style: str = "v1") -> str:
    """Root URL for the v1/deployments styles (a pasted .../openai or .../openai/v1 suffix is removed); base_url is kept as given."""
    base = endpoint.strip().rstrip("/")
    if api_style == "base_url":
        return base
    for suffix in ("/openai/v1", "/openai"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return base


def api_key(ai: dict) -> str:
    return os.environ.get(ai.get("api_key_env") or "AZURE_OPENAI_API_KEY", "").strip()


def chat_url(ai: dict) -> str:
    """Chat Completions URL for the configured API style (Azure OpenAI resource or an API Management gateway)."""
    base, style = ai["endpoint"].rstrip("/"), ai.get("api_style", "v1")
    if style == "deployments":
        return f"{base}/openai/deployments/{urllib.parse.quote(ai['model'], safe='')}/chat/completions?api-version={ai['api_version']}"
    if style == "base_url":
        return base + "/chat/completions"
    return base + "/openai/v1/chat/completions"


def request_headers(ai: dict) -> dict[str, str]:
    """Extra (non-secret) headers plus the key in the configured header: api-key, Ocp-Apim-Subscription-Key, Authorization ..."""
    key, header = api_key(ai), (ai.get("auth_header") or "api-key").strip()
    headers = {str(k): str(v) for k, v in (ai.get("headers") or {}).items()}
    headers[header] = f"Bearer {key}" if header.lower() == "authorization" else key
    return headers


def copilot_provider(ai: dict, wire_api: str) -> dict:
    """GitHub Copilot SDK BYOK provider for the same endpoint, style and key header as the direct client.

    The 'azure' provider type is always given the key explicitly: without one the runtime falls back to an Entra ID token
    of the signed-in Windows/Azure CLI identity, which must never be sent to a gateway.
    """
    key, header, style = api_key(ai), (ai.get("auth_header") or "api-key").strip(), ai.get("api_style", "v1")
    extra = {str(k): str(v) for k, v in (ai.get("headers") or {}).items()}
    base = ai["endpoint"].rstrip("/")
    if style == "deployments":
        provider: dict = {"type": "azure", "base_url": base, "wire_api": wire_api, "azure": {"api_version": ai["api_version"]}}
        if header.lower() == "authorization":
            provider["bearer_token"] = key
        else:
            provider["api_key"] = key  # sent as api-key
            if header.lower() != "api-key":
                extra[header] = key
    else:
        provider = {"type": "openai", "base_url": (base + "/" if style == "base_url" else base + "/openai/v1/"), "wire_api": wire_api}
        if header.lower() == "authorization":
            provider["api_key"] = key  # sent as Authorization: Bearer
        else:
            extra[header] = key
    if extra:
        provider["headers"] = extra
    return provider


def describe_endpoint(ai: dict) -> str:
    """URL and key header for diagnostics; never the key itself."""
    return f"{chat_url(ai)} (style {ai.get('api_style', 'v1')}, key in header '{ai.get('auth_header') or 'api-key'}')"


def _endpoint_scheme_ok(endpoint: str) -> bool:
    url = urllib.parse.urlsplit(endpoint)
    return url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"))


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
    elif not _endpoint_scheme_ok(cfg["ai"]["endpoint"]):
        problems.append("ai.endpoint must be an https:// URL")
    if cfg["ai"].get("api_style", "v1") not in API_STYLES:
        problems.append("ai.api_style must be 'v1' (/openai/v1), 'deployments' (/openai/deployments/<name>?api-version=, "
                        "classic Azure OpenAI and most API Management imports) or 'base_url' (OpenAI-compatible base URL used as is)")
    elif cfg["ai"].get("api_style") == "deployments" and not cfg["ai"].get("api_version"):
        problems.append("ai.api_version is required when ai.api_style is 'deployments'")
    if not isinstance(cfg["ai"].get("headers") or {}, dict):
        problems.append("ai.headers must be an object of header names and (non-secret) values")
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
