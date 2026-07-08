# Packaging & Release (maintainers)

This directory builds the Windows installer for pdf-ocrer:
**PyInstaller** (onedir) freezes the app, **Inno Setup** wraps it into a single
`pdf-ocrer-setup-<version>.exe`. The bundled OCR engine is **RapidOCR** with its
offline PP-OCRv6 small models — **PaddleOCR is deliberately excluded** to keep the
installer small (~117 MB vs ~800 MB+ with paddle) and dependency-free.

## You usually don't need to build manually

Releases are automated. To cut a release:

```bash
# 1. Bump the version (single source of truth)
#    edit src/pdf_ocrer/__init__.py -> __version__ = "0.6.0"
# 2. Commit, then tag and push
git tag v0.6.0
git push origin v0.6.0
```

`.github/workflows/release.yml` then runs on a clean `windows-latest` runner:
tests → version-consistency check (tag must equal `__version__`) → install Inno
Setup → `packaging/build.ps1` → publish a GitHub Release with the installer
attached and auto-generated release notes.

## Building locally

Requires Python 3.12, **PowerShell 7+ (`pwsh`)**, and Inno Setup 6
(`winget install JRSoftware.InnoSetup` or `choco install innosetup`).
`build.ps1` uses PowerShell 7 syntax and will not run on stock Windows
PowerShell 5.1.

```powershell
pwsh packaging/build.ps1            # reuses the build venv (fast)
pwsh packaging/build.ps1 -SkipDeps  # skip dependency reinstall (fastest)
pwsh packaging/build.ps1 -Clean     # fully fresh venv + build
```

Output: `packaging/Output/pdf-ocrer-setup-<version>.exe`.

## How it works

| File | Role |
|---|---|
| `build.ps1` | Orchestration (shared by local + CI). Creates a **paddle-free** venv, runs PyInstaller, fails if any `paddle` artifact leaks into the venv or the frozen output, then runs Inno Setup. |
| `pdf_ocrer.spec` | PyInstaller spec: onedir, GUI (`pdf-ocrer-gui.exe`, windowed) + CLI (`pdf-ocrer.exe`, console) sharing one `_internal/`. Collects rapidocr models (no hook exists), customtkinter/tkinterdnd2 data, and excludes paddle. |
| `entry_gui.py` | GUI entry wrapper for PyInstaller (the CLI reuses `src/pdf_ocrer/__main__.py`). |
| `installer.iss` | Inno Setup script. Installs to `Program Files\pdf-ocrer`, Start Menu shortcut with `WorkingDir={app}`. |
| `config.installer.toml` | First-run config, installed as `{app}\config.toml` with `onlyifdoesntexist uninsneveruninstall` — created on fresh install, **never overwritten on upgrade or removed on uninstall**. Defaults to `engine="rapidocr"` and `naming.enabled=false` (no LLM-timeout trap on a fresh clinic PC). |

The installer wizard is English (`Default.isl`, bundled with every Inno Setup).
To localize it, drop an unofficial Traditional-Chinese `.isl` into this directory
and add a `[Languages]` entry to `installer.iss`.

## Notes / future

- **No custom icon** in v1 (PyInstaller/Inno defaults). To add one: drop
  `packaging/app.ico`, set `icon="app.ico"` in both `EXE()` calls in the spec and
  `SetupIconFile=app.ico` in `installer.iss`.
- **No code signing** — the installer will trigger SmartScreen "unknown publisher"
  on first run (normal for unsigned open-source installers).
- **GPU builds are not packaged** — GPU acceleration (CUDA/DirectML) is
  source/pip-only; see the main README's GPU section.
- The runtime dependency list in `build.ps1` (`$RuntimeDeps`) mirrors
  `pyproject.toml` `[project.dependencies]` **minus paddleocr**. Keep it in sync
  when dependencies change.
