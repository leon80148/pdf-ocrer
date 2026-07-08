"""Guardrails for the Windows packaging artifacts (packaging/).

These run in CI's release gate so a broken installer config, a stale pyproject
version wiring, or a paddle leak into the packaged config is caught before a tag
is ever cut — without needing to actually build the installer.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pdf_ocrer.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = REPO_ROOT / "packaging"


def test_installer_config_is_valid_and_rapidocr_cpu() -> None:
    cfg = load_config(PACKAGING / "config.installer.toml")

    # The packaged build has no paddle, so the installed default MUST be rapidocr.
    assert cfg.ocr.engine == "rapidocr"
    # CPU-only packaged build; no GPU onnxruntime is bundled.
    assert cfg.ocr.device == "cpu"
    assert cfg.ocr.model_type == "small"
    # Avoid the up-to-60s Ollama-timeout-per-file trap on a fresh clinic PC.
    assert cfg.naming.enabled is False


def test_pyproject_version_is_dynamic_from_init() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" in pyproject["project"]["dynamic"]
    assert "version" not in pyproject["project"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "src/pdf_ocrer/__init__.py"


def test_packaging_files_exist() -> None:
    for name in ("pdf_ocrer.spec", "installer.iss", "build.ps1", "entry_gui.py"):
        assert (PACKAGING / name).is_file(), f"missing packaging/{name}"


def test_installer_spec_excludes_paddle() -> None:
    spec = (PACKAGING / "pdf_ocrer.spec").read_text(encoding="utf-8")

    for pkg in ("paddleocr", "paddlepaddle", "paddlex"):
        assert pkg in spec, f"spec should list {pkg} in excludes"
    assert 'collect_data_files("rapidocr")' in spec


def test_release_workflow_present() -> None:
    workflow = REPO_ROOT / ".github" / "workflows" / "release.yml"
    assert workflow.is_file()
    text = workflow.read_text(encoding="utf-8")
    assert "build.ps1" in text
    assert "__version__" in text  # tag/version consistency gate
