from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    from fixtures_gen import build_all

    folder = tmp_path_factory.mktemp("pdf_fixtures")
    build_all(folder)
    return folder


@pytest.fixture()
def work_folder(fixtures_dir, tmp_path) -> Path:
    folder = tmp_path / "fixtures"
    folder.mkdir()
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, folder / path.name)
    return folder
