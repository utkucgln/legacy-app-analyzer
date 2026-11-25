# Readiness report: running the toolkit on the application server

**Date:** 25 November 2026 · **Scope:** everything in this repository · **Verdict:** **Ready for testing on the target environment**, subject to the five preconditions in section 7.

## 1. Target environment assessed

| Environment fact | Consequence for the toolkit |
|---|---|
| The legacy Java application runs on a Windows VM, and only the compiled application is there | The Legacy Decompiler must run against the deployed WAR/EAR, using whatever Java exists on the server. |
| The web UI can be launched on the same server | The AI UI Analyst can run there, against the UAT URL. |
| The VM is small and has restrictions | No Python/pip installation, no internet except to the LLM, possibly AppLocker/WDAC and no administrator rights; memory must be conserved. |
| Firewall to the LLM is open | Only the APIM gateway (HTTPS 443) is reachable. All other dependencies must be pre-staged. |
| Azure OpenAI sits behind APIM | Endpoint URL shape, subscription key header and the operations exposed by APIM decide whether the AI tools work. |

## 2. Summary of findings

The original packages were written for a **separate analysis VM with Python** and for a **direct Azure OpenAI endpoint**. On an application server behind APIM, **both AI tools would have failed at the first model call**, the packaged UI Analyst could not have used IE mode at all, and the decompiler would have reported NOT READY on a typical legacy Java installation. All findings below were fixed and re-tested with the built executables.

| # | Severity | Finding (before) | Fix (now) |
|---|---|---|---|
| F1 | **Blocking** | Both AI tools hard-coded `{endpoint}/openai/v1/…`. APIM APIs imported with *Azure OpenAI* compatibility (the usual case) only route `/openai/deployments/{name}/…?api-version=…`, so requests returned **HTTP 404**. | New `ai.api_style` setting: `v1`, `deployments` or `base_url`, plus `ai.api_version`. |
| F2 | **Blocking** | The Copilot runtime (Code Analyst, and the UI Analyst `copilot` runtime) sent the key as `Authorization: Bearer <key>`. APIM ignores this by default and requires `Ocp-Apim-Subscription-Key` or its configured header, so requests returned **HTTP 401**. The direct client hard-coded `api-key`. | New `ai.auth_header` setting (`api-key` / `Ocp-Apim-Subscription-Key` / `Authorization` / custom), plus `ai.headers` for extra non-secret headers. Both clients and the Copilot provider use them. |
| F3 | High (security) | A probe showed that the Copilot SDK `azure` provider **without** an explicit key sends the **signed-in Entra ID token of the machine or user** to the endpoint. | The provider mapping always passes the APIM key explicitly. A traffic capture confirmed that no `Authorization` token reaches the gateway unless `auth_header = Authorization` is chosen. |
| F4 | **Blocking** | Only the UI Analyst had an executable build. It was a one-file build that unpacks itself to `%TEMP%` on every start, which AppLocker/WDAC and antivirus often block and which is slow on a weak VM. The decompiler and the Code Analyst required Python and pip on the server. | `build_exe.ps1` for all three packages, using PyInstaller folder builds, and a root `build-release.ps1`. That script assembles an offline `release\` with the exes, one shared Copilot runtime, IEDriverServer, the decompiler jars, APIM-ready configuration templates and `SHA256SUMS.txt`. |
| F5 | High | Decompiler `check` returned **NOT READY for Java 8 or a JRE**, which is common on legacy servers. If recompilation failed (no `javac`), the whole fidelity result was discarded. The Java heap was fixed at 2 GB. | Java 8 is now accepted: CFR and Procyon run, and Vineflower (Java 11+) is skipped with a note. A JRE is accepted: recompilation and `jdeps` are skipped with a reason, and the decompiled sources and `fidelity-summary.json` are kept. New `jdk.heap` setting. |
| F6 | High | `check` for the Copilot runtime sent a prompt **without tools**, so it could print READY while the real run failed. Reproduced: with `wire_api = completions` and a reasoning model, every agent session failed with *"Function tools with reasoning_effort are not supported … /chat/completions"*. | The preflight now performs a real **tool-call round trip**. Failures print a targeted hint for 401 (header), 404 (path/style/operation), reasoning-tool, TLS and network errors. |
| F7 | Medium | IE engine: with current Selenium, a driver start failure surfaced as `'Service' object has no attribute 'process'`, which hid the real cause. | The cleanup is defensive, so the real reason is now reported (for example *"no IE-mode session after 60s"*). |
| F8 | Medium | The Copilot SDK was unpinned (`>=1.0.31`). Two builds a few hours apart pulled different runtimes (1.0.109 and 1.0.112). | Pinned to `github-copilot-sdk==1.0.34` (runtime 1.0.112), the version validated here. |
| F9 | Low | Error bodies from APIM (`{"statusCode":401,"message":"Access denied…"}`) were discarded, leaving an empty error code. | APIM and Azure OpenAI error shapes are both parsed into the message. |
| F10 | Low | `browser.iedriver_path` and `ai.copilot.cli_path` were not resolved relative to the config file. `http://` was refused even for loopback test gateways. | Relative paths are resolved. `http://` is allowed only for `localhost` / `127.0.0.1`. |
| F11 | Docs | The documents stated that nothing runs on the application VM, covered only direct Azure OpenAI endpoints and required Python everywhere. | READMEs, PREREQUISITES and samples were updated, and an operator guide (`release-template\README.md`) was added for the application server. |
| F12 | **Blocking** | **IE mode could never start in the packaged UI Analyst.** The executable build collected Selenium's data files but not its lazily imported submodules, so the exe failed with `No module named 'selenium.webdriver.ie.webdriver'`. The original `build_exe.ps1` had the same flag. This was found only after F7 exposed the real error. | `--collect-submodules selenium` was added. The packaged exe now behaves exactly like the Python source. |
| F13 | Medium | The build scripts built inside the package folder. In a synced folder (OneDrive), or on a second run, the setuptools/PyInstaller work folders failed intermittently with `WinError 183`; this was reproduced three times. | The sources are staged in `%TEMP%` and built there; only `dist\` is written back. |
| F14 | High | The UI Analyst classified every screen into a **fixed, industry-specific module list** (`app.modules` default, undocumented), so any other application got wrong module labels. | The default is now empty: the analyst names the module freely, or uses the documented `app.modules` list when one is set. |
| F15 | Medium | `app.start_paths` appeared in every sample but was never read. | Start paths are now passed to the planner and tried in the access check, like the declared actions. They are documented together with the login selectors (`login.*_selector`, `success_text`) needed for non-standard sign-in pages. |
| F16 | Low | Samples, templates and the `facts.json` field name were tied to one specific application. | Neutral samples (`app.war`, `operator` / `approver` roles). The `facts.json` field is now `business_module` in all three tools. |

## 3. What was verified, and how

**Test bed:** Windows 11 Enterprise x64 (8 vCPU, 32 GB), Edge 158, a sample legacy web application (Struts 1 / Tomcat / MySQL with mocked external services, 114 classes, Java 5 bytecode, 27 JSPs) and a reasoning-capable Azure OpenAI deployment. All tests used the **built executables**, not Python.

**Two kinds of APIM:**

1. **Real APIM** (an existing enterprise gateway in a Microsoft test tenant), used read-only. It is an enterprise *AI gateway* pattern: classic `/openai/deployments/…` import, `api-key` subscription header and model-based backend pools. It authenticated the requests and forwarded them to its backend. That backend had no chat deployment, so it answered `DeploymentNotFound`. This proves URL shape and key handling, but not inference.
2. **APIM simulator** for full inference. It mimics an *Azure OpenAI* import: API suffix `/aoai/openai`, `Ocp-Apim-Subscription-Key`, only the imported operations routed, APIM 401/404 bodies, client key stripped, and a managed-identity-style Entra token sent to the backend. It is the strict case, because it is not the `api-key` default.

| Area | Test | Result |
|---|---|---|
| Copilot runtime request shapes | Local capture of 10 provider configurations | Mapped exact URL and header behaviour (section 5); found F2 and F3. |
| Decompiler exe, JDK 17 + JDK 8 | `check` + `run` on the sample WAR, `-Xmx1g` | READY; 3 decompilers; recompile CFR 93.5% / Procyon 97.4% / Vineflower 83.1%; primary **procyon**; **57 s** |
| Decompiler exe, JDK 8 only | same | READY (WARN); CFR + Procyon; recompile 96.1%; primary procyon; **25 s** |
| Decompiler exe, **JRE 8 only** | same | READY (WARN); CFR + Procyon decompiled 100%; recompile/jdeps skipped with reason; `actions.json` produced; **10 s** |
| Code Analyst exe → simulator | `check`, `wire_api = responses` | READY, including the tool call (`/aoai/openai/responses?api-version=2025-04-01-preview`) |
| Code Analyst exe → simulator | `run --only <one 5-class package>` | Architecture + component done; **16 verified business rules, 24 findings**; 22 calls, 614K input / 19K output tokens, 8 min; one 429 retried automatically |
| Code Analyst exe → simulator | `check` with the wrong header, and with the old v1/Bearer behaviour | NOT READY, HTTP 401, with the header hint |
| Code Analyst exe → simulator | `check`, `wire_api = completions` with a reasoning model | NOT READY, with the reasoning-tools hint (before the fix this case passed `check` and then failed every session) |
| Code Analyst exe → **real APIM** | `check`, `deployments`, `api-key` | Authentication and route accepted; backend returned 404 because the gateway has no chat model |
| UI Analyst exe → simulator | `check`, `engine = edge`, direct runtime | READY: model call (JSON schema), Edge start, login of 2 roles |
| UI Analyst exe → simulator | `check --runtime copilot` | READY, including the Copilot BYOK tool call |
| UI Analyst exe → simulator | `run` (2 roles, 12 steps each, read-only) | **7 screens**, role matrix, **20 data-changing actions blocked**, design audit, 69 facts, 51 calls, 0 errors, 22 min (including one 9-min network stall that was retried automatically) |
| UI Analyst exe, `engine = ie` | `check` | **Not validated on this machine.** IE mode did not start (*no IE-mode session after 60s*), with the Python source and with the packaged exe after F12. The test VM lacks the HKLM IE-mode prerequisites, which were not changed on a shared machine. |
| UI Analyst exe → **real APIM** | `check`, `deployments`, `api-key` / wrong header | Key and route accepted, then `HTTP 404 DeploymentNotFound` (no chat model on that gateway). With `Ocp-Apim-Subscription-Key`: `HTTP 401 Access denied due to missing subscription key`, with the correct hint. |
| **Release acceptance** | Fresh copy of `release\`, checksums verified with the README snippet, shipped templates filled in, every tool run from the copy | 1,710/1,710 checksums OK. Decompiler READY + run (Java 8). Code Analyst READY (shared runtime found in `..\copilot-runtime`, tool call OK). UI Analyst READY (edge engine, direct + Copilot runtime). |

## 4. How to configure APIM (what to ask the APIM team)

| Question | Config key | Notes |
|---|---|---|
| Gateway URL and API URL suffix of the Azure OpenAI API | `ai.endpoint` | `https://<gateway>/<suffix>`; a trailing `/openai` is accepted |
| Is the API the classic import (`/openai/deployments/{id}/…?api-version=`) or *Azure OpenAI v1*? | `ai.api_style` | `deployments` (most common) or `v1` |
| Subscription key header name | `ai.auth_header` | `api-key` is common for Azure OpenAI APIs (as on the real APIM tested); otherwise `Ocp-Apim-Subscription-Key` |
| A subscription key for a product that includes this API | `AZURE_OPENAI_API_KEY` (session only) | Rotate it after the analysis |
| Deployment name(s) routed by the gateway | `ai.model` | A JSON-schema + vision capable model for the UI Analyst; a model with tool calling for the Code Analyst |
| Are `POST /responses` **and** `POST /deployments/{id}/chat/completions` exposed? | `wire_api` | **Reasoning models need `/responses` for tool use** (verified). If the API has no Responses operation, add it, re-import from a recent specification, or use a non-reasoning deployment with `wire_api = completions`. |
| Token-limit policy, request-size limit, timeouts | | The UI Analyst sends screenshots (about 0.2–1 MB per call). Agent turns can last minutes. 429s are retried. |
| Is request/response **body logging** enabled (Application Insights / Log Analytics)? | | Body logging would copy decompiled source code and screen content into the logs. Agree on this with the data owner. |
| TLS: is the gateway certificate issued by an internal CA? | `NODE_EXTRA_CA_CERTS` | Python uses the Windows store. The Copilot runtime (Node.js) needs the CA as a PEM file. |

## 5. Verified Copilot runtime behaviour (reference for support)

| Provider sent by the tools | URL | Headers |
|---|---|---|
| `deployments` + `api-key` | `{endpoint}/openai/deployments/{model}/chat/completions?api-version=…` or `{endpoint}/openai/responses?api-version=…` | `api-key` |
| `deployments` + `Ocp-Apim-Subscription-Key` | same | `Ocp-Apim-Subscription-Key` **and** `api-key` (same APIM key; the SDK always sends `api-key` for this provider type) |
| `deployments` + `Authorization` | same | `Authorization: Bearer <key>` |
| `v1` / `base_url` + any custom header | `{endpoint}/openai/v1/…` or `{endpoint}/…` | only the configured header |

`analysis.reasoning_effort = "none"` is **not** passed to the model by the runtime, so it is not a workaround for reasoning models on `/chat/completions`.

## 6. Release folder (`release\`, also attached to the GitHub Release as a zip)

Built by `build-release.ps1` (Python 3.12 build machine, about 6 minutes): **434 MB, 1,710 files**. The application server needs **no Python, no pip and no internet**; only the APIM gateway must be reachable.

| Folder | Content |
|---|---|
| `legacy-decompiler\` | `legacy-decompiler.exe` and `tools\` (Vineflower 1.10.1, CFR 0.152, Procyon 0.6.0, servlet/jsp APIs; SHA-1 verified against Maven Central) |
| `code-analyst\` | `code-analyst.exe` (Copilot SDK 1.0.34) |
| `ui-analyst\` | `ui-analyst.exe` (Selenium + Playwright engines), `drivers\IEDriverServer.exe` 4.14.0 Win32 |
| `copilot-runtime\1.0.112\` | Shared runtime, found automatically by both AI exes |
| `config\` | `decompiler.json`, `code-analysis.json`, `ui-agent.json`: APIM `deployments` style, `jdk.heap = 1g`, relative paths |
| `input\`, `work\` | Artifact in, all outputs out |
| `README.md`, `SHA256SUMS.txt` | Operator guide and integrity list |

## 7. Preconditions and residual risks for the first test

| # | Item | Owner | Mitigation |
|---|---|---|---|
| P1 | **APIM answers** (section 4), especially the header name and a `/responses` operation for the Code Analyst | APIM team | `check` names the exact failing URL and header and prints the fix. |
| P2 | **Java** on the application server for the decompiler | Application team | A portable JDK 17 ZIP (no installation, no admin) gives full results; Java 8 / JRE give reduced but usable results (verified). |
| P3 | **Executable allow-listing** (AppLocker/WDAC) for 3 exes and `copilot-runtime.exe`; antivirus exclusion for `work\` | Security team | Folder builds avoid `%TEMP%` extraction. `SHA256SUMS.txt` supports approval. Unblock the downloaded zip before extracting. |
| P4 | **UI engine choice**: IE mode needs a one-time administrator setup (policy, `FEATURE_BFCACHE`, IE ESC, Protected Mode) and could not be validated on the test VM | Server administrator | The template uses `engine = "edge"` (no admin, no driver; verified end to end). Use IE mode only for IE-only pages, after its `check` prints READY. |
| P5 | **Load on the application server** | Application team | Run one tool at a time, off-peak. Decompiler heap 1 GB. UI Analyst ≈ 1–1.5 GB with Edge. Point the UI Analyst at **UAT**, never at production, even from the same server. Use read-only test accounts without MFA. |
| R1 | Edge shutdown took 2–100 s per browser restart on the test VM (environmental) | – | Happens once per role or login check, not per screen; documented. |
| R2 | The real APIM in the test bed had no chat deployment, so full inference through a production-grade APIM was proven with the simulator only | – | The first `check` on the target environment is the confirmation. Both tools print READY only after a successful call through their gateway. |
| R3 | Windows Server 2019/2022 specifics (Edge not preinstalled on 2019, Server Core without a browser) were not tested; the tests ran on Windows 11 x64 | – | The executables have no OS-specific dependencies beyond x64 Windows and the VC runtime bundled by PyInstaller. Edge must be present for the UI Analyst. |
| R4 | Data handling | Data owner | Code and screens go only to your own APIM/Azure OpenAI. Secrets and card numbers are masked before sending. Check APIM body logging (section 4). |

## 8. Suggested first-day test sequence on the application server

1. Download the release zip, run `Unblock-File` on it, extract it to `D:\modernisation`, then verify `SHA256SUMS.txt`.
2. Copy the deployed archive to `input\app.war`, then run `legacy-decompiler check` and `run` (about a minute for a mid-size WAR).
3. Fill in the `ai` block in `config\code-analysis.json`, set `$env:AZURE_OPENAI_API_KEY`, then run `code-analyst check` until it prints READY.
4. Run `code-analyst run --only <one small package>` to measure tokens and time, then the full run.
5. Fill in `config\ui-agent.json` (UAT URL, test roles, same `ai` block), then run `ui-analyst check` with `engine = "edge"`, then `run` with `max_steps_per_role` about 30 for a first pass.

## 9. Files changed

- `ui-analysis-agent\uiagent\config.py`: `api_style`, `api_version`, `auth_header`, `headers`; URL/header/provider builders; relative paths; loopback `http`.
- `ui-analysis-agent\uiagent\llm.py`: gateway URL and headers; APIM error parsing.
- `ui-analysis-agent\uiagent\copilot_runtime.py`: shared provider mapping; runtime discovery one folder up; tool-call preflight.
- `ui-analysis-agent\uiagent\cli.py`: endpoint diagnostics and hints.
- `ui-analysis-agent\uiagent\orchestrator.py`: new LLM signature; `app.start_paths`.
- `ui-analysis-agent\uiagent\agents\analyst.py`, `planner.py`, `report.py`: free module classification when `app.modules` is empty; neutral prompt examples; `business_module`.
- `ui-analysis-agent\uiagent\browser\iemode.py`: F7.
- `source-code-analysis-agent\code_analyst\common.py`: same `ai` settings and provider mapping.
- `source-code-analysis-agent\code_analyst\analysis.py`: provider; runtime discovery for the exe; tool-call preflight.
- `source-code-analysis-agent\code_analyst\cli.py`: diagnostics and hints.
- `code-decompiler\legacy_decompiler\decompile.py`, `common.py`, `cli.py`, `code_pillar.py`: Java 8/JRE handling, `jdk.heap`, fidelity kept when recompilation is skipped.
- New: `*\build_exe.ps1` (decompiler, code analyst; UI rewritten: folder build, Selenium submodules, staged build outside the source tree), `*\exe_entry.py`, `build-release.ps1`, `release-template\` (configs + operator README), and the built `release\`.
- `pyproject.toml` (both AI packages): SDK pinned.
- READMEs, PREREQUISITES and sample configs of all three packages, and the top-level README.
