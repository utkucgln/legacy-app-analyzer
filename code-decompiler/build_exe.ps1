# Builds dist\legacy-decompiler\legacy-decompiler.exe: a folder build (no Python needed on the target, nothing unpacked to %TEMP%).
#   .\build_exe.ps1                 # also stages the decompiler jars in dist\legacy-decompiler\tools (needs Maven Central / GitHub)
#   .\build_exe.ps1 -SkipTools      # exe only
# Run on a connected build machine with Python 3.12 x64; copy dist\legacy-decompiler to the target. Java is still needed there.
param([string]$Python, [switch]$SkipTools)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path .venv-build)) { if ($Python) { & $Python -m venv .venv-build } else { & py -3.12 -m venv .venv-build } }
if ($LASTEXITCODE) { throw 'could not create the build virtual environment (is Python 3.12 x64 installed?)' }
$py = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
# Build from a copy in %TEMP%: setuptools/PyInstaller work folders inside a synced folder (OneDrive) or a reused build\ folder
# make wheel builds fail intermittently (WinError 183). Only dist\ is written back here.
$src = Join-Path $env:TEMP 'build-legacy-decompiler-src'
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
robocopy $PSScriptRoot $src /E /XD .venv .venv-build dist build legacy_decompiler.egg-info __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw 'could not stage the sources' }
& $py -m pip install -q --upgrade $src 'pyinstaller>=6.0'
if ($LASTEXITCODE) { throw 'pip install failed' }
& $py -m PyInstaller --noconfirm --clean --onedir --console --name legacy-decompiler `
    --collect-submodules legacy_decompiler --collect-data legacy_decompiler `
    --distpath dist --workpath "$src\pyinstaller" --specpath "$src\pyinstaller" "$src\exe_entry.py"
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
Remove-Item $src -Recurse -Force -ErrorAction SilentlyContinue
if (-not $SkipTools) {
    $cfg = Join-Path $env:TEMP 'legacy-decompiler-fetch.json'
    @{ artifact = $cfg; tools_dir = (Join-Path $PSScriptRoot 'dist\legacy-decompiler\tools') } | ConvertTo-Json | Set-Content $cfg -Encoding utf8
    & .\dist\legacy-decompiler\legacy-decompiler.exe fetch-tools --config $cfg
    if ($LASTEXITCODE) { throw 'fetch-tools failed' }
    Remove-Item $cfg
}
Get-Item dist\legacy-decompiler\legacy-decompiler.exe | Select-Object FullName
