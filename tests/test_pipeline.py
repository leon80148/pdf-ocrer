from __future__ import annotations

import csv
import hashlib
import shutil
import threading
from pathlib import Path

import pymupdf

from fixtures_gen import GT_LINES
from pdf_ocrer.config import AppConfig, DebugConfig, LlmConfig, NamingConfig, OcrConfig, OutputConfig
from pdf_ocrer.llm_providers import LLMError
from pdf_ocrer.ocr_engine import OcrLine
from pdf_ocrer.pipeline import FileResult, FileStatus, run_batch

_DPI = 200
_FONT = pymupdf.Font("cjk")
_CSV_HEADER = ["原檔名", "新檔名", "狀態", "總頁數", "OCR頁數", "命名來源", "備註"]
_PROMPT = "$text"


class FakeEngine:
    def __init__(self, lines: list[OcrLine] | None = None) -> None:
        self.lines = _gt_ocr_lines() if lines is None else lines

    def recognize(self, img_rgb) -> list[OcrLine]:  # noqa: ANN001
        return self.lines


class StaticClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.name


class RaisingClient:
    def complete(self, prompt: str) -> str:
        raise LLMError("offline")


def make_cfg(**overrides: object) -> AppConfig:
    return AppConfig(
        ocr=OcrConfig(),
        output=OutputConfig(),
        naming=NamingConfig(),
        llm=LlmConfig(),
        debug=DebugConfig(),
    )


def test_run_batch_statuses_csv_collision_and_preserves_sources(work_folder) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf", "encrypted.pdf", "corrupt.pdf"})
    cfg = make_cfg()
    output_dir = work_folder / cfg.output.subdir_name
    output_dir.mkdir()
    shutil.copy2(work_folder / "native.pdf", output_dir / "decoy.pdf")
    before = _source_hashes(work_folder)
    file_results: list[FileResult] = []

    summary = run_batch(
        work_folder,
        cfg,
        FakeEngine(),
        StaticClient("20260615_診斷證明書"),
        _PROMPT,
        file_cb=file_results.append,
    )

    assert summary.cancelled is False
    assert [result.source.name for result in summary.results] == [
        "corrupt.pdf",
        "encrypted.pdf",
        "native.pdf",
        "scanned.pdf",
    ]
    by_name = {result.source.name: result for result in summary.results}
    assert by_name["corrupt.pdf"].status is FileStatus.FAILED
    assert by_name["encrypted.pdf"].status is FileStatus.SKIPPED_ENCRYPTED
    assert by_name["native.pdf"].status is FileStatus.SUCCESS_EXISTING_TEXT
    assert by_name["scanned.pdf"].status is FileStatus.SUCCESS_OCR
    assert by_name["native.pdf"].output is not None
    assert by_name["native.pdf"].output.name == "20260615_診斷證明書.pdf"
    assert by_name["scanned.pdf"].output is not None
    assert by_name["scanned.pdf"].output.name == "20260615_診斷證明書_2.pdf"
    assert "decoy.pdf" not in {result.source.name for result in summary.results}

    assert summary.csv_path is not None
    assert summary.csv_path.read_bytes()[:3] == b"\xef\xbb\xbf"
    rows = _read_csv(summary.csv_path)
    assert rows[0] == _CSV_HEADER
    assert len(rows) == 1 + len(summary.results)
    assert [row[0] for row in rows[1:]] == [result.source.name for result in summary.results]
    assert _source_hashes(work_folder) == before
    assert len(file_results) == len(summary.results)
    assert [result.source.name for result in file_results] == [
        "corrupt.pdf",
        "encrypted.pdf",
        "native.pdf",
        "scanned.pdf",
    ]
    assert all(isinstance(result, FileResult) for result in file_results)
    assert [result.status for result in file_results] == [result.status for result in summary.results]


def test_run_batch_llm_errors_fallback_and_continue(work_folder) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})

    summary = run_batch(work_folder, make_cfg(), FakeEngine(), RaisingClient(), _PROMPT)

    assert summary.cancelled is False
    assert [result.naming_source for result in summary.results] == ["fallback", "fallback"]
    assert [result.output.name for result in summary.results if result.output is not None] == [
        "native_OCR.pdf",
        "scanned_OCR.pdf",
    ]


def test_run_batch_cancel_event_keeps_completed_rows_only(work_folder) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})
    cancel = threading.Event()

    def progress(file_i: int, file_n: int, page_i: int, page_n: int, filename: str) -> None:
        if file_i == 1 and page_i == page_n:
            cancel.set()

    summary = run_batch(
        work_folder,
        make_cfg(),
        FakeEngine(),
        StaticClient("任意名稱"),
        _PROMPT,
        progress_cb=progress,
        cancel_event=cancel,
    )

    assert summary.cancelled is True
    assert [result.source.name for result in summary.results] == ["native.pdf"]
    assert summary.csv_path is not None
    assert len(_read_csv(summary.csv_path)) == 2


def test_run_batch_empty_folder_returns_no_csv(tmp_path) -> None:
    logs: list[str] = []

    summary = run_batch(tmp_path, make_cfg(), FakeEngine(), None, _PROMPT, log_cb=logs.append)

    assert summary.results == []
    assert summary.csv_path is None
    assert summary.output_dir == tmp_path / OutputConfig().subdir_name
    assert summary.cancelled is False
    assert logs


def _gt_ocr_lines() -> list[OcrLine]:
    return [OcrLine(text, _px_poly(point, fontsize, text), 0.99) for point, fontsize, text in GT_LINES]


def _px_poly(
    point: tuple[float, float],
    fontsize: float,
    text: str,
) -> tuple[tuple[float, float], ...]:
    baseline_x, baseline_y = point
    top = baseline_y - _FONT.ascender * fontsize
    bottom = baseline_y - _FONT.descender * fontsize
    right = baseline_x + _FONT.text_length(text, fontsize)
    scale = _DPI / 72.0
    return (
        (baseline_x * scale, top * scale),
        (right * scale, top * scale),
        (right * scale, bottom * scale),
        (baseline_x * scale, bottom * scale),
    )


def _keep_only(folder: Path, names: set[str]) -> None:
    for path in folder.iterdir():
        if path.is_file() and path.name not in names:
            path.unlink()


def _source_hashes(folder: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.glob("*.pdf"))
    }


def _read_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.reader(file))
