from __future__ import annotations

import csv
import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath

from pdf_ocrer.config import AppConfig, resolve_worker_count
from pdf_ocrer.llm_namer import resolve_collision, suggest_filename
from pdf_ocrer.llm_providers import LLMClient
from pdf_ocrer.manifest import MANIFEST_NAME, FileIdentity, Manifest, ManifestEntry
from pdf_ocrer.ocr_engine import OcrEngineProtocol
from pdf_ocrer.pdf_processor import BatchCancelled, EncryptedPdfError, PdfResult, process_pdf
from pdf_ocrer.scanning import ScanItem, scan_inputs


class FileStatus(str, Enum):
    SUCCESS_OCR = "OCR完成"
    SUCCESS_EXISTING_TEXT = "已有文字層-僅命名"
    NO_TEXT_FOUND = "無文字-原樣輸出"
    SKIPPED_ENCRYPTED = "加密-跳過"
    SKIPPED_DONE = "已處理-跳過"
    FAILED = "失敗"


@dataclass
class FileResult:
    source: Path
    output: Path | None
    status: FileStatus
    total_pages: int
    ocr_pages: int
    naming_source: str
    note: str
    rel: str = ""


@dataclass
class BatchSummary:
    results: list[FileResult]
    csv_path: Path | None
    output_dir: Path
    cancelled: bool


ProgressCb = Callable[[int, int, int, int, str], None]

_CSV_HEADER = ["原檔名", "新檔名", "狀態", "總頁數", "OCR頁數", "命名來源", "備註"]
_logger = logging.getLogger(__name__)


def run_batch(
    folder: Path,
    cfg: AppConfig,
    engine: OcrEngineProtocol,
    client: LLMClient | None,
    prompt_template: str,
    progress_cb: ProgressCb | None = None,
    log_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    file_cb: Callable[[FileResult], None] | None = None,
    force: bool = False,
    files: list[ScanItem] | None = None,
) -> BatchSummary:
    workers = resolve_worker_count(cfg.performance, os.cpu_count())
    if workers > 1:
        from pdf_ocrer.parallel import run_batch_parallel

        return run_batch_parallel(
            folder,
            cfg,
            engine,
            client,
            prompt_template,
            progress_cb=progress_cb,
            log_cb=log_cb,
            cancel_event=cancel_event,
            file_cb=file_cb,
            force=force,
            files=files,
        )

    folder = Path(folder)
    output_dir = folder / cfg.output.subdir_name
    output_dir.mkdir(exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    manifest = Manifest.load(manifest_path)
    incremental = cfg.output.incremental and not force

    items = files if files is not None else scan_inputs(folder, cfg.output.subdir_name, cfg.input)
    _logger.info("batch start folder=%s files=%d", folder, len(items))
    if not items:
        if log_cb is not None:
            log_cb("找不到 PDF 檔案。")
        summary = BatchSummary(results=[], csv_path=None, output_dir=output_dir, cancelled=False)
        _log_batch_end(summary)
        return summary

    csv_path: Path | None = None
    csv_file = None
    writer: csv.writer | None = None
    results: list[FileResult] = []
    used_stems: dict[str, set[str]] = {}
    cancelled = False

    def ensure_writer() -> csv.writer:
        nonlocal csv_path, csv_file, writer
        if writer is None:
            csv_path = output_dir / f"{cfg.output.csv_prefix}_{datetime.now():%Y%m%d_%H%M%S}.csv"
            csv_file = csv_path.open("w", encoding="utf-8-sig", newline="")
            writer = csv.writer(csv_file)
            writer.writerow(_CSV_HEADER)
            csv_file.flush()
        return writer

    try:
        for file_i, item in enumerate(items, start=1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break

            src = item.src
            try:
                identity = FileIdentity.from_stat(src)
            except OSError as exc:
                result = FileResult(
                    source=src,
                    output=None,
                    status=FileStatus.FAILED,
                    total_pages=0,
                    ocr_pages=0,
                    naming_source="none",
                    note=_exception_note(exc),
                    rel=item.rel,
                )
                csv_writer = ensure_writer()
                _write_csv_row(csv_writer, result, output_dir)
                csv_file.flush()
                results.append(result)
                _log_file_result(result, output_dir)
                if file_cb is not None:
                    file_cb(result)
                continue

            previous_entry = manifest.get(item.rel) if cfg.output.incremental else None
            if incremental:
                entry = manifest.should_skip(item.rel, identity, output_dir)
                if entry is not None:
                    result = _skipped_done_result(src, item.rel, entry, output_dir)
                    results.append(result)
                    _log_incremental_skip(item.rel, log_cb)
                    _log_file_result(result, output_dir)
                    if file_cb is not None:
                        file_cb(result)
                    continue

            try:
                processed = process_pdf(
                    src,
                    cfg,
                    engine,
                    page_cb=_page_progress(progress_cb, file_i, len(items), item.rel),
                    cancel=cancel_event,
                )
                _remove_previous_output(output_dir, previous_entry, log_cb)
                result = _finalize_processed_file(
                    src,
                    item.rel,
                    processed,
                    cfg,
                    client,
                    prompt_template,
                    output_dir,
                    used_stems,
                    log_cb,
                )
            except EncryptedPdfError as exc:
                result = FileResult(
                    source=src,
                    output=None,
                    status=FileStatus.SKIPPED_ENCRYPTED,
                    total_pages=0,
                    ocr_pages=0,
                    naming_source="none",
                    note=str(exc),
                    rel=item.rel,
                )
            except BatchCancelled:
                cancelled = True
                break
            except Exception as exc:
                result = FileResult(
                    source=src,
                    output=None,
                    status=FileStatus.FAILED,
                    total_pages=0,
                    ocr_pages=0,
                    naming_source="none",
                    note=_exception_note(exc),
                    rel=item.rel,
                )

            csv_writer = ensure_writer()
            _write_csv_row(csv_writer, result, output_dir)
            csv_file.flush()
            results.append(result)
            _log_file_result(result, output_dir)
            if file_cb is not None:
                file_cb(result)
            if result.status is not FileStatus.FAILED:
                manifest.record(
                    item.rel,
                    identity,
                    result.status.value,
                    None if result.output is None else _relative_output_name(result.output, output_dir),
                )
                manifest.save(manifest_path)
    finally:
        if csv_file is not None:
            csv_file.close()

    summary = BatchSummary(results=results, csv_path=csv_path, output_dir=output_dir, cancelled=cancelled)
    _log_batch_end(summary)
    return summary


def _page_progress(
    progress_cb: ProgressCb | None,
    file_i: int,
    file_n: int,
    filename: str,
) -> Callable[[int, int], None] | None:
    if progress_cb is None:
        return None

    def callback(page_i: int, page_n: int) -> None:
        progress_cb(file_i, file_n, page_i + 1, page_n, filename)

    return callback


def _skipped_done_result(
    src: Path,
    rel: str,
    entry: ManifestEntry,
    output_dir: Path,
) -> FileResult:
    return FileResult(
        source=src,
        output=None if entry.output is None else output_dir / entry.output,
        status=FileStatus.SKIPPED_DONE,
        total_pages=0,
        ocr_pages=0,
        naming_source="manifest",
        note="",
        rel=rel,
    )


def _log_incremental_skip(rel: str, log_cb: Callable[[str], None] | None) -> None:
    message = f"已處理-跳過（先前已完成）: {rel}"
    if log_cb is not None:
        log_cb(message)
    _logger.info("%s", message)


def _remove_previous_output(
    output_dir: Path,
    entry: ManifestEntry | None,
    log_cb: Callable[[str], None] | None,
) -> None:
    if entry is None or entry.output is None:
        return

    output = output_dir / entry.output
    output_was_present = output.exists()
    output_removed = False
    txt_removed = False
    for stale_path in (output, output.with_suffix(".txt")):
        try:
            if stale_path.exists():
                stale_path.unlink(missing_ok=True)
                if stale_path == output:
                    output_removed = True
                else:
                    txt_removed = True
        except OSError as exc:
            _logger.warning("replace old output cleanup failed path=%s error=%s", stale_path, exc)

    if output_was_present and not output_removed:
        return
    if not output_removed and not txt_removed:
        return

    message = f"取代舊輸出: {entry.output}"
    if log_cb is not None:
        log_cb(message)
    _logger.info("%s", message)


def _finalize_processed_file(
    src: Path,
    rel: str,
    processed: PdfResult,
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: dict[str, set[str]],
    log_cb: Callable[[str], None] | None,
) -> FileResult:
    doc_closed = False
    try:
        all_existing_text = all(report.action == "kept_existing" for report in processed.reports)
        output_parent = output_dir / PurePosixPath(rel).parent
        output_parent.mkdir(parents=True, exist_ok=True)
        parent_key = PurePosixPath(rel).parent.as_posix()
        parent_used_stems = used_stems.setdefault(parent_key, set())
        stem, naming_source = _choose_stem(
            src,
            processed.page_texts,
            cfg,
            client,
            prompt_template,
            output_parent,
            parent_used_stems,
            log_cb,
            keep_original=all_existing_text and not cfg.naming.rename_files_with_text,
        )
        output = output_parent / f"{stem}.pdf"

        if all_existing_text:
            processed.doc.close()
            doc_closed = True
            shutil.copy2(src, output)
            status = FileStatus.SUCCESS_EXISTING_TEXT
        else:
            processed.doc.subset_fonts()
            processed.doc.save(output, garbage=3, deflate=True)
            processed.doc.close()
            doc_closed = True
            status = (
                FileStatus.SUCCESS_OCR
                if any(report.action == "ocr" for report in processed.reports)
                else FileStatus.NO_TEXT_FOUND
            )

        _write_txt_export(output, processed.page_texts, cfg)

        return FileResult(
            source=src,
            output=output,
            status=status,
            total_pages=processed.total_pages,
            ocr_pages=processed.ocr_pages,
            naming_source=naming_source,
            note="",
            rel=rel,
        )
    finally:
        if not doc_closed:
            processed.doc.close()


def _choose_stem(
    src: Path,
    page_texts: list[str],
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: set[str],
    log_cb: Callable[[str], None] | None,
    *,
    keep_original: bool,
) -> tuple[str, str]:
    if keep_original:
        stem = src.stem
        naming_source = "none"
    elif cfg.naming.enabled:
        naming_text = _text_for_naming(page_texts, cfg)
        if naming_text.strip():
            stem, naming_source = suggest_filename(
                naming_text,
                src.stem,
                cfg,
                client,
                prompt_template,
                log=log_cb,
            )
        else:
            stem = f"{src.stem}{cfg.naming.fallback_suffix}"
            naming_source = "fallback"
    else:
        stem = f"{src.stem}{cfg.naming.fallback_suffix}"
        naming_source = "none"

    return resolve_collision(output_dir, stem, used_stems), naming_source


def _text_for_naming(page_texts: list[str], cfg: AppConfig) -> str:
    pages = page_texts[: cfg.naming.max_pages_to_llm]
    return "\n\n".join(page.strip() for page in pages)[: cfg.naming.max_chars_to_llm]


def _write_txt_export(output: Path, page_texts: list[str], cfg: AppConfig) -> None:
    if not cfg.output.export_txt or not any(page_text.strip() for page_text in page_texts):
        return

    blocks = [
        f"--- 第 {index} 頁 ---\n{page_text.strip()}"
        for index, page_text in enumerate(page_texts, start=1)
    ]
    try:
        output.with_suffix(".txt").write_text(
            "\n\n".join(blocks) + "\n",
            encoding="utf-8-sig",
            errors="replace",
        )
    except OSError as exc:
        _logger.warning("txt export failed output=%s error=%s", output, exc)


def _write_csv_row(writer: csv.writer, result: FileResult, output_dir: Path) -> None:
    writer.writerow(
        [
            result.rel or result.source.name,
            "" if result.output is None else _relative_output_name(result.output, output_dir),
            result.status.value,
            result.total_pages,
            result.ocr_pages,
            result.naming_source,
            result.note,
        ]
    )


def _log_file_result(result: FileResult, output_dir: Path) -> None:
    source_name = result.rel or result.source.name
    output_name = "" if result.output is None else _relative_output_name(result.output, output_dir)
    _logger.info(
        "file result source=%s status=%s output=%s note=%s",
        source_name,
        result.status.value,
        output_name,
        result.note,
    )


def _relative_output_name(output: Path, output_dir: Path) -> str:
    try:
        return output.relative_to(output_dir).as_posix()
    except ValueError:
        return output.name


def _exception_note(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _log_batch_end(summary: BatchSummary) -> None:
    failed = sum(result.status is FileStatus.FAILED for result in summary.results)
    skipped = sum(result.status is FileStatus.SKIPPED_ENCRYPTED for result in summary.results)
    skipped_done = sum(result.status is FileStatus.SKIPPED_DONE for result in summary.results)
    _logger.info(
        "batch end results=%d failed=%d skipped=%d skipped_done=%d cancelled=%s csv=%s",
        len(summary.results),
        failed,
        skipped,
        skipped_done,
        summary.cancelled,
        summary.csv_path,
    )
