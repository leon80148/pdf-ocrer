from __future__ import annotations

import pymupdf
import pytest

from fixtures_gen import GT_LINES, build_all, build_image_jpg, build_image_png, build_tiff_multipage


def test_build_all_creates_expected_fixture_files(tmp_path):
    paths = build_all(tmp_path)

    assert set(paths) == {
        "native",
        "scanned",
        "rotated",
        "rotated_180",
        "rotated_270",
        "mixed",
        "encrypted",
        "corrupt",
    }
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


@pytest.mark.parametrize(
    ("name", "rotation"),
    [("rotated", 90), ("rotated_180", 180), ("rotated_270", 270)],
)
def test_rotated_fixtures_have_requested_rotation(fixtures_dir, name: str, rotation: int):
    doc = pymupdf.open(fixtures_dir / f"{name}.pdf")

    assert doc[0].rotation == rotation
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


@pytest.mark.parametrize(
    ("builder", "filename"),
    [(build_image_png, "scan.png"), (build_image_jpg, "scan.jpg")],
)
def test_image_fixtures_convert_to_single_page_pdf(tmp_path, builder, filename: str):
    path = tmp_path / filename
    builder(path)

    image_doc = pymupdf.open(path)
    pdf_doc = pymupdf.open("pdf", image_doc.convert_to_pdf())

    assert pdf_doc.page_count == 1
    assert abs(pdf_doc[0].rect.width - 595) < 2
    assert abs(pdf_doc[0].rect.height - 842) < 2


def test_tiff_fixture_converts_to_two_page_pdf(tmp_path):
    path = tmp_path / "scan.tiff"
    build_tiff_multipage(path)

    image_doc = pymupdf.open(path)
    pdf_doc = pymupdf.open("pdf", image_doc.convert_to_pdf())

    assert pdf_doc.page_count == 2
