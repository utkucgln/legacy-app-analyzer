# Source Code Analysis Agents

These are AI agents that analyse a legacy Java web application **for modernisation**. They work on either:

- the reconstructed sources produced by the **Legacy Decompiler**, or
- the **original source code** once it is available. Both inputs can be used together.

The agents run on the GitHub Copilot SDK agent runtime, with your Azure OpenAI deployment (BYOK, API key), called directly or through API Management.

Before installing, read [PREREQUISITES.md](PREREQUISITES.md).

## How it works

1. **Workspace.** A sanitised, read-only copy of what the agents may read: `src/`, `web/`, `evidence/` and an `INDEX.md`. Secrets are masked.
   - From decompiler output: the recommended primary decompilation goes under `src/`. Classes where the decompilers disagree, or that did not recompile, also get the alternative versions under `alt/<tool>/` so the agents can compare them.
   - From source: files are arranged by their `package` declaration, so any repository layout works.
2. **Agents.**

   | Agent | Output |
   |---|---|
   | Architect | Architecture style, layers, frameworks and their lifecycle (current / outdated / end-of-life) with modern replacements, entry points, external systems, cross-cutting concerns, technical debt. |
   | Component Analyst | One session per package (large packages are split). It reads every class and records: business rules with values and locations; security, reliability, data-integrity and obsolete-technology findings (with CWE ids); data access and transactions; integrations; class-to-target mapping; strategy (retain … rebuild), complexity, effort, blockers, behaviour to preserve and tests to write. |
   | Cross-cutting Reviewer | Authentication and session handling, authorisation, input validation and output encoding, SQL construction, secrets and configuration, transactions, error handling, batch concurrency, integration retries. |
   | Modernization Planner | Target architecture, bounded contexts, data ownership, API surface, strangler-fig migration waves, decisions, risks, quick wins, effort bands. |

3. **Evidence verification.** Every rule and finding must quote the code verbatim. The quote is checked against the cited file, and the line number is computed locally. Unverifiable records are rejected and the agent corrects them.
4. **Resilience.** The analysis is resilient to failures:
   - transient model and network errors are retried with back-off;
   - a stall watchdog aborts a silent session and retries it with a fresh one;
   - a failed component does not stop the run;
   - state is saved after every phase, so the next `run` resumes where it stopped.

## Install

```powershell
cd source-code-analysis-agent
.\install.ps1                      # or -IndexUrl <mirror>; offline: -Wheelhouse D:\wheels -SkipRuntimeDownload
```

**No Python on the target (for example the application server):** run `.\build_exe.ps1` on a connected build machine. It produces `dist\code-analyst\code-analyst.exe`, a folder build with nothing unpacked to `%TEMP%`, with the pinned Copilot runtime staged in `dist\code-analyst\copilot-runtime\` and found automatically. Copy the folder across and use `code-analyst.exe` in place of `.\.venv\Scripts\code-analyst.exe`. `..\build-release.ps1` builds all three packages into one release folder.

## Configure

Copy [examples/code-analysis.sample.json](examples/code-analysis.sample.json) and adjust it. Relative paths are resolved against the folder that contains the config file.

| Key | Meaning |
|---|---|
| `input.decompiler_output` | The Legacy Decompiler `output_dir`. |
| `input.source_dirs` | Original Java source roots, for example `["D:\\src\\app\\src\\main\\java"]`. When set, the analysis uses the original source. |
| `input.web_dirs` | Web content and resources, for example `src\main\webapp` and `src\main\resources`. |
| `output_dir` | Results and resumable state. |
| `ai.endpoint`, `ai.model` | Azure OpenAI endpoint, or API Management gateway URL + API URL suffix, and the deployment name. Precedence: the environment variables `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT`, then the config, then a prompt at start. |
| `ai.api_style` | `v1` (default): `{endpoint}/openai/v1/…`. `deployments`: `{endpoint}/openai/deployments/<model>/…?api-version=…` and `{endpoint}/openai/responses?api-version=…`, for the classic Azure OpenAI route and most **API Management** imports. `base_url`: the endpoint is an OpenAI-compatible base URL, used as is. |
| `ai.api_version` | `2025-04-01-preview`. Used by `deployments` only. |
| `ai.auth_header` | Header that carries the key: `api-key` (default), `Ocp-Apim-Subscription-Key` (APIM default), or `Authorization` (sent as `Bearer <key>`). With `deployments` and a custom header, the key is also sent as `api-key`. |
| `ai.headers` | Extra non-secret headers for the gateway, for example `{"x-team": "modernisation"}`. |
| `ai.api_key_env` | Environment variable that holds the API key or the APIM subscription key (default `AZURE_OPENAI_API_KEY`). The key is never read from the config. |
| `analysis.wire_api` | `responses` (default, required for tool use with reasoning models) or `completions` (non-reasoning deployments only, for gateways without a `/responses` operation). |
| `analysis.reasoning_effort` | Optional `low` / `medium` / `high` for reasoning models (the runtime default is `medium`). |
| `analysis.target_stack` | The modernisation target that the agents map to (default: Java 21 / Spring Boot 3 / React / Azure Container Apps or AKS / Azure SQL or PostgreSQL / Entra ID). |
| `analysis.max_classes_per_component` / `max_components` | 25 / 60. Component sizing and cap. |
| `analysis.max_model_calls` | 2500. Hard budget for one run. |
| `analysis.idle_timeout_s` / `turn_timeout_s` / `session_attempts` | 300 / 2400 / 2. Stall watchdog, per-session limit, retries. |
| `analysis.cli_path` | Path to a staged `copilot-runtime.exe` (offline VMs). |

## Run

```powershell
$env:AZURE_OPENAI_API_KEY = '<Azure OpenAI key>'
.\.venv\Scripts\code-analyst.exe check --config code-analysis.json                 # asks for endpoint/deployment if not configured; real model call with one tool call -> READY
.\.venv\Scripts\code-analyst.exe run   --config code-analysis.json --prepare-only  # workspace only, no model calls
.\.venv\Scripts\code-analyst.exe run   --config code-analysis.json                 # full analysis (resumable)
.\.venv\Scripts\code-analyst.exe run   --config code-analysis.json --only com.example.orders   # (re)analyse selected packages
.\.venv\Scripts\code-analyst.exe run   --config code-analysis.json --fresh         # discard earlier results
```

To re-run the analysis on the original source after decompiled-code analysis, use a separate config whose `output_dir` is a new folder and whose `input.source_dirs` is set. You can then compare the two reports.

## Outputs (`output_dir`)

| File | Content |
|---|---|
| `modernization-report.html`, `.md` | Architecture, component table, target architecture and migration waves, decisions, risks, quick wins, findings, business rules, component details. |
| `architecture.json` | Architecture overview, frameworks and lifecycle, cross-cutting concerns. |
| `components.json` | Per-component assessment and modernisation strategy. |
| `rules.json` | Business rules with kind, values, verified file and line, certainty, modernisation note. |
| `findings.json` | Findings with category, severity, CWE, verified location, recommendation. |
| `modernization-plan.json` | Bounded contexts, migration waves, decisions, risks, quick wins, effort summary. |
| `facts.json` | Rules and findings as evidence facts, which can be combined with the other packages' outputs. |
| `summary.json`, `run.log` | Counts, token usage, tool calls, timings. |
| `workspace/`, `workspace.json` | The sanitised workspace the agents read. |
| `state.json` | Resumable state. Delete it, or use `--fresh`, to start over. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check` reports the API key missing | Set `AZURE_OPENAI_API_KEY` (or the variable named by `ai.api_key_env`) in the session that runs the tool. |
| `Authentication failed with provider ... (HTTP 401)` | Wrong key, endpoint or deployment name, or the resource does not allow key authentication. Behind API Management: `ai.auth_header` must match the API's subscription key header. |
| `Resource not found on provider ... (HTTP 404)` | Behind API Management: wrong `ai.endpoint` path or `ai.api_style`, or the API has no `POST /responses` operation (add it, or use a non-reasoning deployment with `wire_api` = `completions`). `DeploymentNotFound`: the key was accepted; fix `ai.model`. |
| `Function tools with reasoning_effort are not supported ... /chat/completions` | The deployment is a reasoning model: use `analysis.wire_api` = `responses`. |
| Certificate error at the Copilot runtime | The runtime is Node.js-based and does not use the Windows certificate store. Export the gateway's root CA as PEM and set `NODE_EXTRA_CA_CERTS`. |
| `check` fails at the Copilot runtime | Run `python -m copilot download-runtime`, or set `analysis.cli_path`. Check the firewall, proxy, DNS (private endpoint) and TLS inspection for the runtime's HTTPS call to Azure OpenAI. |
| Rate-limit warnings (HTTP 429) in `run.log` | Retried automatically. Raise the deployment's TPM quota, or analyse in batches with `--only`. |
| `attempt 1/2 failed: no session activity` | The watchdog recovered a stalled session. No action is needed unless both attempts fail; then re-run with `--only <package>`. |
| A component is reported `INCOMPLETE` | Re-run with `--only <component>`; the other results are kept. |
| `modernization-plan.json` is null | Re-run `run`; only the missing planning phase is executed. |
