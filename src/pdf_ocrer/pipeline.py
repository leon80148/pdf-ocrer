from __future__ import annotations

import csv
import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from pdf_ocrer.config import AppConfig
from pdf_ocrer.llm_namer import resolve_collision, suggest_filename
from pdf_ocrer.llm_providers import LLMClient
from pdf_ocrer.ocr_engine import OcrEngineProtocol
from pdf_ocrer.pdf_processor import BatchCancelled, EncryptedPdfError, PdfResult, process_pdf


class FileStatus(str, Enum):
    SUCCESS_OCR = "OCR完成"
    SUCCESS_EXISTING_TEXT = "已有文字層-僅命名"
    NO_TEXT_FOUND = "無文字-原樣輸出"
    SKIPPED_ENCRYPTED = "加密-跳過"
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
) -> BatchSummary:
    folder = Path(folder)
    output_dir = folder / cfg.output.subdir_name
    output_dir.mkdir(exist_ok=True)

    files = _scan_pdfs(folder, output_dir)
    _logger.info("batch start folder=%s files=%d", folder, len(files))
    if not files:
        if log_cb is not None:
            log_cb("找不到 PDF 檔案。")
        summary = BatchSummary(results=[], csv_path=None, output_dir=output_dir, cancelled=False)
        _log_batch_end(summary)
        return summary

    csv_path = output_dir / f"{cfg.output.csv_prefix}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    results: list[FileResult] = []
    used_stems: set[str] = set()
    cancelled = False

    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(_CSV_HEADER)
        csv_file.flush()

        for file_i, src in enumerate(files, start=1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break

            try:
                processed = process_pdf(
                    src,
                    cfg,
                    engine,
                    page_cb=_page_progress(progress_cb, file_i, len(files), src.name),
                    cancel=cancel_event,
                )
                result = _finalize_processed_file(
                    src,
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
                    note=f"{type(exc).__name__}: {exc}",
                )

            _write_csv_row(writer, result)
            csv_file.flush()
            results.append(result)
            _log_file_result(result)
            if file_cb is not None:
                file_cb(result)

    summary = BatchSummary(results=results, csv_path=csv_path, output_dir=output_dir, cancelled=cancelled)
    _log_batch_end(summary)
    return summary


def _scan_pdfs(folder: Path, output_dir: Path) -> list[Path]:
    output_root = output_dir.resolve()
    files = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            continue
        try:
            if path.resolve().is_relative_to(output_root):
                continue
        except OSError:
            pass
        files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


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


def _finalize_processed_file(
    src: Path,
    processed: PdfResult,
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: set[str],
    log_cb: Callable[[str], None] | None,
) -> FileResult:
    doc_closed = False
    try:
        all_existing_text = all(report.action == "kept_existing" for report in processed.reports)
        stem, naming_source = _choose_stem(
            src,
            processed.page_texts,
            cfg,
            client,
            prompt_template,
            output_dir,
            used_stems,
            log_cb,
            keep_original=all_existing_text and not cfg.naming.rename_files_with_text,
        )
        output = output_dir / f"{stem}.pdf"

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


def _write_csv_row(writer: csv.writer, result: FileResult) -> None:
    writer.writerow(
        [
            result.source.name,
            "" if result.output is None else result.output.name,
            result.status.value,
            result.total_pages,
            result.ocr_pages,
            result.naming_source,
            result.note,
        ]
    )


def _log_file_result(result: FileResult) -> None:
    output_name = "" if result.output is None else result.output.name
    _logger.info(
        "file result source=%s status=%s output=%s note=%s",
        result.source.name,
        result.status.value,
        output_name,
        result.note,
    )


def _log_batch_end(summary: BatchSummary) -> None:
    failed = sum(result.status is FileStatus.FAILED for result in summary.results)
    skipped = sum(result.status is FileStatus.SKIPPED_ENCRYPTED for result in summary.results)
    _logger.info(
        "batch end results=%d failed=%d skipped=%d cancelled=%s csv=%s",
        len(summary.results),
        failed,
        skipped,
        summary.cancelled,
        summary.csv_path,
    )
