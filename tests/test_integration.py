from __future__ import annotations

import numpy as np
import pymupdf
import pytest

from pdf_ocrer.config import OcrConfig
from pdf_ocrer.ocr_engine import PaddleOcrEngine


@pytest.mark.integration
def test_real_paddle_engine_recognizes_scanned_fixture(fixtures_dir) -> None:
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")
    pix = doc[0].get_pixmap(dpi=200, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

    lines = PaddleOcrEngine(OcrConfig()).recognize(img)

    assert "診斷證明書" in [line.text for line in lines]


@pytest.mark.integration
def test_real_process_pdf_adds_searchable_text_layer(fixtures_dir, tmp_path) -> None:
    from fixtures_gen import GT_LINES
    from pdf_ocrer.config import AppConfig, DebugConfig, LlmConfig, NamingConfig, OutputConfig
    from pdf_ocrer.pdf_processor import process_pdf

    cfg = AppConfig(
        ocr=OcrConfig(),
        output=OutputConfig(),
        naming=NamingConfig(),
        llm=LlmConfig(),
        debug=DebugConfig(),
    )
    engine = PaddleOcrEngine(cfg.ocr)

    scanned = process_pdf(fixtures_dir / "scanned.pdf", cfg, engine)
    scanned_out = tmp_path / "scanned_ocr.pdf"
    scanned.doc.subset_fonts()
    scanned.doc.save(scanned_out, garbage=3, deflate=True)
    scanned_page = pymupdf.open(scanned_out)[0]
    scanned_text = scanned_page.get_text()

    for _, _, expected in GT_LINES:
        assert expected in scanned_text
    scanned_rects = scanned_page.search_for("診斷證明書")
    assert scanned_rects
    assert abs(scanned_rects[0].x0 - 72) < 20

    rotated = process_pdf(fixtures_dir / "rotated.pdf", cfg, engine)
    rotated_out = tmp_path / "rotated_ocr.pdf"
    rotated.doc.subset_fonts()
    rotated.doc.save(rotated_out, garbage=3, deflate=True)
    rotated_page = pymupdf.open(rotated_out)[0]

    assert rotated_page.search_for("診斷證明書")
