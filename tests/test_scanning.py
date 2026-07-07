from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pdf_ocrer.config import InputConfig
from pdf_ocrer.scanning import scan_inputs


def test_scan_inputs_non_recursive_top_level_pdfs_sorted_and_excludes_output(tmp_path: Path) -> None:
    _write(tmp_path / "B.PDF")
    _write(tmp_path / "a.pdf")
    _write(tmp_path / "note.txt")
    _write(tmp_path / "sub" / "nested.pdf")
    _write(tmp_path / "OCR輸出" / "hidden.pdf")

    items = scan_inputs(tmp_path, "OCR輸出", InputConfig())

    assert [(item.src, item.rel) for item in items] == [
        (tmp_path / "a.pdf", "a.pdf"),
        (tmp_path / "B.PDF", "B.PDF"),
    ]


def test_scan_inputs_recursive_calculates_posix_rel_and_prunes_output_dirs(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "top.pdf")
    _write(tmp_path / "sub" / "B.pdf")
    _write(tmp_path / "sub" / "deep" / "a.PDF")
    _write(tmp_path / "sub" / "deep" / "note.txt")
    _write(tmp_path / "OCR輸出" / "root-decoy.pdf")
    _write(tmp_path / "sub" / "OCR輸出" / "nested-decoy.pdf")

    items = scan_inputs(tmp_path, "OCR輸出", InputConfig(recursive=True))

    assert [item.rel for item in items] == [
        "sub/B.pdf",
        "sub/deep/a.PDF",
        "top.pdf",
    ]
    assert [item.src for item in items] == [
        tmp_path / "sub" / "B.pdf",
        tmp_path / "sub" / "deep" / "a.PDF",
        tmp_path / "top.pdf",
    ]


def test_scan_inputs_recursive_warns_when_pruning_nested_output_dir(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write(tmp_path / "top.pdf")
    _write(tmp_path / "OCR輸出" / "root-decoy.pdf")
    nested_output = tmp_path / "sub" / "OCR輸出"
    _write(nested_output / "nested-decoy.pdf")

    with caplog.at_level(logging.WARNING, logger="pdf_ocrer.scanning"):
        items = scan_inputs(tmp_path, "OCR輸出", InputConfig(recursive=True))

    assert [item.rel for item in items] == ["top.pdf"]
    assert str(nested_output) in caplog.text
    assert str(tmp_path / "OCR輸出") not in caplog.text


def test_scan_inputs_recursive_skips_revisited_resolved_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pdf_ocrer.scanning as scanning_module

    loop_dir = tmp_path / "loop"
    _write(loop_dir / "same.pdf")

    def fake_walk(folder: Path, topdown: bool):  # noqa: ANN202
        assert folder == tmp_path
        assert topdown is True
        yield tmp_path, ["loop"], []
        yield loop_dir, [], ["same.pdf"]
        yield loop_dir, [], ["same.pdf"]

    monkeypatch.setattr(scanning_module.os, "walk", fake_walk)

    items = scan_inputs(tmp_path, "OCR輸出", InputConfig(recursive=True))

    assert [(item.src, item.rel) for item in items] == [(loop_dir / "same.pdf", "loop/same.pdf")]


def test_scan_inputs_recursive_false_ignores_subfolders(tmp_path: Path) -> None:
    _write(tmp_path / "top.pdf")
    _write(tmp_path / "sub" / "nested.pdf")

    items = scan_inputs(tmp_path, "OCR輸出", InputConfig(recursive=False))

    assert [item.rel for item in items] == ["top.pdf"]


def test_scan_inputs_includes_pdf_and_configured_images(tmp_path: Path) -> None:
    _write(tmp_path / "doc.pdf")
    _write(tmp_path / "photo.JPG")
    _write(tmp_path / "scan.png")
    _write(tmp_path / "pages.TIFF")
    _write(tmp_path / "note.txt")

    items = scan_inputs(tmp_path, "OCR輸出", InputConfig())

    assert [item.rel for item in items] == [
        "doc.pdf",
        "pages.TIFF",
        "photo.JPG",
        "scan.png",
    ]


def test_scan_inputs_empty_image_extensions_only_includes_pdfs(tmp_path: Path) -> None:
    _write(tmp_path / "doc.pdf")
    _write(tmp_path / "photo.jpg")
    _write(tmp_path / "scan.png")

    items = scan_inputs(tmp_path, "OCR輸出", InputConfig(image_extensions=()))

    assert [item.rel for item in items] == ["doc.pdf"]


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n")
