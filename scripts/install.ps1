<#
.SYNOPSIS
  SEAM Studio one-command installer (Windows / PowerShell).

  Idempotent: re-running is safe. Creates backend/.venv if missing, installs the
  backend (editable, with the dev and parquet extras), installs the frontend,
  regenerates the demo projects, checks which Dr.Jit compute backend works, and
  prints next steps.

  The real ray-tracing engine (sionna-rt) is a base backend dependency, so it
  installs here too (Dr.Jit/Mitsuba, no GPU needed to install). Running it needs
  an NVIDIA GPU (CUDA) or LLVM-C for the CPU backend (LLVM 18+ and
  DRJIT_LIBLLVM_PATH, see INSTALL.md); with neither, 'auto' solves use the Mock
  backend. See INSTALL.md for alternate engine venvs.

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
    Fail ("npm not found on PATH. Install Node.js 20+ and re-run this script:`n" +
          "         winget install OpenJS.NodeJS.LTS`n" +
          "       (or download from https://nodejs.org/) - then OPEN A NEW terminal so PATH refreshes.")
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

Step "Installing backend (editable + dev and parquet extras)"
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }
& $VenvPython -m pip install -e "backend[dev,parquet]"
if ($LASTEXITCODE -ne 0) { Fail "backend install failed (pip install -e `"backend[dev,parquet]`")." }

# ------------------------------------------------------------ compute backend
Step "Checking the Sionna RT compute backend (Dr.Jit CUDA / LLVM)"
# drjit prints a harmless 'jitc_llvm_init(): LLVM API initialization failed'
# on stderr when LLVM-C is absent; Windows PowerShell turns native stderr into
# errors under "Stop", so relax it for this one probe.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$probeOut = & $VenvPython -c "import drjit as dr; print(int(dr.has_backend(dr.JitBackend.CUDA)), int(dr.has_backend(dr.JitBackend.LLVM)))" 2>$null
$probeCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap
$probeLine = @($probeOut | Where-Object { $_ -match '^[01] [01]$' } | Select-Object -Last 1)
if ($probeCode -ne 0 -or $probeLine.Count -eq 0) {
    Write-Host "  could not probe Dr.Jit (import drjit failed); solves will use the Mock backend. See INSTALL.md." -ForegroundColor Yellow
} elseif ($probeLine[0] -eq "0 0") {
    Write-Host "  No CUDA device and no LLVM-C found: Sionna RT cannot run here, so 'auto' solves use the Mock backend." -ForegroundColor Yellow
    Write-Host "  For the CPU backend install LLVM 18+ (https://github.com/llvm/llvm-project/releases), then:" -ForegroundColor Yellow
    Write-Host "     setx DRJIT_LIBLLVM_PATH `"C:\Program Files\LLVM\bin\LLVM-C.dll`"" -ForegroundColor Yellow
    Write-Host "  and open a new terminal. See INSTALL.md (The real Sionna RT engine)." -ForegroundColor Yellow
} elseif ($probeLine[0].StartsWith("1")) {
    Write-Host "  CUDA backend available (GPU solves)."
} else {
    Write-Host "  LLVM CPU backend available (no CUDA device; solves run on the CPU)."
}

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
# Demo projects (sample_demo + lab_room + ftc_outdoor) are COMMITTED under
# examples/demo_project/. Regeneration from the reference bundle is optional:
# a fresh clone without reference-bundle/ must still install cleanly.
Step "Regenerating demo projects (sample_demo + lab_room + ftc_outdoor)"
& $VenvPython (Join-Path $RepoRoot "examples\scripts\create_demo_project.py")
if ($LASTEXITCODE -ne 0) { Fail "create_demo_project.py failed." }
if (Test-Path (Join-Path $RepoRoot "reference-bundle\indoor\lab_room.xml")) {
    & $VenvPython (Join-Path $RepoRoot "examples\scripts\import_bundle_scene.py")
    if ($LASTEXITCODE -ne 0) { Fail "import_bundle_scene.py failed." }
} else {
    Write-Host " reference-bundle/ not present - keeping the committed lab_room demo." -ForegroundColor Yellow
}
if (Test-Path (Join-Path $RepoRoot "reference-bundle\outdoor_material_assigned_cv_28ghz_safe.xml")) {
    & $VenvPython (Join-Path $RepoRoot "examples\scripts\import_bundle_scene.py") `
        --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" `
        --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor `
        --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
    if ($LASTEXITCODE -ne 0) { Fail "import_bundle_scene.py (ftc_outdoor) failed." }
} else {
    Write-Host " reference-bundle/ not present - keeping the committed ftc_outdoor demo." -ForegroundColor Yellow
}

# ------------------------------------------------------------ done
Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " SEAM Studio install complete." -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Next steps:"
Write-Host "   1. Start both servers:   powershell -ExecutionPolicy Bypass -File scripts\start.ps1"
Write-Host "   2. Open the app:         http://localhost:5173"
Write-Host "      (the Sample Demo project loads automatically)"
Write-Host ""
Write-Host " Walkthrough: TUTORIAL.md    Install details/troubleshooting: INSTALL.md"
Write-Host ""
