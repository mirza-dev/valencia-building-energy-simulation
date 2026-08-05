$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { Write-Error 'Run .\scripts\install.ps1 first.'; exit 1 }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'frontend\dist\index.html'))) { Write-Error 'Frontend is not built; run .\scripts\install.ps1 first.'; exit 1 }

$Port = if ($env:WORKBENCH_PORT) { $env:WORKBENCH_PORT } else { '8765' }
$StateDir = if ($env:VALENCIA_STATE_DIR) { $env:VALENCIA_STATE_DIR } else { Join-Path $ProjectDir 'var' }
$RunDir = if ($env:VALENCIA_RUN_DIR) { $env:VALENCIA_RUN_DIR } else { Join-Path $ProjectDir 'out\ui_runs' }
$TempDir = Join-Path $StateDir 'tmp'
New-Item -ItemType Directory -Force -Path $StateDir, $RunDir, $TempDir | Out-Null

$env:PYTHONPATH = Join-Path $ProjectDir 'src'
$env:WORKBENCH_ENV = 'production'
$env:WORKBENCH_PORT = $Port
$env:WORKBENCH_VAR_DIR = $StateDir
$env:WORKBENCH_DB_PATH = Join-Path $StateDir 'workbench.sqlite3'
$env:WORKBENCH_PREVIEW_ROOT = Join-Path $StateDir 'previews'
$env:WORKBENCH_IMPORT_ROOT = Join-Path $StateDir 'imports'
$env:WORKBENCH_RUN_ROOT = $RunDir
$env:WORKBENCH_EXPORT_ROOT = Join-Path $StateDir 'exports'
$env:MPLCONFIGDIR = Join-Path $StateDir 'matplotlib'
$env:TMPDIR = $TempDir
$env:TMP = $TempDir
$env:TEMP = $TempDir
foreach ($Name in @('WORKBENCH_TEST_ROOT', 'WORKBENCH_TEST_RUN_ID', 'WORKBENCH_TEST_REQUIRE_HEADER')) {
  Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
}

$Url = "http://127.0.0.1:$Port/#/files"
$BrowserJob = $null
if ($env:VALENCIA_NO_BROWSER -ne '1') {
  $BrowserJob = Start-Job -ScriptBlock { param($Target); Start-Sleep -Seconds 2; Start-Process $Target } -ArgumentList $Url
}
Write-Host "Valencia Workbench: $Url"
Write-Host 'Press Ctrl+C to stop.'
Set-Location -LiteralPath $ProjectDir
try { & $Python -m workbench; exit $LASTEXITCODE }
finally {
  if ($BrowserJob) {
    Stop-Job -Job $BrowserJob -ErrorAction SilentlyContinue
    Remove-Job -Job $BrowserJob -ErrorAction SilentlyContinue
  }
}
