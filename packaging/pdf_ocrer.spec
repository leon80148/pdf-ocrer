# PyInstaller spec — pdf_ocrer (onedir, RapidOCR-only, GUI + CLI sharing one COLLECT)
#
# Build:  pyinstaller packaging/pdf_ocrer.spec --noconfirm
# Output: dist/pdf_ocrer/  (pdf-ocrer-gui.exe + pdf-ocrer.exe + shared _internal/)
#
# PaddleOCR/paddlepaddle are intentionally excluded from the packaged build
# (see `excludes` below and the paddle-free build venv in build.ps1). The
# bundled OCR engine is RapidOCR with its wheel-bundled PP-OCRv6 small ONNX
# models (offline, no download).

import os

from PyInstaller.utils.hooks import collect_data_files

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))


def project_path(*parts):
    return os.path.join(PROJECT_ROOT, *parts)


# Runtime data files shipped inside _internal/ ---------------------------------
# rapidocr has NO pyinstaller-hooks-contrib hook, so its bundled ONNX models
# (site-packages/rapidocr/models/*.onnx, ~31 MB) must be collected explicitly,
# or first-run OCR would try to download them (defeating offline operation).
datas = collect_data_files("rapidocr")
# customtkinter + tkinterdnd2 DO have contrib hooks, but collecting their data
# explicitly is idempotent, cheap insurance against hook-version drift.
datas += collect_data_files("customtkinter")
datas += collect_data_files("tkinterdnd2")
# App resources placed next to the executables (working dir = install dir).
datas += [
    (project_path("config.example.toml"), "."),
    (project_path("packaging", "config.installer.toml"), "."),
    (project_path("naming_prompt.txt"), "."),
    (project_path("LICENSE"), "."),
]

hiddenimports = [
    # onnxruntime's compiled extension is frequently missed for rapidocr-based
    # frozen apps; add it defensively (the contrib onnxruntime hook covers the
    # provider DLLs, this covers the pybind11 module import).
    "onnxruntime.capi.onnxruntime_pybind11_state",
]

excludes = [
    "paddleocr",
    "paddlex",
    "paddle",
    "paddlepaddle",
]

common = dict(
    pathex=[project_path("src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

gui_analysis = Analysis([project_path("packaging", "entry_gui.py")], **common)
cli_analysis = Analysis([project_path("src", "pdf_ocrer", "__main__.py")], **common)

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="pdf-ocrer-gui",
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="pdf-ocrer",
    console=True,
    icon=None,
)

coll = COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="pdf_ocrer",
)
