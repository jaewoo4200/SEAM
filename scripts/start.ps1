<#
.SYNOPSIS
  Start the SEAM Studio backend (uvicorn :8000) and frontend (vite :5173)
  in two separate windows, then print the URLs.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#>

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir

$VenvPython = Join-Path $RepoRoot "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Fail "backend venv not found. Run scripts\install.ps1 first."
}
if (-not (Test-Path (Join-Path $RepoRoot "frontend\node_modules"))) {
    Fail "frontend/node_modules not found. Run scripts\install.ps1 first."
}

Write-Host "Starting backend  -> http://127.0.0.1:8000  (uvicorn)" -ForegroundColor Cyan
# Each server runs in its own PowerShell window so logs stay separate and
# Ctrl+C in one does not kill the other.
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$RepoRoot'; & '$VenvPython' -m uvicorn --app-dir backend seam_studio.main:app --port 8000"
)

Write-Host "Starting frontend -> http://localhost:5173  (vite dev, proxies /api to :8000)" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$RepoRoot\frontend'; npm run dev"
)

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " SEAM Studio is starting in two new windows." -ForegroundColor Green
Write-Host "   Backend  : http://127.0.0.1:8000   (API + /api/health)"
Write-Host "   Frontend : http://localhost:5173    <- open this"
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " Close either window (or press Ctrl+C in it) to stop that server."
