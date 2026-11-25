# Builds dist\code-analyst\code-analyst.exe: a folder build (no Python needed on the target, nothing unpacked to %TEMP%), with the
# GitHub Copilot runtime pinned by the SDK staged in dist\code-analyst\copilot-runtime\<version> (found automatically by the exe).
#   .\build_exe.ps1                  # exe + runtime (needs HTTPS to PyPI and GitHub on the build machine)
#   .\build_exe.ps1 -SkipRuntime     # exe only; set analysis.cli_path on the target
param([string]$Python, [switch]$SkipRuntime)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .venv-build)) { if ($Python) { & $Python -m venv .venv-build } else { & py -3.12 -m venv .venv-build } }
if ($LASTEXITCODE) { throw 'could not create the build virtual environment (is Python 3.12 x64 installed?)' }
$py = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
# Build from a copy in %TEMP%: setuptools/PyInstaller work folders inside a synced folder (OneDrive) or a reused build\ folder
# make wheel builds fail intermittently (WinError 183). Only dist\ is written back here.
$src = Join-Path $env:TEMP 'build-code-analyst-src'
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
robocopy $PSScriptRoot $src /E /XD .venv .venv-build dist build code_analyst.egg-info __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw 'could not stage the sources' }
& $py -m pip install -q --upgrade $src 'pyinstaller>=6.0'
if ($LASTEXITCODE) { throw 'pip install failed' }
& $py -m PyInstaller --noconfirm --clean --onedir --console --name code-analyst `
    --collect-submodules code_analyst --collect-all copilot `
    --distpath dist --workpath "$src\pyinstaller" --specpath "$src\pyinstaller" "$src\exe_entry.py"
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue
if (-not $SkipRuntime) {
    & $py -m copilot download-runtime
    if ($LASTEXITCODE) { throw 'Copilot runtime download failed (needs HTTPS to GitHub)' }
    $info = & $py -c "from copilot._cli_download import get_cache_dir, get_runtime_platform; from copilot._cli_version import CLI_VERSION; print(CLI_VERSION); print(get_cache_dir(CLI_VERSION) / 'prebuilds' / get_runtime_platform())"
    $version, $source = $info[0], $info[1]
    if (-not (Test-Path (Join-Path $source 'copilot-runtime.exe'))) { throw "copilot-runtime.exe not found in $source" }
    $target = "dist\code-analyst\copilot-runtime\$version"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    Copy-Item $source $target -Recurse
    "staged Copilot runtime $version in $target"
}
Get-Item dist\code-analyst\code-analyst.exe | Select-Object FullName
