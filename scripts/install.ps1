$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $ProjectDir

function Fail([string]$Message) { Write-Error $Message; exit 1 }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'Git is required: https://git-scm.com/download/win' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail 'Node.js 20.19+ is required: https://nodejs.org/en/download' }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail 'npm normally ships with Node.js: https://nodejs.org/en/download' }

$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $PythonCommand = 'py'; $PythonPrefix = @('-3.13') }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PythonCommand = 'python' }
else { Fail 'Python 3.13 is required: https://www.python.org/downloads/windows/' }

& $PythonCommand @PythonPrefix -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) else 1)"
if ($LASTEXITCODE -ne 0) { Fail 'This package requires Python 3.13.x.' }
& node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20 || (a===20 && b>=19) ? 0 : 1)"
if ($LASTEXITCODE -ne 0) { Fail 'Node.js 20.19 or newer is required: https://nodejs.org/en/download' }

$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
  & $PythonCommand @PythonPrefix -m venv (Join-Path $ProjectDir '.venv')
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
if ($LASTEXITCODE -ne 0) { Fail 'The release data payload is incomplete. Use the full Valencia Workbench release bundle or add the four inputs in Files.' }

Write-Host "`nInstallation complete. Start with: .\scripts\start.ps1"
