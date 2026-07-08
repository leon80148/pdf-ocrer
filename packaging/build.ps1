#Requires -Version 7.0
<#
.SYNOPSIS
  Build the pdf-ocrer Windows installer (PyInstaller onedir + Inno Setup).

  Run with PowerShell 7+ (pwsh), e.g. `pwsh packaging/build.ps1`. It uses 7-only
  syntax (ternary, null-conditional) and is invoked via pwsh in CI.

.DESCRIPTION
  Single source of truth for both local builds and the GitHub Actions release
  workflow. Produces packaging\Output\pdf-ocrer-setup-<version>.exe.

  The build deliberately EXCLUDES PaddleOCR/paddlepaddle: a dedicated paddle-free
  venv is created and the output is scanned to guarantee no paddle artifacts ship.
  The bundled OCR engine is RapidOCR with its offline PP-OCRv6 small models.

.PARAMETER Clean
  Wipe the build venv and PyInstaller caches for a fully fresh build.

.PARAMETER SkipDeps
  Reuse the existing build venv without reinstalling dependencies (fast local
  iteration). Ignored if the venv does not exist yet.

.EXAMPLE
  pwsh packaging\build.ps1
  pwsh packaging\build.ps1 -Clean
#>
[CmdletBinding()]
param(
  [switch]$Clean,
  [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $PackagingDir
$VenvDir = Join-Path $PackagingDir ".venv-build"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$DistDir = Join-Path $PackagingDir "dist"
$WorkDir = Join-Path $PackagingDir "build"
$OutputDir = Join-Path $PackagingDir "Output"

# Runtime dependencies = pyproject.toml [project.dependencies] MINUS paddleocr,
# plus the rapidocr extra. Keep in sync with pyproject.toml when deps change.
$RuntimeDeps = @(
  "customtkinter>=6.0",
  "pymupdf>=1.26",
  "openai>=1.60,<3",
  "tkinterdnd2>=0.5",
  "tomlkit>=0.12",
  "rapidocr>=3.9",
  "onnxruntime>=1.19"
)

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# --- 1. Clean -----------------------------------------------------------------
if ($Clean) {
  Write-Step "Clean: removing venv and build artifacts"
  foreach ($p in @($VenvDir, $DistDir, $WorkDir)) {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
  }
}

# --- 2. Build venv ------------------------------------------------------------
if (-not (Test-Path $VenvPython)) {
  Write-Step "Creating paddle-free build venv"
  $basePython = (Get-Command py -ErrorAction SilentlyContinue) `
    ? "py -3.12" : (Get-Command python).Source
  & cmd /c "$basePython -m venv `"$VenvDir`""
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
  $SkipDeps = $false  # a fresh venv must install deps
}

# --- 3. Dependencies ----------------------------------------------------------
if (-not $SkipDeps) {
  Write-Step "Installing build dependencies (no paddle)"
  & $VenvPython -m pip install --upgrade pip | Out-Null
  # --no-deps avoids pulling paddleocr (a hard dependency in pyproject.toml).
  & $VenvPython -m pip install --no-deps $RepoRoot
  if ($LASTEXITCODE -ne 0) { throw "pip install (project) failed" }
  & $VenvPython -m pip install @RuntimeDeps
  if ($LASTEXITCODE -ne 0) { throw "pip install (runtime deps) failed" }
  & $VenvPython -m pip install pyinstaller pyinstaller-hooks-contrib
  if ($LASTEXITCODE -ne 0) { throw "pip install (pyinstaller) failed" }
}

# --- 4. Paddle-absence check #1 (venv) ---------------------------------------
Write-Step "Verifying no paddle in build venv"
$pkgs = & $VenvPython -m pip list 2>$null
if ($pkgs -match "paddle") {
  throw "Paddle package found in build venv — the build must be paddle-free."
}
Write-Host "OK: no paddle package in venv"

# --- 5. Version (single source of truth) --------------------------------------
$AppVersion = (& $VenvPython -c "from pdf_ocrer import __version__; print(__version__)").Trim()
if (-not $AppVersion) { throw "Could not read pdf_ocrer.__version__" }
Write-Host "App version: $AppVersion"

# --- 6. PyInstaller -----------------------------------------------------------
Write-Step "Running PyInstaller (onedir)"
$piArgs = @(
  "-m", "PyInstaller",
  (Join-Path $PackagingDir "pdf_ocrer.spec"),
  "--noconfirm",
  "--distpath", $DistDir,
  "--workpath", $WorkDir
)
if ($Clean) { $piArgs += "--clean" }
& $VenvPython @piArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

# --- 7. Paddle-absence check #2 (frozen output) -------------------------------
Write-Step "Verifying no paddle in frozen output"
$paddleHits = Get-ChildItem -Path (Join-Path $DistDir "pdf_ocrer") -Recurse -Force |
  Where-Object { $_.Name -match "paddle" }
if ($paddleHits) {
  $paddleHits | ForEach-Object { Write-Host "  $($_.FullName)" }
  throw "Paddle artifacts found in frozen output."
}
Write-Host "OK: no paddle artifacts in dist"

# --- 8. Inno Setup ------------------------------------------------------------
Write-Step "Compiling installer with Inno Setup"
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue)?.Source
if (-not $iscc) {
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
  )
  $iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $iscc) {
  throw "Inno Setup Compiler (iscc) not found. Install it: choco install innosetup (CI) or winget install JRSoftware.InnoSetup"
}
New-Item -ItemType Directory -Force $OutputDir | Out-Null
& $iscc "/DMyAppVersion=$AppVersion" (Join-Path $PackagingDir "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed" }

# --- 9. Report ----------------------------------------------------------------
$installer = Join-Path $OutputDir "pdf-ocrer-setup-$AppVersion.exe"
if (-not (Test-Path $installer)) { throw "Expected installer not found: $installer" }
$sizeMB = [math]::Round((Get-Item $installer).Length / 1MB, 1)
Write-Step "Build complete"
Write-Host "Installer: $installer"
Write-Host "Size:      $sizeMB MB"
