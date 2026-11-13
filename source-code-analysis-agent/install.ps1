# Creates .venv in this folder, installs the Source Code Analysis Agents and downloads the GitHub Copilot runtime.
#   .\install.ps1                                   # Python 3.12 via the py launcher, packages from PyPI
#   .\install.ps1 -IndexUrl https://<mirror>/simple # internal PyPI mirror
#   .\install.ps1 -Wheelhouse D:\wheels -SkipRuntimeDownload   # offline; then set analysis.cli_path to a staged runtime
param([string]$Python, [string]$IndexUrl, [string]$Wheelhouse, [switch]$SkipRuntimeDownload)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($Python) { & $Python -m venv .venv } else { & py -3.12 -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw 'could not create the virtual environment (is Python 3.12 x64 installed?)' }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$source = if ($Wheelhouse) { @('--no-index', '--find-links', $Wheelhouse) } elseif ($IndexUrl) { @('--index-url', $IndexUrl) } else { @() }
& $py -m pip install @source '.'
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
if (-not $SkipRuntimeDownload) {
    & $py -m copilot download-runtime
    if ($LASTEXITCODE -ne 0) { throw 'Copilot runtime download failed (needs HTTPS to GitHub; or stage it and set analysis.cli_path)' }
    Get-ChildItem "$env:LOCALAPPDATA\github-copilot-sdk\cli\*\prebuilds\win32-x64\copilot-runtime.exe" | Select-Object -ExpandProperty FullName
}
"installed. Next: .\.venv\Scripts\code-analyst.exe check --config <your-config>.json"
