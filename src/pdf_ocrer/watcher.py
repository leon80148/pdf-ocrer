from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pdf_ocrer import scanning
from pdf_ocrer.config import AppConfig, resolve_worker_count
from pdf_ocrer.pipeline import BatchSummary, FileResult, FileStatus, ProgressCb, run_batch
from pdf_ocrer.scanning import ScanItem

_logger = logging.getLogger(__name__)

_Snapshot = tuple[int, int]


@dataclass(frozen=True)
class WatchCycle:
    index: int
    ready: list[ScanItem]


@dataclass(frozen=True)
class WatchSummary:
    cycles: int
    total_processed: int
    results: list[FileResult]


class FolderWatcher:
    def __init__(self, folder: Path, cfg: AppConfig):
        self.folder = Path(folder)
        self.cfg = cfg
        self._snapshots: dict[str, _Snapshot] = {}
        self._processed: dict[str, _Snapshot] = {}
        self._failures: dict[str, tuple[int, int, int]] = {}

    def poll(self) -> list[ScanItem]:
        ready: list[ScanItem] = []
        seen: set[str] = set()

        for item in scanning.scan_inputs(self.folder, self.cfg.output.subdir_name, self.cfg.input):
            rel = item.rel
            seen.add(rel)
            snapshot = self._stat_snapshot(item.src)
            if snapshot is None:
                self._forget(rel)
                continue

            previous = self._snapshots.get(rel)
            if previous != snapshot:
                self._snapshots[rel] = snapshot
                self._forget_if_stale(rel, snapshot)
                continue

            if self._processed.get(rel) == snapshot:
                continue
            if self._is_frozen(rel, snapshot):
                continue

            ready.append(item)

        for rel in set(self._snapshots) - seen:
            self._forget(rel)

        return ready

    def observe(self, results: list[FileResult]) -> None:
        for result in results:
            rel = _result_rel(result)
            snapshot = self._snapshot_for_result(rel, result.source)
            if snapshot is None:
                self._forget(rel)
                continue

            if result.status == FileStatus.FAILED:
                self._processed.pop(rel, None)
                failed = self._failures.get(rel)
                attempts = (
                    failed[2]
                    if failed is not None and _failure_snapshot(failed) == snapshot
                    else 0
                )
                self._failures[rel] = (snapshot[0], snapshot[1], attempts + 1)
                continue

            self._processed[rel] = snapshot
            self._failures.pop(rel, None)

    def _stat_snapshot(self, path: Path) -> _Snapshot | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    def _snapshot_for_result(self, rel: str, source: Path) -> _Snapshot | None:
        snapshot = self._snapshots.get(rel)
        if snapshot is not None:
            return snapshot

        snapshot = self._stat_snapshot(source)
        if snapshot is not None:
            self._snapshots[rel] = snapshot
        return snapshot

    def _forget(self, rel: str) -> None:
        self._snapshots.pop(rel, None)
        self._processed.pop(rel, None)
        self._failures.pop(rel, None)

    def _forget_if_stale(self, rel: str, snapshot: _Snapshot) -> None:
        if self._processed.get(rel) != snapshot:
            self._processed.pop(rel, None)
        failed = self._failures.get(rel)
        if failed is not None and _failure_snapshot(failed) != snapshot:
            self._failures.pop(rel, None)

    def _is_frozen(self, rel: str, snapshot: _Snapshot) -> bool:
        failed = self._failures.get(rel)
        if failed is None:
            return False
        if _failure_snapshot(failed) != snapshot:
            self._failures.pop(rel, None)
            return False
        return failed[2] >= self.cfg.watch.max_retries


def watch_loop(
    folder: Path,
    cfg: AppConfig,
    engine: Any,
    client: Any,
    prompt_template: str,
    *,
    progress_cb: ProgressCb | None = None,
    log_cb: Callable[[str], None] | None = None,
    file_cb: Callable[[FileResult], None] | None = None,
    cycle_cb: Callable[[int, int, int], None] | None = None,
    stop_event: threading.Event,
    run_batch_fn: Callable[..., BatchSummary] = run_batch,
) -> WatchSummary:
    folder = Path(folder)
    cfg = _watch_config_with_single_worker(cfg, log_cb)
    watcher = FolderWatcher(folder, cfg)
    cycles = 0
    total_processed = 0
    results: list[FileResult] = []

    while not stop_event.is_set():
        cycles += 1
        try:
            cycle = WatchCycle(index=cycles, ready=watcher.poll())

            if cycle.ready:
                _log_work_cycle(cycle.index, len(cycle.ready), log_cb)
                summary = run_batch_fn(
                    folder,
                    cfg,
                    engine,
                    client,
                    prompt_template,
                    progress_cb=progress_cb,
                    log_cb=log_cb,
                    cancel_event=stop_event,
                    file_cb=file_cb,
                    files=cycle.ready,
                )
                watcher.observe(summary.results)
                results.extend(summary.results)
                total_processed += len(summary.results)
                _emit_cycle(cycle_cb, cycle.index, len(cycle.ready), total_processed)
                if summary.cancelled:
                    break
            else:
                _emit_cycle(cycle_cb, cycle.index, 0, total_processed)
        except Exception as exc:
            _log_cycle_error(cycles, cfg.watch.poll_seconds, exc, log_cb)

        if stop_event.is_set():
            break
        stop_event.wait(cfg.watch.poll_seconds)

    return WatchSummary(cycles=cycles, total_processed=total_processed, results=results)


def _result_rel(result: FileResult) -> str:
    return result.rel or result.source.name


def _failure_snapshot(failed: tuple[int, int, int]) -> _Snapshot:
    return (failed[0], failed[1])


def _watch_config_with_single_worker(
    cfg: AppConfig,
    log_cb: Callable[[str], None] | None,
) -> AppConfig:
    if resolve_worker_count(cfg.performance, os.cpu_count()) <= 1:
        return cfg

    message = "監看模式使用單一處理程序（每輪重建平行 worker 開銷過大），已忽略 workers 設定"
    if log_cb is not None:
        log_cb(message)
    _logger.info("%s", message)
    return replace(cfg, performance=replace(cfg.performance, workers=1))


def _log_work_cycle(index: int, ready_count: int, log_cb: Callable[[str], None] | None) -> None:
    message = f"監看第 {index} 輪：就緒 {ready_count} 檔"
    if log_cb is not None:
        log_cb(message)
    _logger.info("%s", message)


def _log_cycle_error(
    index: int,
    poll_seconds: float,
    exc: Exception,
    log_cb: Callable[[str], None] | None,
) -> None:
    message = f"監看第 {index} 輪發生錯誤，{poll_seconds} 秒後重試: {exc}"
    if log_cb is not None:
        log_cb(message)
    _logger.exception("%s", message)


def _emit_cycle(
    cycle_cb: Callable[[int, int, int], None] | None,
    index: int,
    ready_count: int,
    cumulative: int,
) -> None:
    if cycle_cb is not None:
        cycle_cb(index, ready_count, cumulative)
