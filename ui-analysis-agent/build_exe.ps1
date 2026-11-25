# Builds dist\ui-analyst\ui-analyst.exe: a folder build (no Python needed on the target, nothing unpacked to %TEMP%).
#   .\build_exe.ps1 [-WithEdgeEngine] [-WithCopilot] [-SkipIEDriver]
#   -WithEdgeEngine  include Playwright for browser.engine = "edge" (uses the installed Edge, no browser download)
#   -WithCopilot     include the GitHub Copilot SDK and stage its pinned runtime in dist\ui-analyst\copilot-runtime\<version>
#   IEDriverServer (Win32) is staged in dist\ui-analyst\drivers unless -SkipIEDriver; set browser.iedriver_path to it.
# Run on a connected build machine with Python 3.12 x64, then copy dist\ui-analyst to the target VM.
param([string]$Python, [switch]$WithEdgeEngine, [switch]$WithCopilot, [switch]$SkipIEDriver)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .venv-build)) { if ($Python) { & $Python -m venv .venv-build } else { & py -3.12 -m venv .venv-build } }
if ($LASTEXITCODE) { throw 'could not create the build virtual environment (is Python 3.12 x64 installed?)' }
$py = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
# Build from a copy in %TEMP%: setuptools/PyInstaller work folders inside a synced folder (OneDrive) or a reused build\ folder
# make wheel builds fail intermittently (WinError 183). Only dist\ is written back here.
$src = Join-Path $env:TEMP 'build-ui-analyst-src'
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
robocopy $PSScriptRoot $src /E /XD .venv .venv-build dist build ai_ui_analyst.egg-info __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw 'could not stage the sources' }
$extras = @('build') + $(if ($WithEdgeEngine) { 'edge' }) + $(if ($WithCopilot) { 'copilot' })
& $py -m pip install -q --upgrade "$src[$($extras -join ',')]"
if ($LASTEXITCODE) { throw 'pip install failed' }
$piArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console', '--name', 'ui-analyst',
            '--collect-submodules', 'uiagent', '--collect-submodules', 'selenium', '--collect-data', 'selenium',
            '--distpath', 'dist', '--workpath', "$src\pyinstaller", '--specpath', "$src\pyinstaller")
if ($WithEdgeEngine) { $piArgs += @('--collect-all', 'playwright') } else { $piArgs += @('--exclude-module', 'playwright') }
if ($WithCopilot) { $piArgs += @('--collect-all', 'copilot') } else { $piArgs += @('--exclude-module', 'copilot') }
& $py @piArgs "$src\ui_analyst.py"
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue
if ($WithCopilot) {
    & $py -m copilot download-runtime
    if ($LASTEXITCODE) { throw 'Copilot runtime download failed (needs HTTPS to GitHub)' }
    $info = & $py -c "from copilot._cli_download import get_cache_dir, get_runtime_platform; from copilot._cli_version import CLI_VERSION; print(CLI_VERSION); print(get_cache_dir(CLI_VERSION) / 'prebuilds' / get_runtime_platform())"
    $version, $source = $info[0], $info[1]
    if (-not (Test-Path (Join-Path $source 'copilot-runtime.exe'))) { throw "copilot-runtime.exe not found in $source" }
    $target = "dist\ui-analyst\copilot-runtime\$version"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    Copy-Item $source $target -Recurse
    "staged Copilot runtime $version in $target"
}
if (-not $SkipIEDriver) {
    # Selenium Manager resolves the IEDriverServer release that matches the bundled Selenium (Win32 build).
    $sm = Get-ChildItem (Join-Path $PSScriptRoot '.venv-build\Lib\site-packages\selenium') -Recurse -Filter selenium-manager.exe | Select-Object -First 1
    $res = (& $sm.FullName --browser iexplorer --output json | ConvertFrom-Json).result
    if (-not $res.driver_path -or -not (Test-Path $res.driver_path)) { throw 'IEDriverServer download failed (needs HTTPS to GitHub); use -SkipIEDriver and stage it manually' }
    New-Item -ItemType Directory -Force dist\ui-analyst\drivers | Out-Null
    Copy-Item $res.driver_path dist\ui-analyst\drivers\IEDriverServer.exe -Force
    "staged IEDriverServer from $($res.driver_path)"
}
Get-Item dist\ui-analyst\ui-analyst.exe | Select-Object FullName
