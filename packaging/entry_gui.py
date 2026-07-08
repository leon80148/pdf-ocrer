"""PyInstaller entry point for the windowed GUI executable.

The console CLI reuses ``src/pdf_ocrer/__main__.py`` directly; the GUI has no
equivalent module-level runner, so this tiny wrapper provides one.
"""

from pdf_ocrer.gui import run_gui

if __name__ == "__main__":
    run_gui()
