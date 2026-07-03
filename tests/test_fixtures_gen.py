from __future__ import annotations

import pymupdf
import pytest

from fixtures_gen import GT_LINES, build_all


def test_build_all_creates_expected_fixture_files(tmp_path):
    paths = build_all(tmp_path)

    assert set(paths) == {"native", "scanned", "rotated", "mixed", "encrypted", "corrupt"}
    for name, path in paths.items():
        assert path == tmp_path / f"{name}.pdf"
        assert path.is_file()


def test_scanned_has_no_text_layer(fixtures_dir):
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")

    assert doc[0].get_text().strip() == ""


def test_native_has_text(fixtures_dir):
    text = pymupdf.open(fixtures_dir / "native.pdf")[0].get_text()

    assert "診斷證明書" in text
    for _, _, expected in GT_LINES:
        assert expected in text


def test_rotated_is_90(fixtures_dir):
    doc = pymupdf.open(fixtures_dir / "rotated.pdf")

    assert doc[0].rotation == 90
    assert doc[0].get_text().strip() == ""


def test_encrypted_needs_pass(fixtures_dir):
    doc = pymupdf.open(fixtures_dir / "encrypted.pdf")

    assert doc.needs_pass
    assert doc.authenticate("test") > 0


def test_mixed_has_scanned_then_native_text(fixtures_dir):
    doc = pymupdf.open(fixtures_dir / "mixed.pdf")

    assert doc.page_count == 2
    assert doc[0].get_text().strip() == ""
    assert "診斷證明書" in doc[1].get_text()


def test_corrupt_file_is_not_openable(fixtures_dir):
    with pytest.raises(pymupdf.FileDataError):
        pymupdf.open(fixtures_dir / "corrupt.pdf")
