"""PDF rendering and searchable text-layer insertion.

PyMuPDF 1.28 rotation probe, run for this task: after `page.set_rotation(90)`,
`get_text()` still extracts the same text and `search_for()` rectangles remain in
the unrotated page coordinate space. Rendering and OCR boxes are in rotated
display space, so insertion points derived from pixmap coordinates must be
multiplied by `page.derotation_matrix` before `insert_text()`.
2026-07-08 rotation compensation: `insert_text()` morph matrices must also
include `Matrix(page.rotation)` so display-space highlights keep OCR line
orientation on `/Rotate` pages.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf

from pdf_ocrer.config import AppConfig
from pdf_ocrer.ocr_engine import OcrEngineProtocol, OcrLine

_FONT_NAME = "pdfocr-cjk"
_FONT = pymupdf.Font("cjk")


class EncryptedPdfError(Exception):
    """Raised when a PDF cannot be opened without a user password."""


class BatchCancelled(Exception):
    """Raised when the caller cancels processing between pages."""


def has_text_layer(page: pymupdf.Page, min_chars: int) -> bool:
    return len(page.get_text().strip()) >= min_chars


def render_page(page: pymupdf.Page, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return img.copy()


def add_text_layer(
    page: pymupdf.Page,
    lines: list[OcrLine],
    dpi: int,
    visible: bool = False,
) -> int:
    if not lines:
        return 0

    page.insert_font(fontname=_FONT_NAME, fontbuffer=_FONT.buffer)
    scale = 72.0 / dpi
    written = 0

    for line in lines:
        if len(line.poly) < 4:
            continue

        tl, tr, _br, bl = _display_points(line.poly, scale)
        width = _distance(tl, tr)
        height = _distance(tl, bl)
        if height <= 0:
            continue

        fontsize = height / (_FONT.ascender - _FONT.descender)
        natural_width = _FONT.text_length(line.text, fontsize)
        if natural_width <= 0:
            continue

        sx = width / natural_width
        baseline_display = pymupdf.Point(tl.x, tl.y + _FONT.ascender * fontsize)
        baseline = baseline_display * page.derotation_matrix
        page.insert_text(
            baseline,
            line.text,
            fontsize=fontsize,
            fontname=_FONT_NAME,
            render_mode=0 if visible else 3,
            color=(1, 0, 0) if visible else None,
            morph=(baseline, pymupdf.Matrix(sx, 1.0) * pymupdf.Matrix(page.rotation)),
        )
        written += 1

    return written


@dataclass(frozen=True)
class PageReport:
    page_index: int
    action: str
    line_count: int


@dataclass
class PdfResult:
    doc: pymupdf.Document
    text: str
    page_texts: list[str]
    reports: list[PageReport]
    total_pages: int
    ocr_pages: int


def process_pdf(
    src: Path,
    cfg: AppConfig,
    engine: OcrEngineProtocol,
    page_cb: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> PdfResult:
    doc = pymupdf.open(src)
    try:
        if doc.needs_pass:
            doc.authenticate("")
            if doc.is_encrypted:
                raise EncryptedPdfError(f"PDF requires a password: {src}")

        reports: list[PageReport] = []
        page_texts: list[str] = []
        ocr_pages = 0
        total_pages = doc.page_count

        for page_index in range(total_pages):
            if cancel is not None and cancel.is_set():
                raise BatchCancelled()

            page = doc[page_index]
            if cfg.ocr.skip_pages_with_text and has_text_layer(page, cfg.ocr.min_existing_chars):
                reports.append(PageReport(page_index, "kept_existing", 0))
                page_texts.append(page.get_text())
            else:
                img = render_page(page, cfg.ocr.dpi)
                lines = engine.recognize(img)
                del img

                line_count = add_text_layer(page, lines, cfg.ocr.dpi, visible=cfg.debug.visible_text)
                action = "ocr" if line_count else "empty"
                reports.append(PageReport(page_index, action, line_count))
                if action == "ocr":
                    ocr_pages += 1
                page_texts.append(page.get_text())

            if page_cb is not None:
                page_cb(page_index, total_pages)

        return PdfResult(
            doc=doc,
            text="\n\n".join(page_text.strip() for page_text in page_texts),
            page_texts=page_texts,
            reports=reports,
            total_pages=total_pages,
            ocr_pages=ocr_pages,
        )
    except Exception:
        doc.close()
        raise


def _display_points(
    poly: tuple[tuple[float, float], ...],
    scale: float,
) -> tuple[pymupdf.Point, pymupdf.Point, pymupdf.Point, pymupdf.Point]:
    return tuple(pymupdf.Point(x * scale, y * scale) for x, y in poly[:4])  # type: ignore[return-value]


def _distance(a: pymupdf.Point, b: pymupdf.Point) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)
