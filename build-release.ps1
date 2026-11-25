# Builds the offline release for the application server: three folder-build executables (no Python on the target), one shared
# GitHub Copilot runtime, IEDriverServer, the decompiler jars, configuration templates and SHA-256 checksums.
#   .\build-release.ps1                      # -> .\release
#   .\build-release.ps1 -Python C:\Python312\python.exe -Out D:\drop\modernisation-release
# Needs, on the build machine only: Python 3.12 x64 and HTTPS to PyPI, Maven Central and GitHub.
param([string]$Python, [string]$Out = (Join-Path $PSScriptRoot 'release'))
$ErrorActionPreference = 'Stop'
$common = if ($Python) { @{ Python = $Python } } else { @{} }

& (Join-Path $PSScriptRoot 'code-decompiler\build_exe.ps1') @common
& (Join-Path $PSScriptRoot 'source-code-analysis-agent\build_exe.ps1') @common
& (Join-Path $PSScriptRoot 'ui-analysis-agent\build_exe.ps1') @common -WithEdgeEngine -WithCopilot

if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force $Out, "$Out\config", "$Out\input", "$Out\work" | Out-Null
$dists = @{ 'legacy-decompiler' = 'code-decompiler'; 'code-analyst' = 'source-code-analysis-agent'; 'ui-analyst' = 'ui-analysis-agent' }
foreach ($name in $dists.Keys) {
    $src = Join-Path $PSScriptRoot "$($dists[$name])\dist\$name"
    robocopy $src "$Out\$name" /E /NFL /NDL /NJH /NJS /NP /XD copilot-runtime | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copy of $src failed" }
}
# One shared runtime for both AI tools (each exe also looks one folder up).
$rt = Get-ChildItem (Join-Path $PSScriptRoot 'source-code-analysis-agent\dist\code-analyst\copilot-runtime') -Directory | Select-Object -First 1
robocopy $rt.FullName "$Out\copilot-runtime\$($rt.Name)" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw 'copy of the Copilot runtime failed' }
Copy-Item (Join-Path $PSScriptRoot 'release-template\*') $Out -Recurse -Force

$hashes = Get-ChildItem $Out -Recurse -File | Where-Object Name -ne 'SHA256SUMS.txt' | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.FullName.Substring($Out.Length + 1)
}
$hashes | Set-Content "$Out\SHA256SUMS.txt" -Encoding ascii
$size = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
"release ready: $Out ($([math]::Round($size)) MB, $($hashes.Count) files). Copy the whole folder to the application server."
