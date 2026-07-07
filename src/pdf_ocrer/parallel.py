from __future__ import annotations

import csv
import logging
import multiprocessing
import os
import queue
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Executor, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pdf_ocrer.config import AppConfig, resolve_cpu_threads, resolve_worker_count
from pdf_ocrer.llm_namer import resolve_collision
from pdf_ocrer.llm_providers import LLMClient
from pdf_ocrer.manifest import MANIFEST_NAME, FileIdentity, Manifest, ManifestEntry
from pdf_ocrer.ocr_engine import OcrEngineProtocol
from pdf_ocrer.pipeline import (
    BatchSummary,
    FileResult,
    FileStatus,
    ProgressCb,
    _choose_stem,
    _CSV_HEADER,
    _log_batch_end,
    _log_file_result,
    _log_incremental_skip,
    _relative_output_name,
    _remove_previous_output,
    _skipped_done_result,
    _write_csv_row,
    _write_txt_export,
)
from pdf_ocrer.scanning import ScanItem, scan_inputs
from pdf_ocrer.worker import WorkerOutcome, WorkerTask

_logger = logging.getLogger(__name__)
_WARMUP_MESSAGE = "正在載入 OCR 模型…（平行模式，{workers} 個工作行程）"
_CANCEL_FALLBACK_NOTE = "已取消-使用備用檔名"


@dataclass(frozen=True)
class _PendingFile:
    index: int
    source: Path
    rel: str
    identity: FileIdentity
    previous_entry: ManifestEntry | None
    task: WorkerTask


def run_batch_parallel(
    folder: Path,
    cfg: AppConfig,
    engine: OcrEngineProtocol | None,
    client: LLMClient | None,
    prompt_template: str,
    progress_cb: ProgressCb | None = None,
    log_cb: Callable[[str], None] | None = None,
    cancel_event: Any | None = None,
    file_cb: Callable[[FileResult], None] | None = None,
    force: bool = False,
    files: list[ScanItem] | None = None,
    *,
    executor_factory: Callable[[], Executor] | None = None,
    worker_fn: Callable[[WorkerTask], WorkerOutcome] | None = None,
    warmup_fn: Callable[[], Any] | None = None,
    events_queue: Any | None = None,
    worker_cancel: Any | None = None,
) -> BatchSummary:
    """Run a batch with per-file workers coordinated from this process.

    ``engine`` is accepted for signature compatibility with ``run_batch``. In
    parallel mode each worker process creates and owns its OCR engine.
    """

    _ = engine
    folder = Path(folder)
    output_dir = folder / cfg.output.subdir_name
    output_dir.mkdir(exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    manifest = Manifest.load(manifest_path)
    incremental = cfg.output.incremental and not force
    cpu_count = os.cpu_count()
    workers = resolve_worker_count(cfg.performance, cpu_count)
    items = files if files is not None else scan_inputs(folder, cfg.output.subdir_name, cfg.input)
    total_files = len(items)
    _logger.info("parallel batch start folder=%s files=%d workers=%d", folder, total_files, workers)
    _cleanup_stale_temps(output_dir)
    if not items:
        if log_cb is not None:
            log_cb("找不到 PDF 檔案。")
        summary = BatchSummary(results=[], csv_path=None, output_dir=output_dir, cancelled=False)
        _log_batch_end(summary)
        return summary

    csv_path: Path | None = None
    csv_file = None
    writer: csv.writer | None = None
    result_by_index: dict[int, FileResult] = {}
    pending_by_index: dict[int, _PendingFile] = {}
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

    def emit_result(
        index: int,
        result: FileResult,
        *,
        write_csv: bool,
        record_manifest: bool,
    ) -> None:
        if write_csv:
            csv_writer = ensure_writer()
            _write_csv_row(csv_writer, result, output_dir)
            csv_file.flush()

        result_by_index[index] = result
        _log_file_result(result, output_dir)
        if file_cb is not None:
            file_cb(result)

        if record_manifest and result.status is not FileStatus.FAILED:
            pending = pending_by_index.get(index)
            if pending is not None:
                manifest.record(
                    pending.rel,
                    pending.identity,
                    result.status.value,
                    None if result.output is None else _relative_output_name(result.output, output_dir),
                )
                manifest.save(manifest_path)

    try:
        for index, item in enumerate(items):
            if _event_is_set(cancel_event):
                cancelled = True
                break

            try:
                identity = FileIdentity.from_stat(item.src)
            except OSError as exc:
                emit_result(
                    index,
                    FileResult(
                        source=item.src,
                        output=None,
                        status=FileStatus.FAILED,
                        total_pages=0,
                        ocr_pages=0,
                        naming_source="none",
                        note=_exception_note(exc),
                        rel=item.rel,
                    ),
                    write_csv=True,
                    record_manifest=False,
                )
                continue

            previous_entry = manifest.get(item.rel) if cfg.output.incremental else None
            if incremental:
                entry = manifest.should_skip(item.rel, identity, output_dir)
                if entry is not None:
                    result = _skipped_done_result(item.src, item.rel, entry, output_dir)
                    result_by_index[index] = result
                    _log_incremental_skip(item.rel, log_cb)
                    _log_file_result(result, output_dir)
                    if file_cb is not None:
                        file_cb(result)
                    continue

            task = WorkerTask(
                index=index,
                source=str(item.src),
                rel=item.rel,
                temp_output=str(output_dir / f"~{uuid4().hex}.ocrtmp.pdf"),
                total_files=total_files,
            )
            pending_by_index[index] = _PendingFile(
                index=index,
                source=item.src,
                rel=item.rel,
                identity=identity,
                previous_entry=previous_entry,
                task=task,
            )

        if cancelled or not pending_by_index:
            summary = BatchSummary(
                results=_ordered_results(result_by_index),
                csv_path=csv_path,
                output_dir=output_dir,
                cancelled=cancelled,
            )
            _log_batch_end(summary)
            return summary

        if events_queue is None or worker_cancel is None or executor_factory is None:
            mp_context = multiprocessing.get_context("spawn")
            if events_queue is None:
                events_queue = mp_context.Queue()
            if worker_cancel is None:
                worker_cancel = mp_context.Event()
            if executor_factory is None:
                executor_factory = _process_pool_factory(cfg, workers, cpu_count, worker_cancel, events_queue)

        if worker_fn is None or warmup_fn is None:
            from pdf_ocrer import worker as worker_module

            if worker_fn is None:
                worker_fn = worker_module.process_file_task
            if warmup_fn is None:
                warmup_fn = worker_module.warmup

        executor = executor_factory()
        shutdown_called = False
        try:
            _log_message(_WARMUP_MESSAGE.format(workers=workers), log_cb)
            warmup_future = executor.submit(warmup_fn)
            try:
                warmup_future.result()
            except Exception as exc:
                _logger.exception("parallel OCR warmup failed")
                for index in sorted(pending_by_index):
                    pending = pending_by_index[index]
                    emit_result(
                        index,
                        _failed_result(pending, _exception_note(exc)),
                        write_csv=True,
                        record_manifest=False,
                    )
                summary = BatchSummary(
                    results=_ordered_results(result_by_index),
                    csv_path=csv_path,
                    output_dir=output_dir,
                    cancelled=False,
                )
                _log_batch_end(summary)
                return summary

            if _event_is_set(cancel_event):
                cancelled = True
                _set_event(worker_cancel)
                summary = BatchSummary(
                    results=_ordered_results(result_by_index),
                    csv_path=csv_path,
                    output_dir=output_dir,
                    cancelled=True,
                )
                _log_batch_end(summary)
                return summary

            future_to_pending: dict[Future[Any], _PendingFile] = {}
            try:
                for pending in pending_by_index.values():
                    future_to_pending[executor.submit(worker_fn, pending.task)] = pending
            except BrokenProcessPool as exc:
                _logger.exception("parallel worker pool broke during submit")
                for pending in pending_by_index.values():
                    if pending.index not in result_by_index:
                        emit_result(
                            pending.index,
                            _failed_result(pending, _exception_note(exc)),
                            write_csv=True,
                            record_manifest=False,
                        )
                summary = BatchSummary(
                    results=_ordered_results(result_by_index),
                    csv_path=csv_path,
                    output_dir=output_dir,
                    cancelled=False,
                )
                _log_batch_end(summary)
                return summary
            outcome_by_index: dict[int, WorkerOutcome] = {}
            next_finalize = 0

            while future_to_pending:
                _drain_events(
                    events_queue,
                    progress_cb,
                    log_cb,
                    total_files,
                    files_done=len(result_by_index),
                    wait_seconds=0.1,
                )
                broken_exc = _collect_done_futures(future_to_pending, outcome_by_index)
                if broken_exc is not None:
                    _logger.error("parallel worker pool broke: %s", _exception_note(broken_exc))
                    for pending in future_to_pending.values():
                        outcome_by_index[pending.index] = _exception_outcome(pending, broken_exc)
                    future_to_pending.clear()
                    next_finalize = _finalize_ready(
                        next_finalize,
                        total_files,
                        outcome_by_index,
                        result_by_index,
                        pending_by_index,
                        cfg,
                        client,
                        prompt_template,
                        output_dir,
                        used_stems,
                        log_cb,
                        emit_result,
                        cancel_fallback=False,
                    )
                    break

                if _event_is_set(cancel_event):
                    cancelled = True
                    _set_event(worker_cancel)
                    executor.shutdown(wait=False, cancel_futures=True)
                    _wait_for_cancelled_futures(future_to_pending)
                    broken_exc = _collect_done_futures(future_to_pending, outcome_by_index)
                    if broken_exc is not None:
                        _logger.error("parallel worker pool broke: %s", _exception_note(broken_exc))
                        for pending in future_to_pending.values():
                            outcome_by_index[pending.index] = _exception_outcome(pending, broken_exc)
                        future_to_pending.clear()
                    _finalize_cancelled_outcomes(
                        outcome_by_index,
                        result_by_index,
                        pending_by_index,
                        cfg,
                        client,
                        prompt_template,
                        output_dir,
                        used_stems,
                        log_cb,
                        emit_result,
                    )
                    _cleanup_unfinished_temps(future_to_pending.values(), outcome_by_index)
                    future_to_pending.clear()
                    executor.shutdown(wait=True)
                    shutdown_called = True
                    break

                next_finalize = _finalize_ready(
                    next_finalize,
                    total_files,
                    outcome_by_index,
                    result_by_index,
                    pending_by_index,
                    cfg,
                    client,
                    prompt_template,
                    output_dir,
                    used_stems,
                    log_cb,
                    emit_result,
                    cancel_fallback=False,
                )

            _drain_events_until_quiet(
                events_queue,
                progress_cb,
                log_cb,
                total_files,
                files_done=len(result_by_index),
            )
            if not cancelled:
                _finalize_ready(
                    next_finalize,
                    total_files,
                    outcome_by_index,
                    result_by_index,
                    pending_by_index,
                    cfg,
                    client,
                    prompt_template,
                    output_dir,
                    used_stems,
                    log_cb,
                    emit_result,
                    cancel_fallback=False,
                )
        finally:
            if not shutdown_called:
                executor.shutdown(wait=True)

        summary = BatchSummary(
            results=_ordered_results(result_by_index),
            csv_path=csv_path,
            output_dir=output_dir,
            cancelled=cancelled,
        )
        _log_batch_end(summary)
        return summary
    finally:
        if csv_file is not None:
            csv_file.close()


def _process_pool_factory(
    cfg: AppConfig,
    workers: int,
    cpu_count: int | None,
    worker_cancel: Any,
    events_queue: Any,
) -> Callable[[], Executor]:
    from pdf_ocrer import worker as worker_module

    ocr_cfg = replace(
        cfg.ocr,
        cpu_threads=resolve_cpu_threads(cfg.ocr, workers, cpu_count),
    )
    mp_context = multiprocessing.get_context("spawn")

    def factory() -> Executor:
        return ProcessPoolExecutor(
            max_workers=workers,
            mp_context=mp_context,
            initializer=worker_module.init_worker,
            initargs=(ocr_cfg, cfg.debug, worker_cancel, events_queue),
        )

    return factory


def _collect_done_futures(
    future_to_pending: dict[Future[Any], _PendingFile],
    outcome_by_index: dict[int, WorkerOutcome],
) -> BrokenProcessPool | None:
    broken_exc: BrokenProcessPool | None = None
    for future, pending in list(future_to_pending.items()):
        if not future.done():
            continue

        try:
            outcome = future.result()
        except BrokenProcessPool as exc:
            broken_exc = exc if broken_exc is None else broken_exc
            outcome = _exception_outcome(pending, exc)
        except CancelledError:
            outcome = _cancelled_outcome(pending)
        except Exception as exc:
            outcome = _exception_outcome(pending, exc)
        outcome_by_index[outcome.index] = outcome
        del future_to_pending[future]

    return broken_exc


def _wait_for_cancelled_futures(future_to_pending: dict[Future[Any], _PendingFile]) -> None:
    if not future_to_pending:
        return

    _done, not_done = wait(list(future_to_pending), timeout=60)
    if not_done:
        _logger.warning(
            "parallel cancel timed out waiting for workers count=%d timeout_seconds=60",
            len(not_done),
        )


def _finalize_ready(
    next_index: int,
    total_files: int,
    outcome_by_index: dict[int, WorkerOutcome],
    result_by_index: dict[int, FileResult],
    pending_by_index: dict[int, _PendingFile],
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: dict[str, set[str]],
    log_cb: Callable[[str], None] | None,
    emit_result: Callable[..., None],
    *,
    cancel_fallback: bool,
) -> int:
    while next_index < total_files:
        if next_index in result_by_index:
            next_index += 1
            continue

        outcome = outcome_by_index.get(next_index)
        if outcome is None:
            break

        result = _finalize_outcome(
            outcome,
            pending_by_index[next_index],
            cfg,
            client,
            prompt_template,
            output_dir,
            used_stems,
            log_cb,
            cancel_fallback=cancel_fallback,
        )
        if result is not None:
            emit_result(
                next_index,
                result,
                write_csv=True,
                record_manifest=result.status is not FileStatus.FAILED,
            )
        next_index += 1

    return next_index


def _finalize_cancelled_outcomes(
    outcome_by_index: dict[int, WorkerOutcome],
    result_by_index: dict[int, FileResult],
    pending_by_index: dict[int, _PendingFile],
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: dict[str, set[str]],
    log_cb: Callable[[str], None] | None,
    emit_result: Callable[..., None],
) -> None:
    _ = client
    for index in sorted(outcome_by_index):
        if index in result_by_index:
            continue
        outcome = outcome_by_index[index]
        result = _finalize_outcome(
            outcome,
            pending_by_index[index],
            cfg,
            None,
            prompt_template,
            output_dir,
            used_stems,
            log_cb,
            cancel_fallback=True,
        )
        if result is not None:
            emit_result(
                index,
                result,
                write_csv=True,
                record_manifest=result.status is not FileStatus.FAILED,
            )


def _finalize_outcome(
    outcome: WorkerOutcome,
    pending: _PendingFile,
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: dict[str, set[str]],
    log_cb: Callable[[str], None] | None,
    *,
    cancel_fallback: bool,
) -> FileResult | None:
    if outcome.kind == "ok":
        return _finalize_ok_outcome(
            outcome,
            pending,
            cfg,
            client,
            prompt_template,
            output_dir,
            used_stems,
            log_cb,
            cancel_fallback=cancel_fallback,
        )
    if outcome.kind == "encrypted":
        return FileResult(
            source=pending.source,
            output=None,
            status=FileStatus.SKIPPED_ENCRYPTED,
            total_pages=0,
            ocr_pages=0,
            naming_source="none",
            note=outcome.note,
            rel=pending.rel,
        )
    if outcome.kind == "failed":
        return _failed_result(pending, outcome.note)
    if outcome.kind == "cancelled":
        if outcome.temp_output is not None:
            _delete_temp(Path(outcome.temp_output))
        return None

    return _failed_result(pending, f"未知 worker 結果: {outcome.kind}")


def _finalize_ok_outcome(
    outcome: WorkerOutcome,
    pending: _PendingFile,
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    output_dir: Path,
    used_stems: dict[str, set[str]],
    log_cb: Callable[[str], None] | None,
    *,
    cancel_fallback: bool,
) -> FileResult:
    if outcome.temp_output is None:
        return _failed_result(pending, "worker returned ok without temp output")

    temp_output = Path(outcome.temp_output)
    page_texts = list(outcome.page_texts)
    try:
        _remove_previous_output(output_dir, pending.previous_entry, log_cb)
        output_parent = output_dir / PurePosixPath(pending.rel).parent
        output_parent.mkdir(parents=True, exist_ok=True)
        parent_key = PurePosixPath(pending.rel).parent.as_posix()
        parent_used_stems = used_stems.setdefault(parent_key, set())
        if cancel_fallback:
            stem = resolve_collision(
                output_parent,
                f"{pending.source.stem}{cfg.naming.fallback_suffix}",
                parent_used_stems,
            )
            naming_source = "fallback"
            note = _CANCEL_FALLBACK_NOTE
        else:
            stem, naming_source = _choose_stem(
                pending.source,
                page_texts,
                cfg,
                client,
                prompt_template,
                output_parent,
                parent_used_stems,
                log_cb,
                keep_original=outcome.all_existing_text and not cfg.naming.rename_files_with_text,
            )
            note = ""

        output = output_parent / f"{stem}.pdf"
        temp_output.rename(output)
        _write_txt_export(output, page_texts, cfg)
        status = _success_status(outcome)
        return FileResult(
            source=pending.source,
            output=output,
            status=status,
            total_pages=outcome.total_pages,
            ocr_pages=outcome.ocr_pages,
            naming_source=naming_source,
            note=note,
            rel=pending.rel,
        )
    except Exception as exc:
        _delete_temp(temp_output)
        return _failed_result(pending, _exception_note(exc))


def _success_status(outcome: WorkerOutcome) -> FileStatus:
    if outcome.all_existing_text:
        return FileStatus.SUCCESS_EXISTING_TEXT
    if outcome.ocr_pages > 0:
        return FileStatus.SUCCESS_OCR
    return FileStatus.NO_TEXT_FOUND


def _failed_result(pending: _PendingFile, note: str) -> FileResult:
    return FileResult(
        source=pending.source,
        output=None,
        status=FileStatus.FAILED,
        total_pages=0,
        ocr_pages=0,
        naming_source="none",
        note=note,
        rel=pending.rel,
    )


def _exception_outcome(pending: _PendingFile, exc: BaseException) -> WorkerOutcome:
    _delete_temp(Path(pending.task.temp_output))
    return WorkerOutcome(
        index=pending.index,
        source=str(pending.source),
        rel=pending.rel,
        kind="failed",
        temp_output=None,
        page_texts=(),
        total_pages=0,
        ocr_pages=0,
        all_existing_text=False,
        note=_exception_note(exc),
    )


def _cancelled_outcome(pending: _PendingFile) -> WorkerOutcome:
    _delete_temp(Path(pending.task.temp_output))
    return WorkerOutcome(
        index=pending.index,
        source=str(pending.source),
        rel=pending.rel,
        kind="cancelled",
        temp_output=None,
        page_texts=(),
        total_pages=0,
        ocr_pages=0,
        all_existing_text=False,
        note="",
    )


def _exception_note(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _drain_events(
    events_queue: Any,
    progress_cb: ProgressCb | None,
    log_cb: Callable[[str], None] | None,
    total_files: int,
    *,
    files_done: int,
    wait_seconds: float,
) -> None:
    timeout = wait_seconds
    while True:
        try:
            event = events_queue.get(timeout=timeout)
        except queue.Empty:
            return
        except (EOFError, OSError):
            return

        timeout = 0
        _handle_event(event, progress_cb, log_cb, total_files, files_done=files_done)


def _drain_events_until_quiet(
    events_queue: Any,
    progress_cb: ProgressCb | None,
    log_cb: Callable[[str], None] | None,
    total_files: int,
    *,
    files_done: int,
    wait_seconds: float = 0.05,
    max_seconds: float = 0.5,
    empty_limit: int = 2,
) -> None:
    started = time.monotonic()
    empty_count = 0
    while empty_count < empty_limit and time.monotonic() - started < max_seconds:
        remaining = max_seconds - (time.monotonic() - started)
        timeout = min(wait_seconds, max(0.0, remaining))
        try:
            event = events_queue.get(timeout=timeout)
        except queue.Empty:
            empty_count += 1
            continue
        except (EOFError, OSError):
            return

        empty_count = 0
        _handle_event(event, progress_cb, log_cb, total_files, files_done=files_done)


def _handle_event(
    event: Any,
    progress_cb: ProgressCb | None,
    log_cb: Callable[[str], None] | None,
    total_files: int,
    *,
    files_done: int,
) -> None:
    if not isinstance(event, tuple) or not event:
        return

    if event[0] == "page" and len(event) == 5:
        if progress_cb is not None:
            _tag, _index, rel, page_i, page_n = event
            file_i = min(files_done + 1, total_files)
            progress_cb(file_i, total_files, int(page_i), int(page_n), str(rel))
        return

    if event[0] == "log" and len(event) >= 2:
        _log_message(str(event[1]), log_cb)


def _log_message(message: str, log_cb: Callable[[str], None] | None) -> None:
    if log_cb is not None:
        log_cb(message)
    _logger.info("%s", message)


def _cleanup_stale_temps(output_dir: Path) -> None:
    removed = 0
    for path in output_dir.rglob("~*.ocrtmp.pdf"):
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            _logger.warning("stale temp cleanup failed path=%s error=%s", path, exc)
    if removed:
        _logger.warning("removed stale parallel temp files count=%d output_dir=%s", removed, output_dir)


def _cleanup_unfinished_temps(
    pending_files: Any,
    outcome_by_index: dict[int, WorkerOutcome],
) -> None:
    for pending in pending_files:
        if pending.index in outcome_by_index:
            continue
        _delete_temp(Path(pending.task.temp_output))


def _delete_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _ordered_results(result_by_index: dict[int, FileResult]) -> list[FileResult]:
    return [result_by_index[index] for index in sorted(result_by_index)]


def _event_is_set(event: Any | None) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except AttributeError:
        return False


def _set_event(event: Any | None) -> None:
    if event is None:
        return
    try:
        event.set()
    except AttributeError:
        pass
