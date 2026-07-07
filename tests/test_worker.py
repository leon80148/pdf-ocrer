from __future__ import annotations

import queue
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
import pytest

from fixtures_gen import GT_LINES
from pdf_ocrer import worker
from pdf_ocrer.config import DebugConfig, OcrConfig
from pdf_ocrer.ocr_engine import OcrLine
from pdf_ocrer.pdf_processor import PageReport, PdfResult
from pdf_ocrer.worker import WorkerTask

_DPI = 200
_FONT = pymupdf.Font("cjk")


class FakeEngine:
    def __init__(self, lines: list[OcrLine]) -> None:
        self.lines = lines
        self.images: list[np.ndarray] = []

    def recognize(self, img_rgb: np.ndarray) -> list[OcrLine]:
        self.images.append(img_rgb.copy())
        return self.lines


class FakeCancel:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.value


class FakeDoc:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def subset_fonts(self) -> None:
        self.calls.append(("subset_fonts",))

    def save(self, output: Path, garbage: int, deflate: bool) -> None:
        self.calls.append(("save", output, garbage, deflate))
        output.write_bytes(b"%PDF-1.7\n% fake worker output\n")

    def close(self) -> None:
        self.calls.append(("close",))


@pytest.fixture(autouse=True)
def reset_worker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "_engine_factory", _factory_for([]))
    worker.init_worker(OcrConfig(), DebugConfig(), None, None)


def test_process_file_task_writes_searchable_temp_and_page_events(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    engines = _install_engine(monkeypatch, [_gt_ocr_line()])
    worker.init_worker(OcrConfig(), DebugConfig(), None, events)
    temp = tmp_path / "OCR輸出" / "~abc.ocrtmp.pdf"

    outcome = worker.process_file_task(
        WorkerTask(
            index=0,
            source=str(fixtures_dir / "scanned.pdf"),
            rel="scanned.pdf",
            temp_output=str(temp),
            total_files=1,
        )
    )

    assert outcome.kind == "ok"
    assert outcome.temp_output == str(temp)
    assert len(outcome.page_texts) == 1
    assert "診斷證明書" in outcome.page_texts[0]
    assert outcome.total_pages == 1
    assert outcome.ocr_pages == 1
    assert outcome.all_existing_text is False
    assert outcome.note == ""
    assert temp.exists()
    assert _drain_events(events) == [("page", 0, "scanned.pdf", 1, 1)]
    assert len(engines) == 1

    doc = pymupdf.open(temp)
    try:
        assert "診斷證明書" in doc[0].get_text()
        assert doc[0].search_for("診斷證明書")
    finally:
        doc.close()


def test_process_file_task_copies_native_pdf_bytes(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    engines = _install_engine(monkeypatch, [_gt_ocr_line()])
    worker.init_worker(OcrConfig(), DebugConfig(), None, events)
    source = fixtures_dir / "native.pdf"
    temp = tmp_path / "out" / "~native.ocrtmp.pdf"

    outcome = worker.process_file_task(
        WorkerTask(
            index=3,
            source=str(source),
            rel="native.pdf",
            temp_output=str(temp),
            total_files=4,
        )
    )

    assert outcome.kind == "ok"
    assert outcome.temp_output == str(temp)
    assert outcome.total_pages == 1
    assert outcome.ocr_pages == 0
    assert outcome.all_existing_text is True
    assert "高雄市安家診所" in outcome.page_texts[0]
    assert temp.read_bytes() == source.read_bytes()
    assert _drain_events(events) == [("page", 3, "native.pdf", 1, 1)]
    assert len(engines) == 1
    assert engines[0].images == []


@pytest.mark.parametrize(
    ("filename", "expected_kind", "note_fragment"),
    [
        ("encrypted.pdf", "encrypted", "PDF requires a password"),
        ("corrupt.pdf", "failed", "FileDataError:"),
    ],
)
def test_process_file_task_error_kinds_delete_temp(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    expected_kind: str,
    note_fragment: str,
) -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    _install_engine(monkeypatch, [_gt_ocr_line()])
    worker.init_worker(OcrConfig(), DebugConfig(), None, events)
    temp = tmp_path / "out" / f"~{filename}.ocrtmp.pdf"
    temp.parent.mkdir()
    temp.write_bytes(b"stale temp")

    outcome = worker.process_file_task(
        WorkerTask(
            index=1,
            source=str(fixtures_dir / filename),
            rel=filename,
            temp_output=str(temp),
            total_files=2,
        )
    )

    assert outcome.kind == expected_kind
    assert outcome.temp_output is None
    assert outcome.page_texts == ()
    assert outcome.total_pages == 0
    assert outcome.ocr_pages == 0
    assert outcome.all_existing_text is False
    assert note_fragment in outcome.note
    assert not temp.exists()
    assert _drain_events(events) == []


def test_process_file_task_cancelled_kind_deletes_temp(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    cancel = FakeCancel(True)
    _install_engine(monkeypatch, [_gt_ocr_line()])
    worker.init_worker(OcrConfig(), DebugConfig(), cancel, events)
    temp = tmp_path / "out" / "~cancel.ocrtmp.pdf"
    temp.parent.mkdir()
    temp.write_bytes(b"stale temp")

    outcome = worker.process_file_task(
        WorkerTask(
            index=2,
            source=str(fixtures_dir / "scanned.pdf"),
            rel="scanned.pdf",
            temp_output=str(temp),
            total_files=3,
        )
    )

    assert outcome.kind == "cancelled"
    assert outcome.temp_output is None
    assert outcome.note == ""
    assert cancel.calls == 1
    assert not temp.exists()
    assert _drain_events(events) == []


def test_process_file_task_uses_pipeline_save_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    fake_doc = FakeDoc()
    _install_engine(monkeypatch, [])

    def fake_process_pdf(
        src: Path,
        cfg: object,
        engine: object,
        page_cb: object = None,
        cancel: object = None,
    ) -> PdfResult:
        assert src == tmp_path / "source.pdf"
        assert engine is not None
        assert cancel is None
        if callable(page_cb):
            page_cb(0, 1)
        return PdfResult(
            doc=fake_doc,  # type: ignore[arg-type]
            text="文字",
            page_texts=["文字"],
            reports=[PageReport(page_index=0, action="ocr", line_count=1)],
            total_pages=1,
            ocr_pages=1,
        )

    monkeypatch.setattr(worker, "process_pdf", fake_process_pdf)
    worker.init_worker(OcrConfig(), DebugConfig(visible_text=True), None, events)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"not read by fake processor")
    temp = tmp_path / "out" / "~save.ocrtmp.pdf"

    outcome = worker.process_file_task(
        WorkerTask(
            index=5,
            source=str(source),
            rel="source.pdf",
            temp_output=str(temp),
            total_files=6,
        )
    )

    assert outcome.kind == "ok"
    assert outcome.page_texts == ("文字",)
    assert temp.exists()
    assert fake_doc.calls == [
        ("subset_fonts",),
        ("save", temp, 3, True),
        ("close",),
    ]
    assert _drain_events(events) == [("page", 5, "source.pdf", 1, 1)]


def test_warmup_recognizes_white_image_and_reuses_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    engines = _install_engine(monkeypatch, [])
    worker.init_worker(replace(OcrConfig(), engine="rapidocr"), DebugConfig(), None, events)

    description = worker.warmup()
    second_description = worker.warmup()

    assert description == second_description
    assert "FakeEngine" in description
    assert len(engines) == 1
    assert len(engines[0].images) == 2
    assert all(image.shape == (8, 8, 3) for image in engines[0].images)
    assert all(image.dtype == np.uint8 for image in engines[0].images)
    assert all(np.all(image == 255) for image in engines[0].images)


def test_event_queue_handler_emits_log_message() -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    handler = worker._EventQueueHandler(events)
    record = logging.getLogger("pdf_ocrer.worker.test").makeRecord(
        "pdf_ocrer.worker.test",
        logging.INFO,
        __file__,
        1,
        "hello %s",
        ("worker",),
        None,
    )

    handler.emit(record)

    assert events.get_nowait() == ("log", "hello worker")


def test_init_worker_routes_pdf_ocrer_info_logs_to_events() -> None:
    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    worker.init_worker(OcrConfig(), DebugConfig(), None, events)

    logging.getLogger("pdf_ocrer.worker.test").info("worker log %s", "message")

    assert events.get_nowait() == ("log", "worker log message")


def _install_engine(
    monkeypatch: pytest.MonkeyPatch,
    lines: list[OcrLine],
) -> list[FakeEngine]:
    engines: list[FakeEngine] = []
    monkeypatch.setattr(worker, "_engine_factory", _factory_for(lines, engines))
    return engines


def _factory_for(
    lines: list[OcrLine],
    engines: list[FakeEngine] | None = None,
):
    def factory(cfg: OcrConfig, log: object = None) -> FakeEngine:
        assert isinstance(cfg, OcrConfig)
        assert log is None or callable(log)
        engine = FakeEngine(lines)
        if engines is not None:
            engines.append(engine)
        return engine

    return factory


def _gt_ocr_line() -> OcrLine:
    point, fontsize, text = GT_LINES[0]
    return OcrLine(text, _px_poly(point, fontsize, text), 0.99)


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


def _drain_events(events: queue.Queue[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    drained: list[tuple[Any, ...]] = []
    while True:
        try:
            drained.append(events.get_nowait())
        except queue.Empty:
            return drained
