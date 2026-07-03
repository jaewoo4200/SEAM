<#
.SYNOPSIS
  SionnaTwin Studio one-command installer (Windows / PowerShell).

  Idempotent: re-running is safe. Creates backend/.venv if missing, installs the
  backend (editable, with dev extras), installs the frontend, regenerates the
  demo projects, and prints next steps.

  The real ray-tracing engine (sionna-rt) is NOT installed here: the Mock
  backend always works and the whole app runs without a GPU. See INSTALL.md for
  the optional `sionna` extra and alternate engine venvs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>

# PowerShell 5.1-compatible: no '&&', no ternary. Fail loudly.
$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Host ""
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    exit 1
}

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# Repo root is the parent of this script's folder.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

$VenvDir    = Join-Path $RepoRoot "backend\.venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

# ------------------------------------------------------------ prerequisites
Step "Checking prerequisites"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) {
    Fail "Python not found on PATH. Install Python 3.11+ from https://www.python.org/downloads/ (check 'Add python.exe to PATH')."
}
Write-Host "  python: $($python.Source)"

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Fail "npm not found on PATH. Install Node.js 20+ from https://nodejs.org/."
}
Write-Host "  npm:    $($npm.Source)"

# ------------------------------------------------------------ backend venv
Step "Backend virtual environment (backend\.venv)"
if (-not (Test-Path $VenvPython)) {
    Write-Host "  creating venv..."
    & $python.Source -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail "python -m venv failed." }
} else {
    Write-Host "  venv already exists, reusing."
}
if (-not (Test-Path $VenvPython)) { Fail "venv python missing after creation: $VenvPython" }

Step "Installing backend (editable + dev extras)"
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }
& $VenvPython -m pip install -e "backend[dev]"
if ($LASTEXITCODE -ne 0) { Fail "backend install failed (pip install -e `"backend[dev]`")." }

# ------------------------------------------------------------ frontend
Step "Installing frontend (npm install in frontend\)"
Push-Location (Join-Path $RepoRoot "frontend")
try {
    & $npm.Source install
    if ($LASTEXITCODE -ne 0) { Fail "npm install failed." }
} finally {
    Pop-Location
}

# ------------------------------------------------------------ demo projects
Step "Generating demo projects (kaist_demo + lab_room)"
& $VenvPython (Join-Path $RepoRoot "examples\scripts\create_demo_project.py")
if ($LASTEXITCODE -ne 0) { Fail "create_demo_project.py failed." }
& $VenvPython (Join-Path $RepoRoot "examples\scripts\import_bundle_scene.py")
if ($LASTEXITCODE -ne 0) { Fail "import_bundle_scene.py failed." }

# ------------------------------------------------------------ done
Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " SionnaTwin Studio install complete." -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Next steps:"
Write-Host "   1. Start both servers:   powershell -ExecutionPolicy Bypass -File scripts\start.ps1"
Write-Host "   2. Open the app:         http://localhost:5173"
Write-Host "      (the KAIST Demo project loads automatically)"
Write-Host ""
Write-Host " Walkthrough: TUTORIAL.md    Install details/troubleshooting: INSTALL.md"
Write-Host ""
