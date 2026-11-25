# AI UI Analyst

The AI UI Analyst is a multi-agent explorer for legacy web applications. For each business role it:

- signs in to the application (Edge in Internet Explorer mode, or standard Edge);
- explores the screens safely and records the screens, fields, actions and navigation;
- checks which screens each role can and cannot reach;
- produces a functional and UI design analysis for modernisation.

Before installing, read [PREREQUISITES.md](PREREQUISITES.md).

## Agents

| Agent | Responsibility |
|---|---|
| Login | Finds the login controls, or uses configured selectors, and signs each role in. |
| Planner / Navigator | Decide which menu item, link or tab to explore next, within a step and time budget per role. |
| Guard | The safety policy. In `read_only` mode it blocks any action that could change data. Arabic and English keywords are covered. |
| Screen Analyst | Describes each screen: business purpose, fields, validations shown, actions and module. |
| Judge / Comparator | Detect duplicate screens, compare what each role sees, and run the access check (direct URL attempts per role). |
| Prober (optional) | In `probe` mode, submits invalid input to learn the validations. Use it only on a disposable test environment. |
| Designer | Measures the UI and audits it: tokens, components, accessibility, usability and legacy technology. It maps the result to a target design system (default: Fluent UI React v9). |
| Reporter | Writes the HTML and Markdown reports, the CSV/JSON datasets and `facts.json`. |

There are two runtimes, set with `ai.runtime`:

- **`direct`** (default): built-in orchestrator; each agent makes structured calls to the Azure OpenAI deployment.
- **`copilot`**: the GitHub Copilot SDK agent runtime supervises each role and delegates to the sub-agents above through browser tools. Inference still goes to your Azure OpenAI deployment (BYOK). It uses more tokens than `direct`.

## Install

```powershell
cd ui-analysis-agent
.\install.ps1                         # IE mode engine
.\install.ps1 -WithEdgeEngine -WithCopilot   # optional extras
```

Offline installation: on a connected machine run `pip download ".[edge,copilot]" -d D:\wheels`, copy the folder across, then run `.\install.ps1 -Wheelhouse D:\wheels`.

Single executable folder (no Python on the target, for example the application server): `.\build_exe.ps1 [-WithEdgeEngine] [-WithCopilot]` produces `dist\ui-analyst\ui-analyst.exe`. It is a folder build, so nothing is unpacked to `%TEMP%`, which suits AppLocker/WDAC and antivirus on locked-down servers. It also stages `dist\ui-analyst\drivers\IEDriverServer.exe` (Win32). With `-WithCopilot` it stages the pinned Copilot runtime in `dist\ui-analyst\copilot-runtime`, which is found automatically. Copy the `dist\ui-analyst` folder to the VM. `..\build-release.ps1` builds all three packages into one release folder.

## Configure

1. Copy [examples/ui-agent.sample.json](examples/ui-agent.sample.json), or run `ui-analyst init my-app.json`.
2. Set the following:
   - `app.base_url`, `app.login_url`, `app.scope_prefixes`: the path prefixes the analyst may visit.
   - `roles`: user name, and the environment variable that holds the password, for each role.
   - Optional: `ai.endpoint` and `ai.model` (the Azure OpenAI endpoint and deployment name). If they are empty and the environment variables `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` are not set, `ui-analyst` asks for them when it starts.
   - `output_dir`.
3. Optional: `app.declared_paths_file` can point at the `actions.json` produced by the **Legacy Decompiler**. The access check then also tries declared Struts actions that are not reachable from the menus.
4. Optional: `app.sample_values` lists valid identifiers for search screens, as lists per field. Example: `{"customer id": ["<valid id>"], "order number": ["<valid number>"]}`.
5. Optional: `app.modules` lists the application's functional areas, for example `["Customers", "Orders", "Administration"]`. Each screen is then classified into one of them, or `Other`. Leave it empty to let the analyst name the module of each screen itself.
6. If the sign-in is not found automatically, set the CSS selectors `login.username_selector`, `login.password_selector`, `login.submit_selector`, and optionally `login.success_text` (a text that appears only after a successful sign-in). Use `login.mode = "sso"` for single sign-on or Windows Integrated Authentication.

| Key | Default | Meaning |
|---|---|---|
| `browser.engine` | `ie` | `ie` = Edge IE mode (Selenium IEDriver), for pages that only work in Internet Explorer; `edge` = standard Edge (Playwright), with no driver and no administrator setup. |
| `app.start_paths` | `[]` | Extra paths relative to `app.base_url` (for example `"/reports/daily.do"`): given to the planner as entry hints and tried in the access check, like the declared actions. |
| `app.modules` | `[]` | Functional areas used to classify screens; empty means free classification. |
| `login.mode` | `form` | `form` (user name/password form), `sso` (already signed in by Windows/SSO) or `none`. |
| `browser.proxy` | `direct` | `direct`, `system`, or `host:port`. |
| `safety.mode` | `read_only` | `read_only`, `probe` or `full`. Use `full` only on disposable environments. |
| `limits.max_steps_per_role` / `max_minutes` / `max_llm_calls` | 150 / 240 / 2000 | Budgets per run. |
| `phases` | explore, access_check, design, report on | Turn phases on or off. `probe` is off by default. |
| `design.target_design_system` | Fluent UI React v9 | Target of the UI component mapping. |
| `ai.endpoint` / `ai.model` | prompt | Azure OpenAI endpoint, or API Management gateway URL + API URL suffix, and the deployment name. Precedence: environment variable, then config, then prompt. |
| `ai.api_style` | `v1` | `v1`: `{endpoint}/openai/v1/chat/completions`. `deployments`: `{endpoint}/openai/deployments/<model>/chat/completions?api-version=…`, the classic route used by most **API Management** imports. `base_url`: the endpoint is an OpenAI-compatible base URL, used as is. |
| `ai.api_version` | `2025-04-01-preview` | Used by `deployments` only. |
| `ai.auth_header` | `api-key` | Header that carries the key: `api-key`, `Ocp-Apim-Subscription-Key` (APIM default), or `Authorization` (sent as `Bearer <key>`). |
| `ai.headers` | `{}` | Extra non-secret headers for the gateway. |
| `ai.api_key_env` | `AZURE_OPENAI_API_KEY` | Environment variable that holds the API key or the APIM subscription key. The key is never read from the config. |
| `ai.copilot.wire_api` | `responses` | API used by the `copilot` runtime. Keep `responses` with reasoning models: they accept function tools only there. |
| `ai.copilot.cli_path` | auto | Path to a staged `copilot-runtime.exe`; found automatically next to the executable build. |

## Run

```powershell
$env:AZURE_OPENAI_API_KEY = '<Azure OpenAI key>'
$env:UIA_PWD_OPERATOR = '<operator password>'   # names come from roles.*.password_env
$env:UIA_PWD_APPROVER = '<approver password>'
.\.venv\Scripts\ui-analyst.exe check  --config my-app.json   # asks for endpoint/deployment if not configured; model, browser, login -> READY
.\.venv\Scripts\ui-analyst.exe run    --config my-app.json   # explore all roles, access check, report
.\.venv\Scripts\ui-analyst.exe design --config my-app.json   # UI design analysis of the explored screens
.\.venv\Scripts\ui-analyst.exe report --config my-app.json   # rebuild the report from state.json
```

Useful options:

- `run --resume` continues an interrupted run.
- `run --roles operator,approver` runs a subset of roles.
- `run --runtime copilot` overrides `ai.runtime` for this run.
- `design --redo` re-measures the screens and redoes the audits.

## Outputs (`output_dir`)

| File | Content |
|---|---|
| `report.html`, `report.md` | Functional UI analysis: screens per role and module, navigation, role matrix, blocked actions, coverage. |
| `screens.json`, `transitions.csv` | Screen inventory (fields, actions, purpose) and the navigation graph. |
| `role-matrix.csv`, `blocked-actions.csv` | Which role reaches which screen, and the actions the safety guard blocked. |
| `screenshots/<role>/` | One screenshot per distinct screen. |
| `design/design-report.html` | Design system, components, design tokens, accessibility and usability issues, legacy technology, target mapping. |
| `design/*.csv`, `design/design-tokens.json` | Machine-readable design outputs. |
| `facts.json`, `design/facts.json` | Evidence facts, which can be combined with the other packages' outputs. |
| `crawler-sessions.txt` | Session IDs used by the analyst, so its traffic can be excluded from server log analysis. |
| `state.json`, `llm-calls.jsonl`, `run.log` | Resumable state, model call log and run log. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Edge IE mode did not start` | Check the IE mode policy, IE ESC (off for the account), Protected Mode consistency, the `FEATURE_BFCACHE` key and `browser.iedriver_path`. Make sure the session is interactive and not minimised. |
| Session start takes about 2 minutes | Keep `browser.proxy = "direct"`, or set an explicit `host:port`. |
| Blank screenshots | The RDP window was minimised. Keep it open, or use the console session. |
| HTTP 401 / HTTP 403 from the model | Check `AZURE_OPENAI_API_KEY`, the endpoint and the deployment name. The resource must allow key authentication, and its firewall must admit the VM (or use the private endpoint). Behind API Management, `ai.auth_header` must match the API's subscription key header, and the key is the APIM subscription key. |
| HTTP 404 `Resource not found` from API Management | Wrong `ai.endpoint` path or `ai.api_style`. `DeploymentNotFound` means the key was accepted but `ai.model` is wrong. |
| Copilot runtime: certificate error | It is Node.js-based and does not read the Windows certificate store. Export the gateway's root CA as PEM and set `NODE_EXTRA_CA_CERTS`. |
| Long pauses between roles with `engine = "edge"` | Edge can take 20–100 s to exit on busy or heavily scanned VMs (measured in the lab). This happens once per role switch, not per screen. |
| Unattended run stops with a configuration error about the endpoint | There is no console to prompt in; set `ai.endpoint` / `ai.model` or the `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` variables. |
| HTTP 429 | The deployment's TPM quota is exhausted. Calls are retried automatically; raise the quota or lower `limits.max_llm_calls`. |
| Search screens are never reached | Add valid identifiers to `app.sample_values`. |
