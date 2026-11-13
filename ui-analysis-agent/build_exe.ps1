# Builds dist\ui-analyst.exe (single file). Run from the ui-agent folder:  .\build_exe.ps1 [-WithEdgeEngine] [-WithCopilot]
# -WithCopilot also stages the GitHub Copilot runtime in dist\copilot-runtime so the target machine needs no download.
param([switch]$WithEdgeEngine, [switch]$WithCopilot)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$extras = @('build') + $(if ($WithEdgeEngine) { 'edge' }) + $(if ($WithCopilot) { 'copilot' })
& $py -m pip install -q -e ".[$($extras -join ',')]"
$piArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--name', 'ui-analyst',
          '--collect-submodules', 'uiagent', '--collect-data', 'selenium')
if ($WithEdgeEngine) { $piArgs += @('--collect-all', 'playwright') } else { $piArgs += @('--exclude-module', 'playwright') }
if ($WithCopilot) { $piArgs += @('--collect-all', 'copilot') } else { $piArgs += @('--exclude-module', 'copilot') }
$piArgs += 'ui_analyst.py'
& $py @piArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
if ($WithCopilot) {
    & $py -m copilot download-runtime
    if ($LASTEXITCODE -ne 0) { throw 'copilot runtime download failed' }
    $exe = Get-ChildItem "$env:LOCALAPPDATA\github-copilot-sdk\cli\*\prebuilds\win32-x64\copilot-runtime.exe" |
        Sort-Object { [version]($_.FullName -replace '.*\\cli\\([\d.]+)\\.*', '$1') } | Select-Object -Last 1
    if (-not $exe) { throw 'copilot-runtime.exe not found after download' }
    $version = $exe.FullName -replace '.*\\cli\\([\d.]+)\\.*', '$1'
    $target = "dist\copilot-runtime\$version"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Copy-Item $exe.Directory.FullName $target -Recurse
    "staged Copilot runtime $version in $target"
}
Get-Item dist\ui-analyst.exe | Select-Object FullName, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
