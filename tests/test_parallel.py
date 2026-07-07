from __future__ import annotations

import csv
import json
import multiprocessing
import queue
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from pathlib import Path
from typing import Any

from pdf_ocrer.config import load_config
from pdf_ocrer.config import (
    AppConfig,
    DebugConfig,
    InputConfig,
    LlmConfig,
    NamingConfig,
    OcrConfig,
    OutputConfig,
    PerformanceConfig,
)
from pdf_ocrer.manifest import MANIFEST_NAME, FileIdentity, Manifest
from pdf_ocrer.parallel import _drain_events_until_quiet, _handle_event, run_batch_parallel
from pdf_ocrer.pipeline import FileResult, FileStatus
from pdf_ocrer.scanning import ScanItem
from pdf_ocrer.worker import WorkerOutcome, WorkerTask

_CSV_HEADER = ["原檔名", "新檔名", "狀態", "總頁數", "OCR頁數", "命名來源", "備註"]
_PROMPT = "$text"


class StaticClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.name


def make_cfg(**overrides: object) -> AppConfig:
    values = {
        "ocr": OcrConfig(),
        "output": OutputConfig(),
        "naming": NamingConfig(),
        "llm": LlmConfig(),
        "debug": DebugConfig(),
        "performance": PerformanceConfig(workers=2),
    }
    values.update(overrides)
    return AppConfig(**values)


def test_parallel_finalizes_csv_collision_txt_and_manifest_in_scan_order(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "b.pdf").write_bytes(b"b")
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    calls: list[str] = []
    logs: list[str] = []
    progress: list[tuple[int, int, int, int, str]] = []
    file_results: list[FileResult] = []

    def warmup() -> str:
        calls.append("warmup")
        return "fake"

    def worker_fn(task: WorkerTask) -> WorkerOutcome:
        calls.append(f"worker:{task.rel}")
        if task.rel == "a.pdf":
            time.sleep(0.05)
        temp = Path(task.temp_output)
        temp.write_bytes(f"pdf for {task.rel}".encode("ascii"))
        events.put(("log", f"log:{task.rel}"))
        events.put(("page", task.index, task.rel, 1, 1))
        return _ok_outcome(task, [f"text for {task.rel}"], ocr_pages=1)

    cfg = make_cfg(output=OutputConfig(export_txt=True))

    summary = run_batch_parallel(
        tmp_path,
        cfg,
        None,
        StaticClient("Report"),
        _PROMPT,
        progress_cb=lambda *args: progress.append(args),
        log_cb=logs.append,
        file_cb=file_results.append,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=2),
        worker_fn=worker_fn,
        warmup_fn=warmup,
        events_queue=events,
        worker_cancel=threading.Event(),
    )

    assert calls[0] == "warmup"
    assert summary.cancelled is False
    assert [result.rel for result in summary.results] == ["a.pdf", "b.pdf"]
    assert [result.output.name for result in summary.results if result.output is not None] == [
        "Report.pdf",
        "Report_2.pdf",
    ]
    assert [result.rel for result in file_results] == ["a.pdf", "b.pdf"]
    assert {item[-1] for item in progress} == {"a.pdf", "b.pdf"}
    assert [item[0] for item in progress] == sorted(item[0] for item in progress)
    assert all(item[1:4] == (2, 1, 1) for item in progress)
    assert "log:a.pdf" in logs
    assert "log:b.pdf" in logs

    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert rows[0] == _CSV_HEADER
    assert [row[0] for row in rows[1:]] == ["a.pdf", "b.pdf"]
    assert [row[1] for row in rows[1:]] == ["Report.pdf", "Report_2.pdf"]

    first = summary.output_dir / "Report.pdf"
    second = summary.output_dir / "Report_2.pdf"
    assert first.read_bytes() == b"pdf for a.pdf"
    assert second.read_bytes() == b"pdf for b.pdf"
    assert "text for a.pdf" in first.with_suffix(".txt").read_text(encoding="utf-8-sig")
    assert "text for b.pdf" in second.with_suffix(".txt").read_text(encoding="utf-8-sig")

    manifest = json.loads((summary.output_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["entries"]["a.pdf"]["output"] == "Report.pdf"
    assert manifest["entries"]["b.pdf"]["output"] == "Report_2.pdf"


def test_parallel_explicit_files_skips_scan_and_processes_only_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "drop" / "nested" / "picked.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"picked")
    (tmp_path / "outside.pdf").write_bytes(b"outside")
    files = [ScanItem(source, "drop/nested/picked.pdf")]
    submitted: list[tuple[str, int]] = []

    def fail_scan(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("scan_inputs should not be called when files are supplied")

    def worker_fn(task: WorkerTask) -> WorkerOutcome:
        submitted.append((task.rel, task.total_files))
        Path(task.temp_output).write_bytes(b"pdf for picked")
        return _ok_outcome(task, ["text for picked"], ocr_pages=1)

    monkeypatch.setattr("pdf_ocrer.parallel.scan_inputs", fail_scan)

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(naming=NamingConfig(enabled=False)),
        None,
        None,
        _PROMPT,
        files=files,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=1),
        worker_fn=worker_fn,
        warmup_fn=lambda: "fake",
        events_queue=queue.Queue(),
        worker_cancel=threading.Event(),
    )

    assert submitted == [("drop/nested/picked.pdf", 1)]
    assert [result.rel for result in summary.results] == ["drop/nested/picked.pdf"]
    assert summary.results[0].status is FileStatus.SUCCESS_OCR
    assert summary.results[0].output == summary.output_dir / "drop" / "nested" / "picked_OCR.pdf"
    assert summary.results[0].output.read_bytes() == b"pdf for picked"
    assert not (summary.output_dir / "outside_OCR.pdf").exists()
    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert [row[0] for row in rows[1:]] == ["drop/nested/picked.pdf"]


def test_parallel_missing_explicit_file_fails_and_continues(tmp_path: Path) -> None:
    present = tmp_path / "present.pdf"
    present.write_bytes(b"present")
    files = [
        ScanItem(tmp_path / "missing.pdf", "missing.pdf"),
        ScanItem(present, "present.pdf"),
    ]
    submitted: list[str] = []

    def worker_fn(task: WorkerTask) -> WorkerOutcome:
        submitted.append(task.rel)
        Path(task.temp_output).write_bytes(b"pdf for present")
        return _ok_outcome(task, ["text for present"], ocr_pages=1)

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(naming=NamingConfig(enabled=False)),
        None,
        None,
        _PROMPT,
        files=files,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=1),
        worker_fn=worker_fn,
        warmup_fn=lambda: "fake",
        events_queue=queue.Queue(),
        worker_cancel=threading.Event(),
    )

    assert submitted == ["present.pdf"]
    assert [result.rel for result in summary.results] == ["missing.pdf", "present.pdf"]
    assert [result.status for result in summary.results] == [
        FileStatus.FAILED,
        FileStatus.SUCCESS_OCR,
    ]
    assert "FileNotFoundError" in summary.results[0].note
    assert summary.results[1].output == summary.output_dir / "present_OCR.pdf"
    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert [row[0] for row in rows[1:]] == ["missing.pdf", "present.pdf"]
    assert [row[2] for row in rows[1:]] == [
        FileStatus.FAILED.value,
        FileStatus.SUCCESS_OCR.value,
    ]
    manifest = json.loads((summary.output_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "missing.pdf" not in manifest["entries"]
    assert manifest["entries"]["present.pdf"]["output"] == "present_OCR.pdf"


def test_parallel_cancel_uses_fallback_for_completed_and_cleans_pending_temps(
    tmp_path: Path,
) -> None:
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (tmp_path / name).write_bytes(name.encode("ascii"))
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    cancel = threading.Event()
    worker_cancel = threading.Event()
    first_two_ready = threading.Event()
    lock = threading.Lock()
    ready_count = 0
    returned_count = 0
    client = StaticClient("ShouldNotBeUsed")

    def worker_fn(task: WorkerTask) -> WorkerOutcome:
        nonlocal ready_count, returned_count
        if task.rel == "c.pdf":
            while not cancel.is_set():
                time.sleep(0.005)
            return _cancelled_outcome(task)

        with lock:
            ready_count += 1
            if ready_count == 2:
                first_two_ready.set()
        first_two_ready.wait(timeout=2)
        temp = Path(task.temp_output)
        temp.write_bytes(f"pdf for {task.rel}".encode("ascii"))
        outcome = _ok_outcome(task, [f"text for {task.rel}"], ocr_pages=1)
        with lock:
            returned_count += 1
            if returned_count == 2:
                cancel.set()
        return outcome

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(),
        None,
        client,
        _PROMPT,
        cancel_event=cancel,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=3),
        worker_fn=worker_fn,
        warmup_fn=lambda: "fake",
        events_queue=events,
        worker_cancel=worker_cancel,
    )

    assert summary.cancelled is True
    assert worker_cancel.is_set()
    assert client.prompts == []
    assert [result.rel for result in summary.results] == ["a.pdf", "b.pdf"]
    assert [result.output.name for result in summary.results if result.output is not None] == [
        "a_OCR.pdf",
        "b_OCR.pdf",
    ]
    assert all(result.naming_source == "fallback" for result in summary.results)
    assert all(result.note == "已取消-使用備用檔名" for result in summary.results)
    assert not (summary.output_dir / "c_OCR.pdf").exists()
    assert list(summary.output_dir.rglob("~*.ocrtmp.pdf")) == []


def test_parallel_warmup_failure_marks_unskipped_files_failed(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "b.pdf").write_bytes(b"b")
    file_results: list[FileResult] = []
    logs: list[str] = []

    def worker_fn(task: WorkerTask) -> WorkerOutcome:
        raise AssertionError(f"worker should not run: {task.rel}")

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(),
        None,
        None,
        _PROMPT,
        log_cb=logs.append,
        file_cb=file_results.append,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=2),
        worker_fn=worker_fn,
        warmup_fn=lambda: (_ for _ in ()).throw(RuntimeError("model missing")),
        events_queue=queue.Queue(),
        worker_cancel=threading.Event(),
    )

    assert summary.cancelled is False
    assert [result.status for result in summary.results] == [FileStatus.FAILED, FileStatus.FAILED]
    assert [result.rel for result in file_results] == ["a.pdf", "b.pdf"]
    assert all("RuntimeError: model missing" in result.note for result in summary.results)
    assert logs == ["正在載入 OCR 模型…（平行模式，2 個工作行程）"]
    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert [row[0] for row in rows[1:]] == ["a.pdf", "b.pdf"]
    assert [row[2] for row in rows[1:]] == [FileStatus.FAILED.value, FileStatus.FAILED.value]


def test_parallel_broken_pool_preserves_other_completed_future(tmp_path: Path) -> None:
    (tmp_path / "broken.pdf").write_bytes(b"broken")
    (tmp_path / "success.pdf").write_bytes(b"success")
    executor = _BrokenThenSuccessExecutor()

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(naming=NamingConfig(enabled=False)),
        None,
        None,
        _PROMPT,
        executor_factory=lambda: executor,
        worker_fn=lambda task: _ok_outcome(task, ["unused"]),
        warmup_fn=lambda: "fake",
        events_queue=queue.Queue(),
        worker_cancel=threading.Event(),
    )

    assert summary.cancelled is False
    assert [result.rel for result in summary.results] == ["broken.pdf", "success.pdf"]
    assert [result.status for result in summary.results] == [
        FileStatus.FAILED,
        FileStatus.SUCCESS_EXISTING_TEXT,
    ]
    assert "BrokenProcessPool: pool broke" in summary.results[0].note
    assert summary.results[1].output == summary.output_dir / "success_OCR.pdf"
    assert summary.results[1].output.read_bytes() == b"completed success"


def test_parallel_cancel_waits_for_running_future_before_cleanup(tmp_path: Path) -> None:
    (tmp_path / "late.pdf").write_bytes(b"late")
    executor = _CompletingOnShutdownExecutor()
    worker_cancel = threading.Event()

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(naming=NamingConfig(enabled=False)),
        None,
        None,
        _PROMPT,
        cancel_event=_FlipCancelEvent(trigger_call=3),
        executor_factory=lambda: executor,
        worker_fn=lambda task: _ok_outcome(task, ["unused"]),
        warmup_fn=lambda: "fake",
        events_queue=queue.Queue(),
        worker_cancel=worker_cancel,
    )

    assert summary.cancelled is True
    assert worker_cancel.is_set()
    assert [result.rel for result in summary.results] == ["late.pdf"]
    assert summary.results[0].output == summary.output_dir / "late_OCR.pdf"
    assert summary.results[0].output.read_bytes() == b"late temp"
    assert summary.results[0].naming_source == "fallback"
    assert executor.shutdown_calls[0] == (False, True)
    assert executor.shutdown_calls[-1] == (True, False)
    assert list(summary.output_dir.rglob("~*.ocrtmp.pdf")) == []


def test_parallel_page_progress_file_index_is_monotonic_for_out_of_order_events() -> None:
    progress: list[tuple[int, int, int, int, str]] = []

    _handle_event(
        ("page", 1, "b.pdf", 1, 1),
        lambda *args: progress.append(args),
        None,
        2,
        files_done=0,
    )
    _handle_event(
        ("page", 0, "a.pdf", 1, 1),
        lambda *args: progress.append(args),
        None,
        2,
        files_done=0,
    )
    _handle_event(
        ("page", 1, "b.pdf", 1, 1),
        lambda *args: progress.append(args),
        None,
        2,
        files_done=1,
    )

    assert [item[0] for item in progress] == [1, 1, 2]
    assert [item[-1] for item in progress] == ["b.pdf", "a.pdf", "b.pdf"]


def test_parallel_final_event_drain_retries_after_transient_empty() -> None:
    progress: list[tuple[int, int, int, int, str]] = []
    events = _TransientEmptyQueue(("page", 0, "tail.pdf", 1, 1))

    _drain_events_until_quiet(
        events,
        lambda *args: progress.append(args),
        None,
        total_files=1,
        files_done=0,
        wait_seconds=0.001,
        max_seconds=0.05,
    )

    assert progress == [(1, 1, 1, 1, "tail.pdf")]


def test_parallel_recursive_encrypted_subfolder_does_not_create_empty_output_dir(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "secret.pdf").write_bytes(b"secret")

    def worker_fn(task: WorkerTask) -> WorkerOutcome:
        return WorkerOutcome(
            index=task.index,
            source=task.source,
            rel=task.rel,
            kind="encrypted",
            temp_output=None,
            page_texts=(),
            total_pages=0,
            ocr_pages=0,
            all_existing_text=False,
            note="encrypted",
        )

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(input=InputConfig(recursive=True)),
        None,
        None,
        _PROMPT,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=1),
        worker_fn=worker_fn,
        warmup_fn=lambda: "fake",
        events_queue=queue.Queue(),
        worker_cancel=threading.Event(),
    )

    assert [result.rel for result in summary.results] == ["nested/secret.pdf"]
    assert summary.results[0].status is FileStatus.SKIPPED_ENCRYPTED
    assert summary.output_dir.exists()
    assert not (summary.output_dir / "nested").exists()


def test_parallel_cleans_stale_temps_and_does_not_submit_skipped_done(tmp_path: Path) -> None:
    source = tmp_path / "done.pdf"
    source.write_bytes(b"done")
    cfg = make_cfg()
    output_dir = tmp_path / cfg.output.subdir_name
    output_dir.mkdir()
    output = output_dir / "done_OCR.pdf"
    output.write_bytes(b"previous")
    stale_temp = output_dir / "~stale.ocrtmp.pdf"
    stale_temp.write_bytes(b"stale")
    manifest = Manifest()
    manifest.record(
        "done.pdf",
        FileIdentity.from_stat(source),
        FileStatus.SUCCESS_OCR.value,
        "done_OCR.pdf",
    )
    manifest.save(output_dir / MANIFEST_NAME)

    def executor_factory() -> ThreadPoolExecutor:
        raise AssertionError("no executor should be created when every file is skipped")

    summary = run_batch_parallel(
        tmp_path,
        cfg,
        None,
        None,
        _PROMPT,
        executor_factory=executor_factory,
        worker_fn=lambda task: _ok_outcome(task, ["unused"]),  # pragma: no cover
        warmup_fn=lambda: "unused",
        events_queue=queue.Queue(),
        worker_cancel=threading.Event(),
    )

    assert [result.status for result in summary.results] == [FileStatus.SKIPPED_DONE]
    assert summary.results[0].output == output
    assert summary.csv_path is None
    assert not stale_temp.exists()


def test_parallel_process_pool_spawn_smoke_without_real_ocr(tmp_path: Path) -> None:
    (tmp_path / "native.pdf").write_bytes(b"native")
    (tmp_path / "corrupt.pdf").write_bytes(b"corrupt")
    ctx = multiprocessing.get_context("spawn")

    summary = run_batch_parallel(
        tmp_path,
        make_cfg(naming=NamingConfig(enabled=False)),
        None,
        None,
        _PROMPT,
        executor_factory=lambda: ProcessPoolExecutor(max_workers=2, mp_context=ctx),
        worker_fn=_spawn_worker,
        warmup_fn=_spawn_warmup,
        events_queue=ctx.Queue(),
        worker_cancel=ctx.Event(),
    )

    assert summary.cancelled is False
    assert [result.rel for result in summary.results] == ["corrupt.pdf", "native.pdf"]
    assert [result.status for result in summary.results] == [
        FileStatus.FAILED,
        FileStatus.SUCCESS_EXISTING_TEXT,
    ]
    assert summary.results[1].output == summary.output_dir / "native_OCR.pdf"
    assert summary.results[1].output.read_bytes() == b"spawn native"
    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert [row[0] for row in rows[1:]] == ["corrupt.pdf", "native.pdf"]


def test_run_batch_default_parallel_spawn_wiring_native_and_corrupt(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    (tmp_path / "native.pdf").write_bytes((fixtures_dir / "native.pdf").read_bytes())
    (tmp_path / "corrupt.pdf").write_bytes((fixtures_dir / "corrupt.pdf").read_bytes())
    cfg = replace(
        load_config(tmp_path / "missing.toml"),
        naming=NamingConfig(enabled=False),
        performance=PerformanceConfig(workers=2),
    )

    summary = run_batch_parallel(
        tmp_path,
        cfg,
        object(),  # type: ignore[arg-type]
        None,
        _PROMPT,
        warmup_fn=_noop_warmup,
    )

    assert summary.cancelled is False
    assert [result.rel for result in summary.results] == ["corrupt.pdf", "native.pdf"]
    assert [result.status for result in summary.results] == [
        FileStatus.FAILED,
        FileStatus.SUCCESS_EXISTING_TEXT,
    ]
    assert summary.results[1].output == summary.output_dir / "native_OCR.pdf"
    assert summary.results[1].output.exists()
    assert summary.csv_path is not None
    rows = _read_csv(summary.csv_path)
    assert [row[0] for row in rows[1:]] == ["corrupt.pdf", "native.pdf"]
    assert [row[2] for row in rows[1:]] == [
        FileStatus.FAILED.value,
        FileStatus.SUCCESS_EXISTING_TEXT.value,
    ]
    assert list(summary.output_dir.rglob("~*.ocrtmp.pdf")) == []


def _spawn_warmup() -> str:
    return "spawn warmup"


def _noop_warmup() -> str:
    return "test warmup skipped"


def _spawn_worker(task: WorkerTask) -> WorkerOutcome:
    if task.rel == "corrupt.pdf":
        return WorkerOutcome(
            index=task.index,
            source=task.source,
            rel=task.rel,
            kind="failed",
            temp_output=None,
            page_texts=(),
            total_pages=0,
            ocr_pages=0,
            all_existing_text=False,
            note="fake corrupt",
        )

    Path(task.temp_output).write_bytes(b"spawn native")
    return WorkerOutcome(
        index=task.index,
        source=task.source,
        rel=task.rel,
        kind="ok",
        temp_output=task.temp_output,
        page_texts=("native text",),
        total_pages=1,
        ocr_pages=0,
        all_existing_text=True,
        note="",
    )


class _DoneFuture:
    def __init__(self, *, result: object = None, exc: BaseException | None = None) -> None:
        self._result = result
        self._exc = exc

    def done(self) -> bool:
        return True

    def result(self) -> object:
        if self._exc is not None:
            raise self._exc
        return self._result


class _BrokenThenSuccessExecutor:
    def submit(self, fn: Any, *args: object) -> _DoneFuture:
        if not args:
            return _DoneFuture(result=fn())

        task = args[0]
        assert isinstance(task, WorkerTask)
        if task.rel == "broken.pdf":
            return _DoneFuture(exc=BrokenProcessPool("pool broke"))

        Path(task.temp_output).write_bytes(b"completed success")
        return _DoneFuture(
            result=_ok_outcome(task, ["native text"], all_existing_text=True),
        )

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        _ = wait, cancel_futures


class _CompletingOnShutdownExecutor:
    def __init__(self) -> None:
        self.shutdown_calls: list[tuple[bool, bool]] = []
        self._future: Future[WorkerOutcome] | None = None
        self._thread: threading.Thread | None = None

    def submit(self, fn: Any, *args: object) -> Future[object]:
        future: Future[object] = Future()
        if not args:
            future.set_result(fn())
            return future

        task = args[0]
        assert isinstance(task, WorkerTask)
        Path(task.temp_output).write_bytes(b"late temp")
        self._future = Future()
        self._outcome = _ok_outcome(task, ["late text"], ocr_pages=1)
        return self._future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))
        self._start_completion_thread()
        if wait and self._thread is not None:
            self._thread.join(timeout=2)

    def _start_completion_thread(self) -> None:
        if self._future is None or self._thread is not None:
            return

        def complete() -> None:
            time.sleep(0.02)
            assert self._future is not None
            if not self._future.done():
                self._future.set_result(self._outcome)

        self._thread = threading.Thread(target=complete)
        self._thread.start()


class _FlipCancelEvent:
    def __init__(self, *, trigger_call: int) -> None:
        self._trigger_call = trigger_call
        self._calls = 0

    def is_set(self) -> bool:
        self._calls += 1
        return self._calls >= self._trigger_call


class _TransientEmptyQueue:
    def __init__(self, event: tuple[Any, ...]) -> None:
        self._event = event
        self._calls = 0

    def get(self, timeout: float) -> tuple[Any, ...]:
        _ = timeout
        self._calls += 1
        if self._calls == 2:
            return self._event
        raise queue.Empty


def _ok_outcome(
    task: WorkerTask,
    page_texts: list[str],
    *,
    ocr_pages: int = 0,
    all_existing_text: bool = False,
) -> WorkerOutcome:
    return WorkerOutcome(
        index=task.index,
        source=task.source,
        rel=task.rel,
        kind="ok",
        temp_output=task.temp_output,
        page_texts=tuple(page_texts),
        total_pages=len(page_texts),
        ocr_pages=ocr_pages,
        all_existing_text=all_existing_text,
        note="",
    )


def _cancelled_outcome(task: WorkerTask) -> WorkerOutcome:
    return WorkerOutcome(
        index=task.index,
        source=task.source,
        rel=task.rel,
        kind="cancelled",
        temp_output=None,
        page_texts=(),
        total_pages=0,
        ocr_pages=0,
        all_existing_text=False,
        note="",
    )


def _read_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.reader(file))
