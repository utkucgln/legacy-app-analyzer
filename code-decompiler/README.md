# Legacy Decompiler

The Legacy Decompiler recovers everything that can be learned from a **deployed Java web application** (WAR, EAR or JAR) when the source code is missing or not trusted. It reconstructs the source with three independent decompilers and measures how faithful each reconstruction is. It makes no AI calls; it is deterministic and runs offline.

Its output is the input of the **Source Code Analysis Agents**. Its `actions.json` can also feed the **AI UI Analyst** access check.

Before installing, read [PREREQUISITES.md](PREREQUISITES.md).

## What it does

1. **Inventory.** Unpacks the artifact, including nested WARs. Lists every file with its SHA-256 hash. Identifies the application package prefix and the libraries, and flags end-of-life libraries with known CVEs.
2. **Descriptors and configuration.** Reads `web.xml`, `struts-config.xml`, `validation.xml`, Tiles, Hibernate (`hbm.xml`, `hibernate.cfg.xml`), Spring beans, Quartz jobs, JPA `persistence.xml` and `.properties` files. Configuration values are classified, and secrets are masked.
3. **Bytecode analysis.** Its own class-file parser, with no JVM needed, extracts:
   - SQL strings and integration strings (URLs, OFS/SWIFT/XML/JSON payloads);
   - constants and business literals, enums and special-case comparisons;
   - entry points: Struts actions, servlets, filters, listeners and jobs;
   - dead code and unmapped actions;
   - class versions and debug-info presence.
4. **Decompilation.** Runs Vineflower, CFR and Procyon on the application classes, with the libraries on the classpath.
5. **Fidelity measurement.** For each decompiler: coverage, error markers, independent recompilation of every class (JDK 8 at the original source level), and bytecode round-trip similarity against the original classes. It also measures cross-decompiler agreement per class, and recommends a **primary decompiler**. Classes where the decompilers disagree are flagged for review.
6. **Optional scanners.** `jdeps` package dependencies, a CycloneDX SBOM (`syft`), and a redacted secret scan (`gitleaks`).

## Install

```powershell
cd code-decompiler
.\install.ps1                    # or -IndexUrl <mirror> / -Wheelhouse D:\wheels (offline)
```

## Configure

Copy [examples/decompiler.sample.json](examples/decompiler.sample.json) and adjust it. Relative paths are resolved against the folder that contains the config file.

| Key | Default | Meaning |
|---|---|---|
| `artifact` | required | Path to the WAR, EAR or JAR copied from the application VM. |
| `output_dir` | `decompiler-out` | Output folder; it is overwritten on each run. Keep the path short. |
| `tools_dir` | `tools` | Decompiler jars, downloaded on first use or staged with `fetch-tools`. |
| `runner` | `local` | `local` = installed JDKs (recommended on Windows Server); `docker` = Linux containers. |
| `jdk.home` | `JAVA_HOME` / PATH | JDK 17 or 21 for the decompilers and `jdeps`. |
| `jdk.recompile_home` | none | JDK 8 for recompilation at the original Java level. |
| `jdk.timeout_s` | 7200 | Time limit for each decompiler run. |
| `app_package_prefix` | auto | For example `com/bank/banca`. Detected from the most common package if empty. |
| `decompilers` | all three | Any subset of `vineflower`, `cfr`, `procyon`. |
| `recompile` / `recompile_limit` | true / 0 | Recompilation fidelity check. `recompile_limit` > 0 samples that many classes for each decompiler. |
| `jdeps`, `sbom`, `gitleaks` | true | Optional steps. A missing executable skips the step and records the reason in `summary.json`. |
| `syft_path`, `gitleaks_path` | PATH | Paths to `syft.exe` and `gitleaks.exe`. |

## Run

```powershell
.\.venv\Scripts\legacy-decompiler.exe check       --config decompiler.json   # -> READY
.\.venv\Scripts\legacy-decompiler.exe fetch-tools --config decompiler.json   # optional: stage jars (connected machine)
.\.venv\Scripts\legacy-decompiler.exe run         --config decompiler.json
```

Indicative timing from the lab (114 classes, 26 libraries): about 7 seconds for the three decompilers and about 16 seconds for recompilation. Time grows roughly linearly with the number of classes.

## Outputs (`output_dir`)

| Path | Content |
|---|---|
| `decompiled/<tool>/` | Reconstructed Java sources from each decompiler. |
| `decompiled/fidelity-summary.json` | Per-decompiler coverage, error markers, recompilation rate and round-trip similarity, cross-tool agreement, and `recommended_primary`. |
| `decompiled/fidelity-*.csv` | Per-class agreement, error markers, recompilation results and round-trip similarity. |
| `unpacked/` | The unpacked artifact: descriptors, JSPs, configuration, libraries. |
| `inventory.csv`, `libraries.csv`, `class-versions.csv` | File inventory with hashes, libraries with end-of-life notes, Java class versions. |
| `webapp.json`, `actions.json`, `validation-rules.json`, `orm-map.json` | Servlets, filters, Struts actions and forwards, validation rules, ORM entities and named queries. |
| `config.json`, `config.csv`, `messages.json` | Configuration (secrets masked) and UI message bundles. |
| `entrypoints.csv`, `dead-code.csv`, `config-keys-not-read-by-literal.csv` | Entry points, unreferenced classes and unmapped actions, unused configuration. |
| `sql-strings.csv`, `integration-strings.csv`, `constants.csv`, `literals.csv`, `special-cases.csv`, `enums.json`, `methods.csv` | Bytecode extractions with class and method locations. |
| `jsp-inventory.json`, `templates.json` | JSP pages and message templates. |
| `deps.csv`, `jdeps.txt` | Package dependency graph. |
| `sbom.cdx.json`, `gitleaks.json` | SBOM and redacted secret-scan findings (optional steps). |
| `facts.json` | Evidence facts (findings, rules, integrations, jobs and more) with source locations. |
| `summary.json`, `run-manifest.json` | Run summary, timings and any step errors. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `java not found` / `jdeps not found` | Set `jdk.home` to a JDK (not a JRE) folder. |
| `syft not found` / `gitleaks not found` in `summary.json` | Optional steps: install the tools and set `syft_path` / `gitleaks_path`, or set `sbom` / `gitleaks` to `false`. |
| Low recompilation rate for one decompiler | Expected for some tools. Use `recommended_primary`; the analysis agents cross-check the flagged classes against the alternative decompilations. |
| Paths too long / file not found deep in `decompiled\` | Enable long paths, or shorten `output_dir`. |
| Download of tool jars fails | Stage `tools_dir` with `fetch-tools` on a connected machine and copy it across. |
