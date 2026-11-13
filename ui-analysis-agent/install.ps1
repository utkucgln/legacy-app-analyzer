# Creates .venv in this folder and installs the AI UI Analyst.
#   .\install.ps1                                    # IE mode engine only (Selenium), packages from PyPI
#   .\install.ps1 -WithEdgeEngine -WithCopilot       # add the Playwright Edge engine and the GitHub Copilot SDK runtime
#   .\install.ps1 -IndexUrl https://<mirror>/simple  # internal PyPI mirror
#   .\install.ps1 -Wheelhouse D:\wheels              # offline: wheels prepared with `pip download ".[edge,copilot]" -d D:\wheels`
# For a single ui-analyst.exe (no Python on the target VM) use .\build_exe.ps1 on the development VM instead.
param([string]$Python, [string]$IndexUrl, [string]$Wheelhouse, [switch]$WithEdgeEngine, [switch]$WithCopilot)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($Python) { & $Python -m venv .venv } else { & py -3.12 -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw 'could not create the virtual environment (is Python 3.12 x64 installed?)' }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$source = if ($Wheelhouse) { @('--no-index', '--find-links', $Wheelhouse) } elseif ($IndexUrl) { @('--index-url', $IndexUrl) } else { @() }
$extras = @() + $(if ($WithEdgeEngine) { 'edge' }) + $(if ($WithCopilot) { 'copilot' })
$target = if ($extras) { ".[$($extras -join ',')]" } else { '.' }
& $py -m pip install @source $target
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
if ($WithCopilot -and -not $Wheelhouse) {
    & $py -m copilot download-runtime
    if ($LASTEXITCODE -ne 0) { throw 'Copilot runtime download failed (needs HTTPS to GitHub; or stage it and set ai.copilot.cli_path)' }
}
"installed. Next: .\.venv\Scripts\ui-analyst.exe check --config <your-config>.json"
