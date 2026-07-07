from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
ctk = pytest.importorskip("customtkinter")

from pdf_ocrer import __version__  # noqa: E402
from pdf_ocrer.config import OcrConfig  # noqa: E402
from pdf_ocrer.gui import App, _folder_from_drop_data  # noqa: E402
from pdf_ocrer.pipeline import BatchSummary, FileResult, FileStatus  # noqa: E402


@pytest.fixture()
def app(tmp_path: Path):
    try:
        probe = ctk.CTk()
    except tk.TclError:
        pytest.skip("no display")
    else:
        probe.withdraw()
        probe.destroy()

    instance = App(
        config_path=tmp_path / "config.toml",
        engine_factory=lambda cfg: None,  # type: ignore[return-value]
        client_factory=lambda cfg: None,
    )
    instance.withdraw()
    try:
        yield instance
    finally:
        instance.destroy()


def test_app_title_contains_version(app: App) -> None:
    assert __version__ in app.title()


def test_log_event_appends_to_read_only_log(app: App) -> None:
    app._queue.put(("log", "hello"))
    app._drain_queue()

    assert "hello" in app.log_text.get("1.0", "end")


def test_done_event_reenables_start_and_disables_cancel(app: App, tmp_path: Path) -> None:
    summary = BatchSummary(results=[], csv_path=None, output_dir=tmp_path, cancelled=False)

    app._queue.put(("done", summary))
    app._drain_queue()

    assert app.start_button.cget("state") == "normal"
    assert app.cancel_button.cget("state") == "disabled"


def test_progress_event_updates_status_table_and_progressbar(app: App) -> None:
    app._queue.put(("progress", 3, 12, 5, 20, "檔名.pdf"))
    app._drain_queue()

    status = app.status_var.get()
    assert "檔" in status
    assert "頁" in status
    assert app.progressbar.get() == pytest.approx(3 / 12)

    rows = app.file_tree.get_children()
    assert len(rows) == 1
    values = app.file_tree.item(rows[0], "values")
    assert values == ("檔名.pdf", "處理中", "", "")


def test_file_done_event_updates_status_table(app: App, tmp_path: Path) -> None:
    result = FileResult(
        source=tmp_path / "原檔.pdf",
        output=tmp_path / "新檔.pdf",
        status=FileStatus.SUCCESS_OCR,
        total_pages=5,
        ocr_pages=3,
        naming_source="llm",
        note="",
    )

    app._queue.put(("file_done", result))
    app._drain_queue()

    rows = app.file_tree.get_children()
    assert len(rows) == 1
    values = app.file_tree.item(rows[0], "values")
    assert values == ("原檔.pdf", FileStatus.SUCCESS_OCR.value, "新檔.pdf", "3")


def test_file_done_event_uses_rel_for_same_basename_rows(app: App, tmp_path: Path) -> None:
    first = FileResult(
        source=tmp_path / "alpha" / "same.pdf",
        output=tmp_path / "OCR輸出" / "alpha" / "same_OCR.pdf",
        status=FileStatus.SUCCESS_OCR,
        total_pages=1,
        ocr_pages=1,
        naming_source="none",
        note="",
        rel="alpha/same.pdf",
    )
    second = FileResult(
        source=tmp_path / "beta" / "same.pdf",
        output=tmp_path / "OCR輸出" / "beta" / "same_OCR.pdf",
        status=FileStatus.SUCCESS_OCR,
        total_pages=1,
        ocr_pages=1,
        naming_source="none",
        note="",
        rel="beta/same.pdf",
    )

    app._queue.put(("file_done", first))
    app._queue.put(("file_done", second))
    app._drain_queue()

    rows = app.file_tree.get_children()
    assert len(rows) == 2
    values = [app.file_tree.item(row, "values") for row in rows]
    assert values == [
        ("alpha/same.pdf", FileStatus.SUCCESS_OCR.value, "alpha/same_OCR.pdf", "1"),
        ("beta/same.pdf", FileStatus.SUCCESS_OCR.value, "beta/same_OCR.pdf", "1"),
    ]


def test_auto_csv_checkbox_defaults_checked(app: App) -> None:
    assert app.auto_csv_var.get() is True


def test_force_checkbox_defaults_unchecked(app: App) -> None:
    assert app.force_var.get() is False


def test_theme_switch_calls_customtkinter(app: App, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("pdf_ocrer.gui.ctk.set_appearance_mode", calls.append)

    app._change_appearance("深色")

    assert calls == ["dark"]


def test_drop_data_uses_file_parent_and_handles_braced_spaces(tmp_path: Path) -> None:
    folder = tmp_path / "資料 夾"
    folder.mkdir()
    pdf = folder / "有 空白.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")

    assert _folder_from_drop_data(f"{{{pdf}}}") == folder
    assert _folder_from_drop_data(str(folder)) == folder


def test_open_settings_dialog_passes_config_path_and_open_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs: list[StubSettingsDialog] = []
    waited: list[object] = []
    app = App.__new__(App)
    app._config_path = tmp_path / "config.toml"
    app._open_path = lambda path: None
    app.wait_window = waited.append  # type: ignore[method-assign]

    class StubSettingsDialog:
        def __init__(self, master: object, config_path: Path, *, open_path: object) -> None:
            self.master = master
            self.config_path = config_path
            self.open_path = open_path
            self.grabbed = False
            dialogs.append(self)

        def grab_set(self) -> None:
            self.grabbed = True

    monkeypatch.setattr("pdf_ocrer.gui.SettingsDialog", StubSettingsDialog)

    app._open_settings_dialog()

    assert len(dialogs) == 1
    assert dialogs[0].master is app
    assert dialogs[0].config_path == app._config_path
    assert dialogs[0].open_path is app._open_path
    assert dialogs[0].grabbed is True
    assert waited == [dialogs[0]]


def test_create_engine_delegates_to_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    app = App.__new__(App)
    app._engine_factory = None
    cfg = OcrConfig(engine="rapidocr")
    sentinel = object()
    captured: dict[str, object] = {}

    def log_cb(message: str) -> None:
        pass

    def fake_create_engine(ocr_cfg: OcrConfig, log: object) -> object:
        captured["cfg"] = ocr_cfg
        captured["log"] = log
        return sentinel

    monkeypatch.setattr("pdf_ocrer.gui.create_engine", fake_create_engine, raising=False)

    assert app._create_engine(cfg, log_cb) is sentinel
    assert captured == {"cfg": cfg, "log": log_cb}


def test_worker_passes_force_checkbox_to_run_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "input"
    folder.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text("[naming]\nenabled = false\n", encoding="utf-8")
    captured: dict[str, object] = {}
    app = App.__new__(App)
    app._config_path = config_path
    app._queue = queue.Queue()
    app._client_factory = None
    app.force_var = _StaticBool(True)
    app._create_engine = lambda cfg, log_cb: object()  # type: ignore[method-assign]

    def fake_run_batch(folder_arg, cfg, engine, client, prompt_template, **kwargs):  # noqa: ANN001, ANN003
        captured["force"] = kwargs.get("force")
        output_dir = folder_arg / cfg.output.subdir_name
        return BatchSummary([], None, output_dir, False)

    monkeypatch.setattr("pdf_ocrer.gui.run_batch", fake_run_batch)

    app._run_worker(folder, threading.Event())

    assert captured == {"force": True}
    assert app._queue.get_nowait()[0] == "done"


def test_completion_message_includes_incremental_skip_count(tmp_path: Path) -> None:
    output_dir = tmp_path / "OCR輸出"
    summary = BatchSummary(
        results=[
            FileResult(
                source=tmp_path / "done.pdf",
                output=output_dir / "done_OCR.pdf",
                status=FileStatus.SKIPPED_DONE,
                total_pages=0,
                ocr_pages=0,
                naming_source="manifest",
                note="",
                rel="done.pdf",
            )
        ],
        csv_path=None,
        output_dir=output_dir,
        cancelled=False,
    )
    app = App.__new__(App)
    app._closed = False

    message = app._completion_message(summary)

    assert "已跳過（先前已處理）：1" in message
    assert "本次無新處理檔案" in message


def test_completion_message_cancelled_without_csv_keeps_cancel_wording(tmp_path: Path) -> None:
    summary = BatchSummary(
        results=[],
        csv_path=None,
        output_dir=tmp_path / "OCR輸出",
        cancelled=True,
    )
    app = App.__new__(App)
    app._closed = False

    message = app._completion_message(summary)

    assert "狀態：已取消" in message
    assert "本次無新處理檔案" not in message


def test_done_with_no_csv_does_not_open_path(app: App, tmp_path: Path) -> None:
    opened: list[Path] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(app, "_open_path", opened.append)
    try:
        summary = BatchSummary(
            results=[
                FileResult(
                    source=tmp_path / "done.pdf",
                    output=tmp_path / "OCR輸出" / "done_OCR.pdf",
                    status=FileStatus.SKIPPED_DONE,
                    total_pages=0,
                    ocr_pages=0,
                    naming_source="manifest",
                    note="",
                    rel="done.pdf",
                )
            ],
            csv_path=None,
            output_dir=tmp_path / "OCR輸出",
            cancelled=False,
        )

        app._queue.put(("done", summary))
        app._drain_queue()

        assert opened == []
    finally:
        monkeypatch.undo()


def test_worker_thread_is_not_daemon(app: App, work_folder: Path, monkeypatch) -> None:
    _keep_only(work_folder, {"native.pdf"})
    monkeypatch.setattr("pdf_ocrer.gui.messagebox.askyesno", lambda *args, **kwargs: False)
    monkeypatch.setattr(app, "_open_path", lambda path: None)
    app.folder_var.set(str(work_folder))

    app._start()

    assert app._worker is not None
    try:
        assert app._worker.daemon is False
    finally:
        _wait_for_worker(app)


def _wait_for_worker(app: App) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.update()
        if app._worker is not None and not app._worker.is_alive():
            app._drain_queue()
            app.update()
            if not app._running:
                return
        time.sleep(0.01)

    raise AssertionError("GUI worker did not finish")


def _keep_only(folder: Path, names: set[str]) -> None:
    for path in folder.iterdir():
        if path.is_file() and path.name not in names:
            path.unlink()


class _StaticBool:
    def __init__(self, value: bool) -> None:
        self._value = value

    def get(self) -> bool:
        return self._value
