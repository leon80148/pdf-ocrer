"""Spawn-safe worker boundary for per-file PDF processing.

This module is imported inside Windows ``spawn`` worker processes, so every
callable and data object used by ``ProcessPoolExecutor`` lives at module scope.
The pickle boundary is intentionally narrow: workers receive ``WorkerTask`` and
return ``WorkerOutcome`` only. PyMuPDF documents, rendered images, and OCR engine
instances never cross process boundaries.

Worker events are also plain tuples. ``("page", index, rel, page_i, page_n)``
drives progress in the coordinator. ``("log", msg)`` is reserved for the
coordinator-side QueueHandler/log-callback bridge.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from pdf_ocrer.config import AppConfig, DebugConfig, LlmConfig, NamingConfig, OcrConfig, OutputConfig
from pdf_ocrer.ocr_engine import OcrEngineProtocol, create_engine
from pdf_ocrer.pdf_processor import BatchCancelled, EncryptedPdfError, process_pdf


@dataclass(frozen=True)
class WorkerTask:
    index: int
    source: str
    rel: str
    temp_output: str
    total_files: int


@dataclass(frozen=True)
class WorkerOutcome:
    index: int
    source: str
    rel: str
    kind: str
    temp_output: str | None
    page_texts: tuple[str, ...]
    total_pages: int
    ocr_pages: int
    all_existing_text: bool
    note: str


_CANCEL: Any | None = None
_EVENTS: Any | None = None
_OCR_CFG = OcrConfig()
_DEBUG_CFG = DebugConfig()
_CFG = AppConfig(_OCR_CFG, OutputConfig(), NamingConfig(), LlmConfig(), _DEBUG_CFG)
_ENGINE: OcrEngineProtocol | None = None
_LOG_HANDLER: logging.Handler | None = None
_engine_factory: Callable[..., OcrEngineProtocol] = create_engine


class _EventQueueHandler(logging.Handler):
    def __init__(self, events: Any) -> None:
        super().__init__(level=logging.INFO)
        self._events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._events.put(("log", record.getMessage()))
        except Exception:
            pass


def init_worker(ocr_cfg: OcrConfig, debug_cfg: DebugConfig, cancel: Any, events: Any) -> None:
    global _CANCEL, _EVENTS, _OCR_CFG, _DEBUG_CFG, _CFG, _ENGINE

    _CANCEL = cancel
    _EVENTS = events
    _OCR_CFG = ocr_cfg
    _DEBUG_CFG = debug_cfg
    _CFG = AppConfig(ocr_cfg, OutputConfig(), NamingConfig(), LlmConfig(), debug_cfg)
    _ENGINE = None
    _configure_event_logging(events)


def _configure_event_logging(events: Any) -> None:
    global _LOG_HANDLER

    logger = logging.getLogger("pdf_ocrer")
    if _LOG_HANDLER is not None:
        logger.removeHandler(_LOG_HANDLER)
        _LOG_HANDLER.close()
        _LOG_HANDLER = None

    if events is None:
        return

    handler = _EventQueueHandler(events)
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    _LOG_HANDLER = handler


def warmup() -> str:
    engine = _get_engine()
    engine.recognize(np.full((8, 8, 3), 255, dtype=np.uint8))
    return f"{type(engine).__module__}.{type(engine).__qualname__}"


def process_file_task(task: WorkerTask) -> WorkerOutcome:
    source = Path(task.source)
    temp_output = Path(task.temp_output)

    try:
        engine = _get_engine()
        processed = process_pdf(
            source,
            _CFG,
            engine,
            page_cb=_page_event_callback(task),
            cancel=_CANCEL,
        )
        doc_closed = False
        try:
            all_existing_text = all(report.action == "kept_existing" for report in processed.reports)
            temp_output.parent.mkdir(parents=True, exist_ok=True)

            if all_existing_text:
                processed.doc.close()
                doc_closed = True
                shutil.copy2(source, temp_output)
            else:
                processed.doc.subset_fonts()
                processed.doc.save(temp_output, garbage=3, deflate=True)
                processed.doc.close()
                doc_closed = True

            return WorkerOutcome(
                index=task.index,
                source=task.source,
                rel=task.rel,
                kind="ok",
                temp_output=str(temp_output),
                page_texts=tuple(processed.page_texts),
                total_pages=processed.total_pages,
                ocr_pages=processed.ocr_pages,
                all_existing_text=all_existing_text,
                note="",
            )
        finally:
            if not doc_closed:
                processed.doc.close()
    except EncryptedPdfError as exc:
        _delete_temp_output(temp_output)
        return _error_outcome(task, "encrypted", str(exc))
    except BatchCancelled:
        _delete_temp_output(temp_output)
        return _error_outcome(task, "cancelled", "")
    except Exception as exc:
        _delete_temp_output(temp_output)
        return _error_outcome(task, "failed", f"{type(exc).__name__}: {exc}")


def _get_engine() -> OcrEngineProtocol:
    global _ENGINE

    if _ENGINE is None:
        _ENGINE = _create_engine()
    return _ENGINE


def _create_engine() -> OcrEngineProtocol:
    try:
        return _engine_factory(_OCR_CFG, _log_event)
    except TypeError as two_arg_error:
        try:
            return _engine_factory(_OCR_CFG)
        except TypeError:
            try:
                return _engine_factory()
            except TypeError:
                raise two_arg_error


def _page_event_callback(task: WorkerTask) -> Callable[[int, int], None]:
    def callback(page_index: int, total_pages: int) -> None:
        _put_event(("page", task.index, task.rel, page_index + 1, total_pages))

    return callback


def _log_event(message: str) -> None:
    _put_event(("log", message))


def _put_event(event: tuple[Any, ...]) -> None:
    if _EVENTS is None:
        return

    try:
        _EVENTS.put(event)
    except Exception:
        pass


def _delete_temp_output(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _error_outcome(task: WorkerTask, kind: str, note: str) -> WorkerOutcome:
    return WorkerOutcome(
        index=task.index,
        source=task.source,
        rel=task.rel,
        kind=kind,
        temp_output=None,
        page_texts=(),
        total_pages=0,
        ocr_pages=0,
        all_existing_text=False,
        note=note,
    )
