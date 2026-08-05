$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectDir
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Error 'Git is required: https://git-scm.com/download/win'; exit 1 }

& git diff --quiet
$WorkingClean = $LASTEXITCODE -eq 0
& git diff --cached --quiet
$IndexClean = $LASTEXITCODE -eq 0
if (-not $WorkingClean -or -not $IndexClean) {
  Write-Error 'Local code changes are present. Commit or stash them before updating. Data and run outputs are not touched.'
  exit 2
}
& git pull --ff-only
if ($LASTEXITCODE -ne 0) { Write-Error 'git pull --ff-only failed; the installation was not changed.'; exit 1 }
& (Join-Path $ProjectDir 'scripts\install.ps1')
exit $LASTEXITCODE
