$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectDir

function Fail([string]$Message) { Write-Error $Message; exit 1 }
# Git belongs to the update path, not to installing a release bundle: a
# downloaded ZIP has no repository and does not need one.  It is required
# only where this really is a checkout that `update.ps1` will pull into.
if ((Test-Path -LiteralPath (Join-Path $ProjectDir '.git')) -and
    -not (Get-Command git -ErrorAction SilentlyContinue)) {
  Fail 'This is a Git checkout, so Git is required to update it: https://git-scm.com/download/win'
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail 'Node.js 20.19+ is required: https://nodejs.org/en/download' }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail 'npm normally ships with Node.js: https://nodejs.org/en/download' }

# The launcher being present does not mean 3.13 is installed under it, so the
# specific interpreter is probed rather than assumed.  Reporting "requires
# 3.13" when `py` exists but `py -3.13` does not leaves the reader with no idea
# what to do next.
$PythonCommand = $null
$PythonPrefix = @()
$PyHasThirteen = $false
if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3.13 -c "pass" 2>$null
  $PyHasThirteen = ($LASTEXITCODE -eq 0)
  if ($PyHasThirteen) { $PythonCommand = 'py'; $PythonPrefix = @('-3.13') }
}
if (-not $PythonCommand -and (Get-Command python -ErrorAction SilentlyContinue)) { $PythonCommand = 'python' }
if (-not $PythonCommand) {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    Fail 'The py launcher is installed, but no Python 3.13 is registered with the py launcher. Install Python 3.13 from https://www.python.org/downloads/windows/ with the launcher option ticked, then confirm with: py -0p'
  }
  Fail 'Python 3.13 is required: https://www.python.org/downloads/windows/'
}

& $PythonCommand @PythonPrefix -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) else 1)"
if ($LASTEXITCODE -ne 0) {
  Fail "This package requires Python 3.13.x, but '$PythonCommand $PythonPrefix' is a different version. Install Python 3.13 from https://www.python.org/downloads/windows/ and run this script again."
}
& node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20 || (a===20 && b>=19) ? 0 : 1)"
if ($LASTEXITCODE -ne 0) { Fail 'Node.js 20.19 or newer is required: https://nodejs.org/en/download' }

$VenvDir = Join-Path $ProjectDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
  # An environment left behind by an older Python is reused silently otherwise,
  # and every later step then runs on the wrong interpreter while reporting
  # success.  It is moved aside rather than deleted: it may hold work.
  & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) else 1)" 2>$null
  if ($LASTEXITCODE -ne 0) {
    $Backup = "$VenvDir.backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Write-Host "Existing .venv is not Python 3.13; moving it to $Backup"
    Move-Item -LiteralPath $VenvDir -Destination $Backup
  }
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
  & $PythonCommand @PythonPrefix -m venv $VenvDir
  if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Python virtual environment.' }
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail 'pip upgrade failed.' }
& $VenvPython -m pip install -r (Join-Path $ProjectDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'Python dependency installation failed.' }
& npm --prefix (Join-Path $ProjectDir 'frontend') ci
if ($LASTEXITCODE -ne 0) { Fail 'Frontend dependency installation failed.' }
& npm --prefix (Join-Path $ProjectDir 'frontend') run build
if ($LASTEXITCODE -ne 0) { Fail 'Frontend build failed.' }
& $VenvPython (Join-Path $ProjectDir 'src\verify_toolchain.py')
if ($LASTEXITCODE -ne 0) { Fail 'OpenStudio 3.11.0 with EnergyPlus 25.2.0 is required: https://github.com/NREL/OpenStudio/releases/tag/v3.11.0. You may also set VALENCIA_EPLUS_DIR.' }
& $VenvPython (Join-Path $ProjectDir 'scripts\verify_distribution.py') --require
if ($LASTEXITCODE -ne 0) { Fail 'The release data payload is incomplete. Use the full Building Stock Energy Workbench release bundle or add the four inputs in Files.' }

Write-Host "`nInstallation complete. Start with: .\scripts\start.ps1"
