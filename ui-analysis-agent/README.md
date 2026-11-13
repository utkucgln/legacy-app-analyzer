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

Single executable: `.\build_exe.ps1 [-WithEdgeEngine] [-WithCopilot]` produces `dist\ui-analyst.exe`. With `-WithCopilot` it also stages the Copilot runtime in `dist\copilot-runtime`. Copy the `dist` folder to the VM; Python is not needed there.

## Configure

1. Copy [examples/ui-agent.sample.json](examples/ui-agent.sample.json), or run `ui-analyst init my-app.json`.
2. Set the following:
   - `app.base_url`, `app.login_url`, `app.scope_prefixes`: the path prefixes the analyst may visit.
   - `roles`: user name, and the environment variable that holds the password, for each role.
   - Optional: `ai.endpoint` and `ai.model` (the Azure OpenAI endpoint and deployment name). If they are empty and the environment variables `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` are not set, `ui-analyst` asks for them when it starts.
   - `output_dir`.
3. Optional: `app.declared_paths_file` can point at the `actions.json` produced by the **Legacy Decompiler**. The access check then also tries declared Struts actions that are not reachable from the menus.
4. Optional: `app.sample_values` lists valid identifiers for search screens, as lists per field. Example: `{"policy number": ["<valid policy no>"], "customer id": ["<valid id>"]}`.

| Key | Default | Meaning |
|---|---|---|
| `browser.engine` | `ie` | `ie` = Edge IE mode (Selenium IEDriver); `edge` = standard Edge (Playwright). |
| `browser.proxy` | `direct` | `direct`, `system`, or `host:port`. |
| `safety.mode` | `read_only` | `read_only`, `probe` or `full`. Use `full` only on disposable environments. |
| `limits.max_steps_per_role` / `max_minutes` / `max_llm_calls` | 150 / 240 / 2000 | Budgets per run. |
| `phases` | explore, access_check, design, report on | Turn phases on or off. `probe` is off by default. |
| `design.target_design_system` | Fluent UI React v9 | Target of the UI component mapping. |
| `ai.endpoint` / `ai.model` | prompt | Azure OpenAI endpoint and deployment name. Precedence: environment variable, then config, then prompt. |
| `ai.api_key_env` | `AZURE_OPENAI_API_KEY` | Environment variable that holds the API key. The key is never read from the config. |
| `ai.copilot.cli_path` | auto | Path to a staged `copilot-runtime.exe` for offline VMs. |

## Run

```powershell
$env:AZURE_OPENAI_API_KEY = '<Azure OpenAI key>'
$env:UIA_PWD_MAKER   = '<maker password>'      # names come from roles.*.password_env
$env:UIA_PWD_CHECKER = '<checker password>'
.\.venv\Scripts\ui-analyst.exe check  --config my-app.json   # asks for endpoint/deployment if not configured; model, browser, login -> READY
.\.venv\Scripts\ui-analyst.exe run    --config my-app.json   # explore all roles, access check, report
.\.venv\Scripts\ui-analyst.exe design --config my-app.json   # UI design analysis of the explored screens
.\.venv\Scripts\ui-analyst.exe report --config my-app.json   # rebuild the report from state.json
```

Useful options:

- `run --resume` continues an interrupted run.
- `run --roles maker,checker` runs a subset of roles.
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
| HTTP 401 / HTTP 403 from the model | Check `AZURE_OPENAI_API_KEY`, the endpoint and the deployment name. The resource must allow key authentication, and its firewall must admit the VM (or use the private endpoint). |
| Unattended run stops with a configuration error about the endpoint | There is no console to prompt in; set `ai.endpoint` / `ai.model` or the `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` variables. |
| HTTP 429 | The deployment's TPM quota is exhausted. Calls are retried automatically; raise the quota or lower `limits.max_llm_calls`. |
| Search screens are never reached | Add valid identifiers to `app.sample_values`. |
