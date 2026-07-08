"""PyInstaller entry point for the windowed GUI executable.

The console CLI reuses ``src/pdf_ocrer/__main__.py`` directly; the GUI has no
equivalent module-level runner, so this tiny wrapper provides one.
"""

import multiprocessing

from pdf_ocrer.gui import run_gui

if __name__ == "__main__":
    # Required before any ProcessPoolExecutor spawn in a frozen build, or each
    # worker would recursively relaunch the app. Must be the first call.
    multiprocessing.freeze_support()
    run_gui()
