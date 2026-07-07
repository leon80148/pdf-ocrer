from __future__ import annotations

import threading
from dataclasses import replace

import numpy as np
import pymupdf
import pytest

from fixtures_gen import GT_LINES, build_image_png, build_tiff_multipage
from pdf_ocrer.config import AppConfig, DebugConfig, LlmConfig, NamingConfig, OcrConfig, OutputConfig
from pdf_ocrer.ocr_engine import OcrLine
from pdf_ocrer.pdf_processor import (
    BatchCancelled,
    CancelFlag,
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


def display_bbox_from_px_poly(poly: tuple[tuple[float, float], ...], dpi: int = _DPI) -> pymupdf.Rect:
    scale = 72.0 / dpi
    xs = [point[0] * scale for point in poly[:4]]
    ys = [point[1] * scale for point in poly[:4]]
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


def display_word_rect(page: pymupdf.Page, text: str) -> pymupdf.Rect:
    words = [word for word in page.get_text("words") if word[4] == text]
    assert words

    rect = pymupdf.Rect(words[0][:4])
    for word in words[1:]:
        rect |= pymupdf.Rect(word[:4])

    matrix = page.rotation_matrix
    corners = (
        pymupdf.Point(rect.x0, rect.y0),
        pymupdf.Point(rect.x1, rect.y0),
        pymupdf.Point(rect.x1, rect.y1),
        pymupdf.Point(rect.x0, rect.y1),
    )
    display_points = [point * matrix for point in corners]
    xs = [point.x for point in display_points]
    ys = [point.y for point in display_points]
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


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

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_add_text_layer_display_highlight_tracks_rotated_page(
    fixtures_dir,
    tmp_path,
    rotation: int,
) -> None:
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")
    text = "診斷證明書"
    display_baseline = pymupdf.Point(120, 150)
    poly = px_poly_from_gt(GT_LINES[0], baseline_pt=(display_baseline.x, display_baseline.y))
    line = OcrLine(text, poly, 0.99)
    out = tmp_path / f"rotated_{rotation}_ocr.pdf"

    try:
        page = doc[0]
        page.set_rotation(rotation)
        written = add_text_layer(page, [line], dpi=_DPI)
        doc.save(out)
    finally:
        doc.close()

    out_doc = pymupdf.open(out)
    try:
        p2 = out_doc[0]
        display_rect = display_word_rect(p2, text)
        expected_rect = display_bbox_from_px_poly(poly)

        assert written == 1
        assert p2.rotation == rotation
        assert text in p2.get_text()
        assert display_rect.height < display_rect.width
        assert abs(display_rect.x0 - expected_rect.x0) < 3
        assert abs(display_rect.y0 - expected_rect.y0) < 3
        assert abs(display_rect.width - expected_rect.width) < 3
    finally:
        out_doc.close()


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
    assert len(result.page_texts) == 2
    assert "診斷證明書" in result.page_texts[0]
    assert "高雄市安家診所" in result.page_texts[1]
    assert "診斷證明書" in result.text


def test_process_pdf_png_converts_to_searchable_a4_pdf(tmp_path) -> None:
    src = tmp_path / "scan.png"
    out = tmp_path / "scan_ocr.pdf"
    build_image_png(src)
    line = OcrLine("診斷證明書", px_poly_from_gt(GT_LINES[0]), 0.99)
    engine = FakeEngine([line])

    result = process_pdf(src, make_cfg(), engine)
    try:
        assert result.total_pages == 1
        assert result.ocr_pages == 1
        assert result.reports == [PageReport(page_index=0, action="ocr", line_count=1)]
        assert len(engine.images) == 1
        assert abs(result.doc[0].rect.width - 595) < 2
        assert abs(result.doc[0].rect.height - 842) < 2
        assert "診斷證明書" in result.page_texts[0]
        result.doc.save(out)
    finally:
        result.doc.close()

    reopened = pymupdf.open(out)
    assert "診斷證明書" in reopened[0].get_text()
    assert reopened[0].search_for("診斷證明書")


def test_process_pdf_tiff_preserves_multiple_pages(tmp_path) -> None:
    src = tmp_path / "scan.tiff"
    build_tiff_multipage(src)
    engine = FakeEngine([])

    result = process_pdf(src, make_cfg(), engine)
    try:
        assert result.total_pages == 2
        assert result.ocr_pages == 0
        assert len(engine.images) == 2
    finally:
        result.doc.close()


def test_process_pdf_encrypted_raises(fixtures_dir) -> None:
    with pytest.raises(EncryptedPdfError):
        process_pdf(fixtures_dir / "encrypted.pdf", make_cfg(), FakeEngine([]))


def test_process_pdf_cancel_raises_between_pages(fixtures_dir) -> None:
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(BatchCancelled):
        process_pdf(fixtures_dir / "scanned.pdf", make_cfg(), FakeEngine([]), cancel=cancel)


def test_process_pdf_accepts_duck_typed_cancel_flag(fixtures_dir) -> None:
    class FakeCancel:
        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return False

    cancel: CancelFlag = FakeCancel()
    line = OcrLine("診斷證明書", px_poly_from_gt(GT_LINES[0]), 0.99)

    result = process_pdf(fixtures_dir / "scanned.pdf", make_cfg(), FakeEngine([line]), cancel=cancel)
    try:
        assert result.total_pages == 1
        assert cancel.calls == 1
    finally:
        result.doc.close()
