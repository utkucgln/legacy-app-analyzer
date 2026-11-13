# Creates .venv in this folder and installs the Legacy Decompiler into it.
#   .\install.ps1                                   # Python 3.12 via the py launcher, packages from PyPI
#   .\install.ps1 -IndexUrl https://<mirror>/simple # internal PyPI mirror
#   .\install.ps1 -Wheelhouse D:\wheels             # offline: wheels prepared with `pip download . -d D:\wheels`
param([string]$Python, [string]$IndexUrl, [string]$Wheelhouse)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($Python) { & $Python -m venv .venv } else { & py -3.12 -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw 'could not create the virtual environment (is Python 3.12 x64 installed?)' }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$source = if ($Wheelhouse) { @('--no-index', '--find-links', $Wheelhouse) } elseif ($IndexUrl) { @('--index-url', $IndexUrl) } else { @() }
& $py -m pip install @source '.'
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
"installed. Next: .\.venv\Scripts\legacy-decompiler.exe check --config <your-config>.json"
