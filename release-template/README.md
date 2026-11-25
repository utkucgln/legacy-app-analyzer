# Modernisation toolkit: run on the application server

This folder runs on the application server (Windows x64) **without Python, pip or any installation**. It is the content of the release zip (`legacy-app-analyzer-release-<date>.zip`). Before extracting a downloaded zip, run `Unblock-File <zip>` (or tick *Unblock* in the zip's file properties). Otherwise Windows marks every extracted file as downloaded from the internet, and SmartScreen or application control may block the executables. Extract it, for example to `D:\modernisation` (rename the extracted `legacy-app-analyzer-release` folder), and keep the folder structure.

| Folder | Content |
|---|---|
| `legacy-decompiler\` | `legacy-decompiler.exe` and the decompiler jars (`tools\`). Needs a Java runtime on the server (see step 2). |
| `code-analyst\` | `code-analyst.exe`: Source Code Analysis Agents. |
| `ui-analyst\` | `ui-analyst.exe`: AI UI Analyst, with `drivers\IEDriverServer.exe` (Edge IE mode) and the Playwright engine (standard Edge). |
| `copilot-runtime\` | GitHub Copilot SDK runtime, shared by both AI tools and found automatically. |
| `config\` | Configuration templates. Relative paths are resolved against this folder. |
| `input\` | Put the copy of the deployed WAR/EAR here (the templates expect `app.war`; change `artifact` for another name). |
| `work\` | Outputs of all three tools. |
| `SHA256SUMS.txt` | SHA-256 of every file, for integrity checks after the transfer. |

**What the toolkit analyses:** Java web applications (WAR, or EAR containing WARs) built on servlets/JSP. The deepest extraction is for Struts 1, Spring XML configuration, Hibernate / JPA, Quartz and `web.xml`; other Java frameworks are decompiled and analysed by the AI agents, with fewer deterministic extractions. The UI Analyst works with any browser-based application (form sign-in or SSO).

Run the tools **one at a time** on a small VM. Run them in a normal console (PowerShell or cmd) of a signed-in user. No administrator rights are needed, except for the optional Edge IE mode settings in step 5.

## 1. Verify the copy

```powershell
cd D:\modernisation
Get-Content SHA256SUMS.txt | ForEach-Object { $h, $f = $_ -split '  ', 2; if ((Get-FileHash $f -Algorithm SHA256).Hash -ne $h) { "MISMATCH $f" } }
```

If AppLocker or WDAC is enforced, allow the three `.exe` files and `copilot-runtime\*\copilot-runtime.exe`, which is in the same folder as `runtime.node`. If antivirus scanning is aggressive, exclude `D:\modernisation\work`.

## 2. Legacy Decompiler (no AI, offline)

1. Copy the deployed archive to `input\app.war`, then record its hash with `Get-FileHash input\app.war -Algorithm SHA256`. For an EAR, or if business classes live in shared server libraries, see the *Application VM* section of `code-decompiler/PREREQUISITES.md` in the repository.
2. Java. The decompiler uses `jdk.home`, then `JAVA_HOME`, then `java` on the PATH.
   - **Best:** a JDK 17 or 21. A portable ZIP (Microsoft Build of OpenJDK or Eclipse Temurin) needs no installation and no administrator rights: unzip it and set `jdk.home` to its folder.
   - **The server's own Java 8 JDK** works too. CFR and Procyon run, and Vineflower is skipped.
   - **A JRE** (no `javac`, no `jdeps`) still decompiles. The recompilation fidelity check and the `jdeps` step are skipped, and the reasons are written to `summary.json`.
   - Optionally set `jdk.recompile_home` to a JDK 8 for recompilation at the original Java level.
3. Run:

```powershell
.\legacy-decompiler\legacy-decompiler.exe check --config config\decompiler.json    # -> READY (WARN lines are informational)
.\legacy-decompiler\legacy-decompiler.exe run   --config config\decompiler.json
```

`jdk.heap` (default `1g` in this template) is the Java heap of each decompiler. The decompilers run one after the other. If the server is short of memory, keep `1g` and run outside business hours. On a 114-class test application, the run took 10 to 60 s depending on the Java available.

## 3. Azure OpenAI through API Management

Both AI tools read the same `ai` block. Ask the APIM team for:

| Value | Config key | Example |
|---|---|---|
| Gateway URL + API URL suffix | `ai.endpoint` | `https://apim-contoso.azure-api.net/aoai` (a trailing `/openai` is accepted) |
| Deployment (model) name | `ai.model` | `gpt-4.1` |
| URL style of the API | `ai.api_style` | `deployments` for `/openai/deployments/<name>/chat/completions?api-version=…` (classic Azure OpenAI import, the usual case). `v1` for `/openai/v1/…`. `base_url` if the endpoint already is the full OpenAI-compatible base URL. |
| API version | `ai.api_version` | `2025-04-01-preview` (style `deployments` only) |
| Subscription key header | `ai.auth_header` | `api-key` (typical for Azure OpenAI imports) or `Ocp-Apim-Subscription-Key` (APIM default). Use `Authorization` if the API accepts the key as a bearer token. |
| Extra non-secret headers | `ai.headers` | `{}` |
| Operations the API must expose | | `POST deployments/{id}/chat/completions` (UI Analyst, direct runtime) and `POST responses` (Code Analyst, and the UI Analyst `copilot` runtime) |

The **APIM subscription key** goes only into an environment variable of the console session, never into a file:

```powershell
$env:AZURE_OPENAI_API_KEY = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'APIM subscription key')).Password
```

The tools send this key only in the configured header. They never send a Windows or Azure CLI identity token to the gateway.

The `check` command prints the exact URL and header name it uses, and makes a real call. For the Copilot runtime, that call includes a tool call, because the agents depend on function tools. On failure it prints the most likely fix:

| Result | Fix |
|---|---|
| HTTP 401 `Access denied due to missing/invalid subscription key` | `ai.auth_header` does not match the API's header name, or wrong key. |
| HTTP 404 `Resource not found` | Wrong `ai.endpoint` path or `ai.api_style`, or the API has no such operation. |
| HTTP 404 `DeploymentNotFound` | APIM accepted the key; the deployment name is wrong. |
| 404 on `/openai/responses` | The APIM API has no Responses operation: ask the APIM team to add `POST /responses` (or re-import the API from a recent Azure OpenAI specification). See the note below. |
| `Function tools with reasoning_effort are not supported … /chat/completions` | Reasoning models need the Responses API for tool use: keep `wire_api` = `responses`. |
| Certificate error from the Copilot runtime | It is Node.js-based and does not read the Windows certificate store. Export the gateway's root CA as PEM and set `$env:NODE_EXTRA_CA_CERTS = 'D:\certs\corp-root.pem'`. |
| Timeout | Firewall or proxy. Set `$env:HTTPS_PROXY` if outbound traffic goes through a proxy. |

Note on `wire_api`: the agents need function tools. With reasoning models (gpt-5 family and later, o-series), Azure OpenAI supports tools only through the **Responses API**, so `wire_api` must be `responses`, and APIM must route `POST {suffix}/openai/responses?api-version=…`. `completions` works only with non-reasoning deployments such as gpt-4.1 or gpt-4o.

## 4. Source Code Analysis Agents

Edit the `ai` block of `config\code-analysis.json`, then:

```powershell
.\code-analyst\code-analyst.exe check --config config\code-analysis.json                     # -> READY
.\code-analyst\code-analyst.exe run   --config config\code-analysis.json --prepare-only      # workspace only, no model calls
.\code-analyst\code-analyst.exe run   --config config\code-analysis.json --only com.example.orders   # one package first (smaller TPM load)
.\code-analyst\code-analyst.exe run   --config config\code-analysis.json                     # everything (resumable)
```

`--prepare-only` writes `work\code-analysis-out\workspace.json`; its `packages` entry lists the Java packages. Use one of them with `--only`. Set `analysis.target_stack` to your modernisation target if it differs from the default (Java 21 / Spring Boot 3 / React on Azure).

Budget for roughly 80K input tokens per class. Interrupted runs resume where they stopped. The report is `work\code-analysis-out\modernization-report.html`.

## 5. AI UI Analyst

Edit `app`, `roles` and the `ai` block of `config\ui-agent.json`:

| Key | What to put |
|---|---|
| `app.name`, `app.description` | The application's name and one sentence on its purpose; this gives the agents context. |
| `app.base_url`, `app.login_url` | UAT URL of the application and of its sign-in page. |
| `app.scope_prefixes` | URL path prefixes the analyst may visit, for example `["/myapp/"]`. Anything else is never opened. |
| `app.modules` | Optional functional areas, for example `["Customers", "Orders", "Administration"]`. Leave `[]` to let the analyst name them. |
| `app.sample_values` | Optional valid search values per field, so screens behind a search can be reached: `{"customer id": ["12345"]}`. |
| `app.start_paths` | Optional extra paths relative to `base_url`, for example screens not linked from any menu. |
| `roles` | One entry per role: test user name and the environment variable that holds its password. Add or remove roles freely. |
| `login.*_selector`, `login.success_text` | Only if `check` cannot find the sign-in form: CSS selectors of the user name, password and submit elements, and a text shown only after sign-in. `login.mode = "sso"` for Windows/SSO sign-in. |
| `limits.max_steps_per_role` | Start with about 30 for a first pass, then raise it. |

Set each role's password in the console session, for example `$env:UIA_PWD_OPERATOR = …`.

- **`browser.engine = "edge"`** (template default: standard Edge, no driver, no registry changes) works if the application renders in modern Edge.
- **`browser.engine = "ie"`** (Edge IE mode, for pages that only work in Internet Explorer) needs one-time machine settings that require an administrator: the IE mode policy, the `FEATURE_BFCACHE` registry key, IE ESC off for the account, and consistent Protected Mode. See `ui-analysis-agent/PREREQUISITES.md` in the repository. `drivers\IEDriverServer.exe` is already configured.
- Run in an interactive desktop session (console or a non-minimised RDP window).

```powershell
.\ui-analyst\ui-analyst.exe check  --config config\ui-agent.json   # model + browser + login for every role -> READY
.\ui-analyst\ui-analyst.exe run    --config config\ui-agent.json
.\ui-analyst\ui-analyst.exe design --config config\ui-agent.json
```

The reports are `work\ui-analysis-out\report.html` and `work\ui-analysis-out\design\design-report.html`.

The analyst works in read-only mode: it never clicks save, submit, approve, delete or similar actions, and lists them in `blocked-actions.csv` instead. Use test accounts without MFA or CAPTCHA, and point it at a UAT instance, not at production, even when it runs on the same server.

## 6. Help

Each tool's `check` prints the most likely fix for any failure. The full documentation for each tool (all configuration keys, outputs and troubleshooting) is in the README and PREREQUISITES of its folder in the repository: `code-decompiler/`, `source-code-analysis-agent/`, `ui-analysis-agent/`.
