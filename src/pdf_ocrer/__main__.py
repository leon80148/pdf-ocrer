import multiprocessing

from pdf_ocrer.cli import main

if __name__ == "__main__":
    # Required before any ProcessPoolExecutor spawn in a frozen build, or each
    # worker would recursively relaunch the app. Must be the first call.
    multiprocessing.freeze_support()
    raise SystemExit(main())
