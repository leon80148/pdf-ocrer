from __future__ import annotations

import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from pdf_ocrer import __version__  # noqa: E402
from pdf_ocrer.gui import App  # noqa: E402
from pdf_ocrer.pipeline import BatchSummary  # noqa: E402


@pytest.fixture()
def app(tmp_path: Path):
    try:
        probe = tk.Tk()
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

    assert "disabled" not in app.start_button.state()
    assert "disabled" in app.cancel_button.state()


def test_progress_event_updates_status(app: App) -> None:
    app._queue.put(("progress", 3, 12, 5, 20, "檔名.pdf"))
    app._drain_queue()

    status = app.status_var.get()
    assert "檔" in status
    assert "頁" in status


def test_worker_thread_is_not_daemon(app: App, work_folder: Path, monkeypatch) -> None:
    _keep_only(work_folder, {"native.pdf"})
    monkeypatch.setattr("pdf_ocrer.gui.messagebox.askyesno", lambda *args, **kwargs: False)
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
