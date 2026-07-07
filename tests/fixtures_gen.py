from __future__ import annotations

from pathlib import Path

import pymupdf

GT_LINES: list[tuple[tuple[float, float], float, str]] = [
    ((72, 100), 20, "診斷證明書"),
    ((72, 140), 12, "病患:王小明 日期:2026年6月15日"),
    ((300, 400), 14, "高雄市安家診所"),
]

PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
FONT_NAME = "cjkF"
_FONT = pymupdf.Font("cjk")


def build_native(path: Path) -> None:
    doc = _new_native_doc()
    _save(doc, path)


def build_scanned(path: Path) -> None:
    doc = _new_scanned_doc()
    _save(doc, path)


def build_rotated(path: Path, rotation: int = 90) -> None:
    doc = _new_scanned_doc()
    doc[0].set_rotation(rotation)
    _save(doc, path)


def build_mixed(path: Path) -> None:
    doc = pymupdf.open()
    _append_scanned_page(doc)
    _append_native_page(doc)
    _save(doc, path)


def build_encrypted(path: Path) -> None:
    doc = _new_scanned_doc()
    _save(
        doc,
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="test",
    )


def build_corrupt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 not really a pdf")


def build_all(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {
        "native": folder / "native.pdf",
        "scanned": folder / "scanned.pdf",
        "rotated": folder / "rotated.pdf",
        "rotated_180": folder / "rotated_180.pdf",
        "rotated_270": folder / "rotated_270.pdf",
        "mixed": folder / "mixed.pdf",
        "encrypted": folder / "encrypted.pdf",
        "corrupt": folder / "corrupt.pdf",
    }
    build_native(paths["native"])
    build_scanned(paths["scanned"])
    build_rotated(paths["rotated"])
    build_rotated(paths["rotated_180"], 180)
    build_rotated(paths["rotated_270"], 270)
    build_mixed(paths["mixed"])
    build_encrypted(paths["encrypted"])
    build_corrupt(paths["corrupt"])
    return paths


def _new_native_doc() -> pymupdf.Document:
    doc = pymupdf.open()
    _append_native_page(doc)
    return doc


def _new_scanned_doc() -> pymupdf.Document:
    doc = pymupdf.open()
    _append_scanned_page(doc)
    return doc


def _append_native_page(doc: pymupdf.Document) -> None:
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_font(fontname=FONT_NAME, fontbuffer=_FONT.buffer)
    for point, fontsize, text in GT_LINES:
        page.insert_text(
            pymupdf.Point(*point),
            text,
            fontsize=fontsize,
            fontname=FONT_NAME,
        )


def _append_scanned_page(doc: pymupdf.Document) -> None:
    native_doc = _new_native_doc()
    native_page = native_doc[0]
    pix = native_page.get_pixmap(dpi=200)
    page = doc.new_page(width=native_page.rect.width, height=native_page.rect.height)
    page.insert_image(page.rect, pixmap=pix)


def _save(doc: pymupdf.Document, path: Path, **kwargs: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    doc.save(path, garbage=3, deflate=True, **kwargs)
    doc.close()
