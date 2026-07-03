from __future__ import annotations

import threading
from dataclasses import replace

import numpy as np
import pymupdf
import pytest

from fixtures_gen import GT_LINES
from pdf_ocrer.config import AppConfig, DebugConfig, LlmConfig, NamingConfig, OcrConfig, OutputConfig
from pdf_ocrer.ocr_engine import OcrLine
from pdf_ocrer.pdf_processor import (
    BatchCancelled,
    EncryptedPdfError,
    PageReport,
    add_text_layer,
    process_pdf,
    render_page,
)

_FONT = pymupdf.Font("cjk")
_DPI = 200


class FakeEngine:
    def __init__(self, lines: list[OcrLine]):
        self.lines = lines
        self.images: list[np.ndarray] = []

    def recognize(self, img_rgb: np.ndarray) -> list[OcrLine]:
        self.images.append(img_rgb)
        return self.lines


def make_cfg(**ocr_overrides: object) -> AppConfig:
    return AppConfig(
        ocr=replace(OcrConfig(), **ocr_overrides),
        output=OutputConfig(),
        naming=NamingConfig(),
        llm=LlmConfig(),
        debug=DebugConfig(),
    )


def px_poly_from_gt(
    gt_line: tuple[tuple[float, float], float, str],
    dpi: int = _DPI,
    *,
    baseline_pt: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], ...]:
    point, fontsize, text = gt_line
    baseline_x, baseline_y = point if baseline_pt is None else baseline_pt
    top = baseline_y - _FONT.ascender * fontsize
    bottom = baseline_y - _FONT.descender * fontsize
    right = baseline_x + _FONT.text_length(text, fontsize)
    scale = dpi / 72.0
    return (
        (baseline_x * scale, top * scale),
        (right * scale, top * scale),
        (right * scale, bottom * scale),
        (baseline_x * scale, bottom * scale),
    )


def test_render_page_dims(fixtures_dir) -> None:
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")

    img = render_page(doc[0], _DPI)

    assert img.shape == (2339, 1653, 3)
    assert img.dtype == np.uint8


def test_add_text_layer_roundtrip_search(fixtures_dir, tmp_path) -> None:
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")
    page = doc[0]
    line = OcrLine("診斷證明書", px_poly_from_gt(GT_LINES[0]), 0.99)

    written = add_text_layer(page, [line], dpi=_DPI)
    out = tmp_path / "ocr.pdf"
    doc.save(out)
    p2 = pymupdf.open(out)[0]

    assert written == 1
    assert "診斷證明書" in p2.get_text()
    rects = p2.search_for("診斷證明書")
    assert rects
    assert abs(rects[0].x0 - 72) < 20
    assert abs(rects[0].y1 - 100) < 25


def test_add_text_layer_rotated_page_search_rect_is_unrotated(fixtures_dir, tmp_path) -> None:
    doc = pymupdf.open(fixtures_dir / "rotated.pdf")
    page = doc[0]
    display_baseline = pymupdf.Point(120, 150)
    expected_baseline = display_baseline * page.derotation_matrix
    line = OcrLine(
        "診斷證明書",
        px_poly_from_gt(GT_LINES[0], baseline_pt=(display_baseline.x, display_baseline.y)),
        0.99,
    )

    written = add_text_layer(page, [line], dpi=_DPI)
    out = tmp_path / "rotated_ocr.pdf"
    doc.save(out)
    p2 = pymupdf.open(out)[0]

    assert written == 1
    assert p2.rotation == 90
    assert "診斷證明書" in p2.get_text()
    rects = p2.search_for("診斷證明書")
    assert rects
    assert abs(rects[0].x0 - expected_baseline.x) < 20
    assert abs(rects[0].y1 - expected_baseline.y) < 25


def test_invisible_by_default_is_pixel_identical(fixtures_dir) -> None:
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")
    page = doc[0]
    before = render_page(page, _DPI)

    add_text_layer(page, [OcrLine("診斷證明書", px_poly_from_gt(GT_LINES[0]), 0.99)], dpi=_DPI)
    after = render_page(page, _DPI)

    assert np.array_equal(before, after)


def test_visible_debug_mode_changes_pixels(fixtures_dir) -> None:
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")
    page = doc[0]
    before = render_page(page, _DPI)

    add_text_layer(
        page,
        [OcrLine("診斷證明書", px_poly_from_gt(GT_LINES[0]), 0.99)],
        dpi=_DPI,
        visible=True,
    )
    after = render_page(page, _DPI)

    assert not np.array_equal(before, after)


def test_process_pdf_mixed_keeps_existing_text_page(fixtures_dir) -> None:
    line = OcrLine("診斷證明書", px_poly_from_gt(GT_LINES[0]), 0.99)
    engine = FakeEngine([line])

    result = process_pdf(fixtures_dir / "mixed.pdf", make_cfg(), engine)

    assert engine.images
    assert result.total_pages == 2
    assert result.ocr_pages == 1
    assert result.reports == [
        PageReport(page_index=0, action="ocr", line_count=1),
        PageReport(page_index=1, action="kept_existing", line_count=0),
    ]
    assert "診斷證明書" in result.text


def test_process_pdf_encrypted_raises(fixtures_dir) -> None:
    with pytest.raises(EncryptedPdfError):
        process_pdf(fixtures_dir / "encrypted.pdf", make_cfg(), FakeEngine([]))


def test_process_pdf_cancel_raises_between_pages(fixtures_dir) -> None:
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(BatchCancelled):
        process_pdf(fixtures_dir / "scanned.pdf", make_cfg(), FakeEngine([]), cancel=cancel)
