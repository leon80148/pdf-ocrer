from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import threading
from pathlib import Path

import pymupdf

from fixtures_gen import GT_LINES, build_image_png
from pdf_ocrer.app_logging import setup_logging
from pdf_ocrer.config import (
    AppConfig,
    DebugConfig,
    InputConfig,
    LlmConfig,
    LoggingConfig,
    NamingConfig,
    OcrConfig,
    OutputConfig,
    PerformanceConfig,
)
from pdf_ocrer.llm_providers import LLMError
from pdf_ocrer.manifest import MANIFEST_NAME
from pdf_ocrer.ocr_engine import OcrLine
from pdf_ocrer.pdf_processor import PageReport, PdfResult
from pdf_ocrer.pipeline import FileResult, FileStatus, _text_for_naming, run_batch

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


class FakeDoc:
    def subset_fonts(self) -> None:
        pass

    def save(self, output: Path, garbage: int, deflate: bool) -> None:
        output.write_bytes(b"%PDF-1.7\n")

    def close(self) -> None:
        pass


def make_cfg(**overrides: object) -> AppConfig:
    values = {
        "ocr": OcrConfig(),
        "output": OutputConfig(),
        "naming": NamingConfig(),
        "llm": LlmConfig(),
        "debug": DebugConfig(),
        "logging": LoggingConfig(),
    }
    values.update(overrides)
    return AppConfig(**values)


def teardown_function() -> None:
    _remove_pdf_ocrer_file_handlers()


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


def test_run_batch_writes_batch_and_file_status_lines_to_log(work_folder, tmp_path) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})
    log_dir = tmp_path / "logs"
    cfg = make_cfg(logging=LoggingConfig(dir=str(log_dir)))
    setup_logging(cfg.logging)

    summary = run_batch(
        work_folder,
        cfg,
        FakeEngine(),
        StaticClient("20260615_診斷證明書"),
        _PROMPT,
    )
    second = run_batch(
        work_folder,
        cfg,
        FakeEngine(),
        StaticClient("20260615_診斷證明書"),
        _PROMPT,
    )
    _flush_pdf_ocrer_file_handlers()

    text = (log_dir / "pdf_ocrer.log").read_text(encoding="utf-8")
    assert "batch start" in text
    assert f"folder={work_folder}" in text
    for result in summary.results:
        output_name = "" if result.output is None else result.output.name
        assert f"source={result.source.name}" in text
        assert f"status={result.status.value}" in text
        assert f"output={output_name}" in text
    assert "batch end" in text
    assert "results=2" in text
    assert "skipped_done=2" in text
    assert [result.status for result in second.results] == [
        FileStatus.SKIPPED_DONE,
        FileStatus.SKIPPED_DONE,
    ]


def test_run_batch_default_logging_uses_isolated_localappdata(work_folder) -> None:
    _keep_only(work_folder, {"scanned.pdf"})
    local_appdata = Path(os.environ["LOCALAPPDATA"])
    assert local_appdata.name.startswith("localappdata")
    cfg = make_cfg(logging=LoggingConfig())
    log_path = setup_logging(cfg.logging)

    summary = run_batch(work_folder, cfg, FakeEngine(), StaticClient("isolated"), _PROMPT)
    _flush_pdf_ocrer_file_handlers()

    assert summary.results
    assert log_path == local_appdata / "pdf_ocrer" / "logs" / "pdf_ocrer.log"
    assert log_path.exists()


def test_run_batch_export_txt_writes_page_sections(work_folder) -> None:
    _keep_only(work_folder, {"mixed.pdf"})
    cfg = make_cfg(output=OutputConfig(export_txt=True), naming=NamingConfig(enabled=False))

    summary = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    result = summary.results[0]
    assert result.output is not None
    txt_path = result.output.with_suffix(".txt")
    assert txt_path.exists()
    assert txt_path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = txt_path.read_text(encoding="utf-8-sig")
    assert "--- 第 1 頁 ---" in text
    assert "--- 第 2 頁 ---" in text
    assert "診斷證明書" in text
    assert "病患:王小明 日期:2026年6月15日" in text
    assert "高雄市安家診所" in text


def test_run_batch_export_txt_disabled_does_not_write_txt(work_folder) -> None:
    _keep_only(work_folder, {"scanned.pdf"})

    summary = run_batch(work_folder, make_cfg(naming=NamingConfig(enabled=False)), FakeEngine(), None, _PROMPT)

    result = summary.results[0]
    assert result.output is not None
    assert not result.output.with_suffix(".txt").exists()


def test_run_batch_export_txt_skips_all_empty_text(work_folder) -> None:
    _keep_only(work_folder, {"scanned.pdf"})
    cfg = make_cfg(output=OutputConfig(export_txt=True), naming=NamingConfig(enabled=False))

    summary = run_batch(work_folder, cfg, FakeEngine([]), None, _PROMPT)

    result = summary.results[0]
    assert result.status is FileStatus.NO_TEXT_FOUND
    assert result.output is not None
    assert not result.output.with_suffix(".txt").exists()


def test_run_batch_export_txt_writes_existing_text_copy(work_folder) -> None:
    _keep_only(work_folder, {"native.pdf"})
    cfg = make_cfg(output=OutputConfig(export_txt=True), naming=NamingConfig(enabled=False))

    summary = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    result = summary.results[0]
    assert result.status is FileStatus.SUCCESS_EXISTING_TEXT
    assert result.output is not None
    text = result.output.with_suffix(".txt").read_text(encoding="utf-8-sig")
    assert "--- 第 1 頁 ---" in text
    assert "診斷證明書" in text


def test_run_batch_export_txt_replaces_unencodable_surrogates(work_folder, monkeypatch) -> None:
    _keep_only(work_folder, {"scanned.pdf"})
    cfg = make_cfg(output=OutputConfig(export_txt=True), naming=NamingConfig(enabled=False))

    def fake_process_pdf(*args, **kwargs) -> PdfResult:  # noqa: ANN002, ANN003
        return PdfResult(
            doc=FakeDoc(),
            text="bad\udc80x",
            page_texts=["bad\udc80x"],
            reports=[PageReport(page_index=0, action="ocr", line_count=1)],
            total_pages=1,
            ocr_pages=1,
        )

    monkeypatch.setattr("pdf_ocrer.pipeline.process_pdf", fake_process_pdf)

    summary = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    result = summary.results[0]
    assert result.status is FileStatus.SUCCESS_OCR
    assert result.output is not None
    text = result.output.with_suffix(".txt").read_text(encoding="utf-8-sig")
    assert "bad?x" in text


def test_run_batch_recursive_mirrors_outputs_csv_rel_and_per_dir_collision(
    work_folder: Path,
    fixtures_dir: Path,
) -> None:
    _keep_only(work_folder, set())
    (work_folder / "alpha").mkdir()
    (work_folder / "beta").mkdir()
    (work_folder / OutputConfig().subdir_name).mkdir()
    (work_folder / "alpha" / OutputConfig().subdir_name).mkdir()
    shutil.copy2(fixtures_dir / "native.pdf", work_folder / "alpha" / "a.pdf")
    shutil.copy2(fixtures_dir / "native.pdf", work_folder / "beta" / "a.pdf")
    shutil.copy2(
        fixtures_dir / "native.pdf",
        work_folder / OutputConfig().subdir_name / "top-decoy.pdf",
    )
    shutil.copy2(
        fixtures_dir / "native.pdf",
        work_folder / "alpha" / OutputConfig().subdir_name / "nested-decoy.pdf",
    )
    progress_names: list[str] = []
    cfg = make_cfg(
        input=InputConfig(recursive=True),
        naming=NamingConfig(enabled=False),
    )

    summary = run_batch(
        work_folder,
        cfg,
        FakeEngine(),
        None,
        _PROMPT,
        progress_cb=lambda file_i, file_n, page_i, page_n, name: progress_names.append(name),
    )

    assert summary.cancelled is False
    assert [result.rel for result in summary.results] == ["alpha/a.pdf", "beta/a.pdf"]
    assert all(result.status is FileStatus.SUCCESS_EXISTING_TEXT for result in summary.results)
    assert (summary.output_dir / "alpha" / "a_OCR.pdf").exists()
    assert (summary.output_dir / "beta" / "a_OCR.pdf").exists()
    assert not (summary.output_dir / "beta" / "a_OCR_2.pdf").exists()
    assert progress_names == ["alpha/a.pdf", "beta/a.pdf"]

    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert [row[0] for row in rows[1:]] == ["alpha/a.pdf", "beta/a.pdf"]
    assert [row[1] for row in rows[1:]] == ["alpha/a_OCR.pdf", "beta/a_OCR.pdf"]


def test_run_batch_recursive_incremental_second_run_skips_nested_outputs(
    work_folder: Path,
    fixtures_dir: Path,
) -> None:
    _keep_only(work_folder, set())
    (work_folder / "alpha").mkdir()
    (work_folder / "beta" / "deep").mkdir(parents=True)
    shutil.copy2(fixtures_dir / "native.pdf", work_folder / "alpha" / "a.pdf")
    shutil.copy2(fixtures_dir / "native.pdf", work_folder / "beta" / "deep" / "b.pdf")
    cfg = make_cfg(input=InputConfig(recursive=True), naming=NamingConfig(enabled=False))

    first = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)
    second = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    assert first.csv_path is not None
    assert second.csv_path is None
    assert [result.rel for result in second.results] == ["alpha/a.pdf", "beta/deep/b.pdf"]
    assert [result.status for result in second.results] == [
        FileStatus.SKIPPED_DONE,
        FileStatus.SKIPPED_DONE,
    ]
    assert [result.output for result in second.results] == [
        first.output_dir / "alpha" / "a_OCR.pdf",
        first.output_dir / "beta" / "deep" / "b_OCR.pdf",
    ]


def test_run_batch_image_input_writes_searchable_pdf_and_csv(work_folder: Path) -> None:
    _keep_only(work_folder, set())
    build_image_png(work_folder / "scan.png")
    cfg = make_cfg(naming=NamingConfig(enabled=False))

    summary = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    assert summary.cancelled is False
    assert len(summary.results) == 1
    result = summary.results[0]
    assert result.rel == "scan.png"
    assert result.status is FileStatus.SUCCESS_OCR
    assert result.output == summary.output_dir / "scan_OCR.pdf"
    assert result.output.exists()
    doc = pymupdf.open(result.output)
    assert "診斷證明書" in doc[0].get_text()

    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert rows[1][0] == "scan.png"
    assert rows[1][1] == "scan_OCR.pdf"
    assert rows[1][2] == FileStatus.SUCCESS_OCR.value


def test_run_batch_incremental_second_run_skips_done_and_writes_no_csv(
    work_folder: Path,
) -> None:
    _keep_only(work_folder, {"native.pdf"})
    cfg = make_cfg(naming=NamingConfig(enabled=False))
    logs: list[str] = []

    first = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)
    second = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT, log_cb=logs.append)

    assert first.csv_path is not None
    assert second.csv_path is None
    assert len(list(first.output_dir.glob("對照表_*.csv"))) == 1
    assert [result.status for result in second.results] == [FileStatus.SKIPPED_DONE]
    assert second.results[0].output == first.results[0].output
    assert second.results[0].rel == "native.pdf"
    assert second.results[0].note == ""
    assert logs == ["已處理-跳過（先前已完成）: native.pdf"]


def test_run_batch_incremental_resumes_after_cancel_from_unfinished_files(
    work_folder: Path,
) -> None:
    _keep_only(work_folder, {"native.pdf", "scanned.pdf"})
    cfg = make_cfg(naming=NamingConfig(enabled=False))
    cancel = threading.Event()

    def cancel_after_first(file_i: int, file_n: int, page_i: int, page_n: int, filename: str) -> None:
        if file_i == 1 and page_i == page_n:
            cancel.set()

    first = run_batch(
        work_folder,
        cfg,
        FakeEngine(),
        None,
        _PROMPT,
        progress_cb=cancel_after_first,
        cancel_event=cancel,
    )
    second = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    assert first.cancelled is True
    assert [result.source.name for result in first.results] == ["native.pdf"]
    assert [result.status for result in second.results] == [
        FileStatus.SKIPPED_DONE,
        FileStatus.SUCCESS_OCR,
    ]
    assert second.csv_path is not None
    rows = _read_csv(second.csv_path)
    assert len(rows) == 2
    assert rows[1][0] == "scanned.pdf"


def test_run_batch_incremental_source_mtime_change_reprocesses(work_folder: Path) -> None:
    _keep_only(work_folder, {"native.pdf"})
    cfg = make_cfg(naming=NamingConfig(enabled=False))
    source = work_folder / "native.pdf"

    run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)
    _touch_mtime(source)
    second = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    assert [result.status for result in second.results] == [FileStatus.SUCCESS_EXISTING_TEXT]
    assert second.csv_path is not None


def test_run_batch_incremental_source_change_replaces_previous_output_and_txt(
    work_folder: Path,
    fixtures_dir: Path,
) -> None:
    _keep_only(work_folder, {"native.pdf"})
    first_cfg = make_cfg(output=OutputConfig(export_txt=True), naming=NamingConfig(enabled=False))

    first = run_batch(work_folder, first_cfg, FakeEngine(), None, _PROMPT)
    output_dir = first.output_dir
    old_output = output_dir / "native_OCR.pdf"
    old_txt = old_output.with_suffix(".txt")
    assert first.results[0].output == old_output
    assert old_txt.exists()

    shutil.copy2(fixtures_dir / "scanned.pdf", work_folder / "native.pdf")
    _touch_mtime(work_folder / "native.pdf")
    logs: list[str] = []
    second_cfg = make_cfg(output=OutputConfig(export_txt=False), naming=NamingConfig(enabled=False))

    second = run_batch(
        work_folder,
        second_cfg,
        FakeEngine([OcrLine("新版內容", _px_poly((72, 72), 12, "新版內容"), 0.99)]),
        None,
        _PROMPT,
        log_cb=logs.append,
    )

    assert [result.status for result in second.results] == [FileStatus.SUCCESS_OCR]
    assert second.results[0].output == old_output
    assert sorted(path.name for path in output_dir.glob("*.pdf")) == ["native_OCR.pdf"]
    assert not (output_dir / "native_OCR_2.pdf").exists()
    assert not old_txt.exists()
    assert logs == ["取代舊輸出: native_OCR.pdf"]
    doc = pymupdf.open(old_output)
    try:
        assert "新版內容" in doc[0].get_text()
    finally:
        doc.close()


def test_run_batch_incremental_missing_output_reprocesses(work_folder: Path) -> None:
    _keep_only(work_folder, {"native.pdf"})
    cfg = make_cfg(naming=NamingConfig(enabled=False))

    first = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)
    assert first.results[0].output is not None
    first.results[0].output.unlink()
    second = run_batch(work_folder, cfg, FakeEngine(), None, _PROMPT)

    assert [result.status for result in second.results] == [FileStatus.SUCCESS_EXISTING_TEXT]
    assert second.csv_path is not None
    assert sorted(path.name for path in second.output_dir.glob("*.pdf")) == ["native_OCR.pdf"]


def test_run_batch_force_ignores_manifest_and_updates_manifest(work_folder: Path) -> None:
    _keep_only(work_folder, {"native.pdf"})
    cfg = make_cfg()

    run_batch(work_folder, cfg, FakeEngine(), StaticClient("first"), _PROMPT)
    second = run_batch(
        work_folder,
        cfg,
        FakeEngine(),
        StaticClient("second"),
        _PROMPT,
        force=True,
    )

    assert [result.status for result in second.results] == [FileStatus.SUCCESS_EXISTING_TEXT]
    assert second.results[0].output is not None
    assert second.results[0].output.name == "second.pdf"
    assert second.csv_path is not None
    assert _read_manifest_output(second.output_dir, "native.pdf") == "second.pdf"


def test_run_batch_incremental_encrypted_skips_from_manifest_without_output(
    work_folder: Path,
) -> None:
    _keep_only(work_folder, {"encrypted.pdf"})
    cfg = make_cfg()

    first = run_batch(work_folder, cfg, FakeEngine(), StaticClient("ignored"), _PROMPT)
    second = run_batch(work_folder, cfg, FakeEngine(), StaticClient("ignored"), _PROMPT)

    assert [result.status for result in first.results] == [FileStatus.SKIPPED_ENCRYPTED]
    assert first.csv_path is not None
    assert [result.status for result in second.results] == [FileStatus.SKIPPED_DONE]
    assert second.results[0].output is None
    assert second.csv_path is None


def test_run_batch_incremental_failed_files_retry_and_write_csv(work_folder: Path) -> None:
    _keep_only(work_folder, {"corrupt.pdf"})
    cfg = make_cfg()

    first = run_batch(work_folder, cfg, FakeEngine(), StaticClient("ignored"), _PROMPT)
    second = run_batch(work_folder, cfg, FakeEngine(), StaticClient("ignored"), _PROMPT)

    assert [result.status for result in first.results] == [FileStatus.FAILED]
    assert [result.status for result in second.results] == [FileStatus.FAILED]
    assert first.csv_path is not None
    assert second.csv_path is not None


def test_run_batch_workers_gt_one_delegates_to_parallel(tmp_path, monkeypatch) -> None:
    cfg = make_cfg(performance=PerformanceConfig(workers=2))
    expected = object()
    calls: list[tuple[object, ...]] = []

    def fake_run_batch_parallel(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr("pdf_ocrer.parallel.run_batch_parallel", fake_run_batch_parallel)
    engine = FakeEngine()
    client = StaticClient("unused")

    summary = run_batch(
        tmp_path,
        cfg,
        engine,
        client,
        _PROMPT,
        force=True,
    )

    assert summary is expected
    args, kwargs = calls[0]
    assert args[:5] == (tmp_path, cfg, engine, client, _PROMPT)
    assert kwargs["force"] is True


def test_text_for_naming_uses_page_texts_before_joining_page_breaks() -> None:
    cfg = make_cfg(naming=NamingConfig(max_pages_to_llm=2, max_chars_to_llm=1000))

    text = _text_for_naming(["page 1 line\n\npage 1 second paragraph", "page 2", "page 3"], cfg)

    assert text == "page 1 line\n\npage 1 second paragraph\n\npage 2"


def test_text_for_naming_strips_each_page_before_joining() -> None:
    cfg = make_cfg(naming=NamingConfig(max_pages_to_llm=2, max_chars_to_llm=1000))

    text = _text_for_naming(["page 1\n", "page 2\n", "page 3\n"], cfg)

    assert text == "page 1\n\npage 2"


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


def _touch_mtime(path: Path) -> None:
    next_ns = path.stat().st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(next_ns, next_ns))


def _read_manifest_output(output_dir: Path, rel: str) -> str | None:
    payload = json.loads((output_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    return payload["entries"][rel]["output"]


def _remove_pdf_ocrer_file_handlers() -> None:
    logger = logging.getLogger("pdf_ocrer")
    for handler in list(logger.handlers):
        if getattr(handler, "_pdf_ocrer_file_handler", False):
            logger.removeHandler(handler)
            handler.close()


def _flush_pdf_ocrer_file_handlers() -> None:
    for handler in logging.getLogger("pdf_ocrer").handlers:
        if getattr(handler, "_pdf_ocrer_file_handler", False):
            handler.flush()
