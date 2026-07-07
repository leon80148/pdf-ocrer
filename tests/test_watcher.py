from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from pdf_ocrer.config import AppConfig, PerformanceConfig, WatchConfig, load_config
from pdf_ocrer.pipeline import BatchSummary, FileResult, FileStatus
from pdf_ocrer.scanning import ScanItem
from pdf_ocrer.watcher import FolderWatcher, watch_loop


def _cfg(tmp_path: Path, **watch_overrides: object) -> AppConfig:
    return replace(
        load_config(tmp_path / "missing.toml"),
        watch=WatchConfig(**watch_overrides),
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _result(item: ScanItem, status: FileStatus) -> FileResult:
    return FileResult(
        source=item.src,
        output=None,
        status=status,
        total_pages=0,
        ocr_pages=0,
        naming_source="none",
        note="",
        rel=item.rel,
    )


def test_poll_waits_until_snapshot_is_stable(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    source = tmp_path / "doc.pdf"
    watcher = FolderWatcher(tmp_path, cfg)

    _write(source, b"a")
    assert watcher.poll() == []

    _write(source, b"ab")
    assert watcher.poll() == []

    _write(source, b"abc")
    assert watcher.poll() == []

    ready = watcher.poll()

    assert [item.rel for item in ready] == ["doc.pdf"]
    assert ready[0].src == source


@pytest.mark.parametrize(
    "status",
    [
        FileStatus.SUCCESS_OCR,
        FileStatus.SKIPPED_DONE,
        FileStatus.SKIPPED_ENCRYPTED,
    ],
)
def test_observe_silences_non_failed_results_until_source_changes(
    tmp_path: Path,
    status: FileStatus,
) -> None:
    cfg = _cfg(tmp_path)
    source = tmp_path / "doc.pdf"
    watcher = FolderWatcher(tmp_path, cfg)

    _write(source, b"ready")
    assert watcher.poll() == []
    ready = watcher.poll()
    assert len(ready) == 1

    watcher.observe([_result(ready[0], status)])

    assert watcher.poll() == []

    _write(source, b"ready with changes")
    assert watcher.poll() == []
    assert [item.rel for item in watcher.poll()] == ["doc.pdf"]


def test_failed_results_retry_until_max_retries_then_unfreeze_on_change(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, max_retries=2)
    source = tmp_path / "doc.pdf"
    watcher = FolderWatcher(tmp_path, cfg)

    _write(source, b"ready")
    assert watcher.poll() == []
    ready = watcher.poll()

    watcher.observe([_result(ready[0], FileStatus.FAILED)])
    retry = watcher.poll()
    assert [item.rel for item in retry] == ["doc.pdf"]

    watcher.observe([_result(retry[0], FileStatus.FAILED)])
    assert watcher.poll() == []
    assert watcher.poll() == []

    _write(source, b"new content")
    assert watcher.poll() == []
    assert [item.rel for item in watcher.poll()] == ["doc.pdf"]


def test_watch_loop_runs_batches_and_reports_cycles(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, poll_seconds=0.01)
    source = tmp_path / "doc.pdf"
    _write(source, b"ready")
    stop_event = threading.Event()
    cycles: list[tuple[int, int, int]] = []
    logs: list[str] = []
    calls: list[dict[str, object]] = []

    def fake_run_batch(
        folder: Path,
        cfg_arg: AppConfig,
        engine: object,
        client: object,
        prompt_template: str,
        *,
        progress_cb: object = None,
        log_cb: object = None,
        cancel_event: threading.Event | None = None,
        file_cb: object = None,
        force: bool = False,
        files: list[ScanItem] | None = None,
    ) -> BatchSummary:
        assert files is not None
        result = _result(files[0], FileStatus.SUCCESS_OCR)
        calls.append(
            {
                "folder": folder,
                "cfg": cfg_arg,
                "engine": engine,
                "client": client,
                "prompt_template": prompt_template,
                "progress_cb": progress_cb,
                "log_cb": log_cb,
                "cancel_event": cancel_event,
                "file_cb": file_cb,
                "force": force,
                "files": files,
            }
        )
        return BatchSummary(
            results=[result],
            csv_path=None,
            output_dir=folder / cfg_arg.output.subdir_name,
            cancelled=False,
        )

    def cycle_cb(index: int, ready_count: int, cumulative: int) -> None:
        cycles.append((index, ready_count, cumulative))
        if index >= 3:
            stop_event.set()

    summary = watch_loop(
        tmp_path,
        cfg,
        engine="engine",
        client="client",
        prompt_template="prompt",
        log_cb=logs.append,
        cycle_cb=cycle_cb,
        stop_event=stop_event,
        run_batch_fn=fake_run_batch,
    )

    assert cycles == [(1, 0, 0), (2, 1, 1), (3, 0, 1)]
    assert len(calls) == 1
    assert [item.rel for item in calls[0]["files"]] == ["doc.pdf"]
    assert calls[0]["cancel_event"] is stop_event
    assert calls[0]["force"] is False
    assert summary.cycles == 3
    assert summary.total_processed == 1
    assert [result.status for result in summary.results] == [FileStatus.SUCCESS_OCR]
    assert any("第 2 輪" in message and "1 檔" in message for message in logs)


def test_watch_loop_logs_poll_error_and_continues_next_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path, poll_seconds=0.01)
    source = tmp_path / "doc.pdf"
    _write(source, b"ready")
    stop_event = threading.Event()
    logs: list[str] = []
    calls = 0
    batches: list[list[ScanItem]] = []

    def flaky_scan(*args: object, **kwargs: object) -> list[ScanItem]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("WinError 59")
        return [ScanItem(source, "doc.pdf")]

    def fake_run_batch(
        folder: Path,
        cfg_arg: AppConfig,
        engine: object,
        client: object,
        prompt_template: str,
        *,
        progress_cb: object = None,
        log_cb: object = None,
        cancel_event: threading.Event | None = None,
        file_cb: object = None,
        force: bool = False,
        files: list[ScanItem] | None = None,
    ) -> BatchSummary:
        assert files is not None
        batches.append(files)
        assert cancel_event is not None
        cancel_event.set()
        return BatchSummary(
            results=[_result(files[0], FileStatus.SUCCESS_OCR)],
            csv_path=None,
            output_dir=folder / cfg_arg.output.subdir_name,
            cancelled=False,
        )

    monkeypatch.setattr("pdf_ocrer.watcher.scanning.scan_inputs", flaky_scan)

    summary = watch_loop(
        tmp_path,
        cfg,
        engine="engine",
        client="client",
        prompt_template="prompt",
        log_cb=logs.append,
        stop_event=stop_event,
        run_batch_fn=fake_run_batch,
    )

    assert calls == 3
    assert [[item.rel for item in batch] for batch in batches] == [["doc.pdf"]]
    assert summary.total_processed == 1
    assert [result.status for result in summary.results] == [FileStatus.SUCCESS_OCR]
    assert any(
        "監看第 1 輪發生錯誤" in message
        and "0.01 秒後重試" in message
        and "WinError 59" in message
        for message in logs
    )


def test_watch_loop_forces_single_worker_config_in_watch_mode(tmp_path: Path) -> None:
    cfg = replace(
        _cfg(tmp_path, poll_seconds=0.01),
        performance=PerformanceConfig(workers=2),
    )
    source = tmp_path / "doc.pdf"
    _write(source, b"ready")
    stop_event = threading.Event()
    logs: list[str] = []
    captured_workers: list[int] = []

    def fake_run_batch(
        folder: Path,
        cfg_arg: AppConfig,
        engine: object,
        client: object,
        prompt_template: str,
        *,
        progress_cb: object = None,
        log_cb: object = None,
        cancel_event: threading.Event | None = None,
        file_cb: object = None,
        force: bool = False,
        files: list[ScanItem] | None = None,
    ) -> BatchSummary:
        assert files is not None
        captured_workers.append(cfg_arg.performance.workers)
        assert cancel_event is not None
        cancel_event.set()
        return BatchSummary(
            results=[_result(files[0], FileStatus.SUCCESS_OCR)],
            csv_path=None,
            output_dir=folder / cfg_arg.output.subdir_name,
            cancelled=False,
        )

    watch_loop(
        tmp_path,
        cfg,
        engine="engine",
        client="client",
        prompt_template="prompt",
        log_cb=logs.append,
        stop_event=stop_event,
        run_batch_fn=fake_run_batch,
    )

    assert captured_workers == [1]
    assert any("監看模式使用單一處理程序" in message for message in logs)
