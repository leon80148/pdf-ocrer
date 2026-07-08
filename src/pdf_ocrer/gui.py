from __future__ import annotations

import os
import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None
    _DND_AVAILABLE = False
else:
    _DND_AVAILABLE = True

from pdf_ocrer import __version__
from pdf_ocrer.cli import DEFAULT_NAMING_PROMPT, _load_prompt
from pdf_ocrer.app_logging import setup_logging
from pdf_ocrer.config import (
    ConfigError,
    GuiConfig,
    LlmConfig,
    LoggingConfig,
    OcrConfig,
    bootstrap_frozen_config,
    default_config_path,
    load_config,
    resolve_prompt_path,
)
from pdf_ocrer.llm_providers import LLMClient, create_client
from pdf_ocrer.ocr_engine import OcrEngineProtocol, create_engine
from pdf_ocrer.pipeline import BatchSummary, FileResult, FileStatus, run_batch
from pdf_ocrer.settings_dialog import SettingsDialog
from pdf_ocrer.watcher import WatchSummary, watch_loop

EngineFactory = Callable[[OcrConfig], OcrEngineProtocol]
ClientFactory = Callable[[LlmConfig], LLMClient | None]
GuiEvent = tuple[object, ...]

_APPEARANCE_TO_SEGMENT = {
    "light": "淺色",
    "dark": "深色",
    "system": "系統",
}
_SEGMENT_TO_APPEARANCE = {
    "淺色": "light",
    "深色": "dark",
    "系統": "system",
}
_TREE_COLUMNS = ("source", "status", "output", "ocr_pages")
_logger = logging.getLogger(__name__)


def run_gui(config_path: Path | None = None) -> None:
    app = App(config_path=config_path)
    app.mainloop()


def _effective_appearance(mode: str) -> str:
    normalized = mode.casefold()
    if normalized == "system":
        getter = getattr(ctk, "get_appearance_mode", None)
        if callable(getter):
            current = str(getter()).casefold()
            if current in {"light", "dark"}:
                return current
        return "light"
    return "dark" if normalized == "dark" else "light"


def _style_treeview(mode: str) -> None:
    appearance = _effective_appearance(mode)
    if appearance == "dark":
        background = "#242424"
        fieldbackground = "#2b2b2b"
        foreground = "#f2f2f2"
        heading_background = "#333333"
        selected_background = "#1f6aa5"
    else:
        background = "#f7f7f7"
        fieldbackground = "#ffffff"
        foreground = "#1f1f1f"
        heading_background = "#e5e5e5"
        selected_background = "#3b8ed0"

    style = ttk.Style()
    if "clam" in style.theme_names():
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
    style.configure(
        "PdfOcrer.Treeview",
        background=background,
        fieldbackground=fieldbackground,
        foreground=foreground,
        borderwidth=0,
        rowheight=28,
    )
    style.map(
        "PdfOcrer.Treeview",
        background=[("selected", selected_background)],
        foreground=[("selected", "#ffffff")],
    )
    style.configure(
        "PdfOcrer.Treeview.Heading",
        background=heading_background,
        foreground=foreground,
        relief="flat",
    )
    style.map("PdfOcrer.Treeview.Heading", relief=[("active", "flat")])


def _folder_from_drop_data(data: str) -> Path | None:
    stripped = data.strip()
    if stripped and not stripped.startswith("{") and Path(stripped).exists():
        return _folder_from_drop_items((stripped,))

    try:
        items = tk.Tcl().splitlist(data)
    except tk.TclError:
        items = _fallback_split_drop_data(data)
    return _folder_from_drop_items(items)


def _fallback_split_drop_data(data: str) -> tuple[str, ...]:
    stripped = data.strip()
    if not stripped:
        return ()
    if stripped.startswith("{"):
        end = stripped.find("}")
        if end > 0:
            return (stripped[1:end],)
    if Path(stripped).exists():
        return (stripped,)
    return (stripped.split(maxsplit=1)[0],)


def _folder_from_drop_items(items: tuple[str, ...] | list[str]) -> Path | None:
    if not items:
        return None

    path = Path(str(items[0]))
    if path.is_file():
        return path.parent
    if path.is_dir():
        return path
    return None


if _DND_AVAILABLE:

    class _AppBase(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[union-attr]
        pass

else:

    class _AppBase(ctk.CTk):
        pass


class App(_AppBase):
    def __init__(
        self,
        config_path: Path | None = None,
        engine_factory: EngineFactory | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self._config_path = (
            default_config_path() if config_path is None else Path(config_path)
        )
        bootstrap_frozen_config(self._config_path)
        self._engine_factory = engine_factory
        self._client_factory = client_factory
        self._queue: queue.Queue[GuiEvent] = queue.Queue()
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._running = False
        self._watch_running = False
        self._closed = False
        self._after_id: str | None = None
        self._file_rows: dict[str, str] = {}
        self._dnd_enabled = False

        gui_config = self._load_initial_gui_config()
        initial_appearance = gui_config.appearance
        ctk.set_appearance_mode(initial_appearance)

        self.folder_var = tk.StringVar()
        self.status_var = tk.StringVar(value="選擇資料夾後開始")
        self.auto_csv_var = tk.BooleanVar(value=True)
        self.force_var = tk.BooleanVar(value=False)
        self.watch_var = tk.BooleanVar(value=False)

        self.title(f"pdf-ocrer {__version__}")
        self.minsize(860, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_widgets(initial_appearance)
        self._set_running(False)
        self._setup_drag_drop()
        if not self._dnd_enabled:
            self._append_log("拖放功能不可用")
        self._after_id = self.after(100, self._poll_queue)

    def destroy(self) -> None:
        self._closed = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        super().destroy()

    def _load_initial_gui_config(self) -> GuiConfig:
        try:
            cfg = load_config(self._config_path)
            setup_logging(cfg.logging)
            return cfg.gui
        except ConfigError as exc:
            setup_logging(LoggingConfig())
            _logger.error("設定錯誤: %s", exc)
            return GuiConfig()

    def _build_widgets(self, initial_appearance: str) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=3)
        self.rowconfigure(5, weight=1)

        folder_frame = ctk.CTkFrame(self, fg_color="transparent")
        folder_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        folder_frame.columnconfigure(0, weight=1)

        self.folder_entry = ctk.CTkEntry(folder_frame, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.choose_button = ctk.CTkButton(
            folder_frame,
            text="選擇資料夾",
            command=self._choose_folder,
        )
        self.choose_button.grid(row=0, column=1)

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
        action_frame.columnconfigure(6, weight=1)

        self.start_button = ctk.CTkButton(action_frame, text="開始", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))

        self.force_checkbox = ctk.CTkCheckBox(
            action_frame,
            text="全部重新處理",
            variable=self.force_var,
        )
        self.force_checkbox.grid(row=0, column=1, padx=(0, 8))

        self.watch_checkbox = ctk.CTkCheckBox(
            action_frame,
            text="監看模式",
            variable=self.watch_var,
            command=self._toggle_watch_mode,
        )
        self.watch_checkbox.grid(row=0, column=2, padx=(0, 8))

        self.cancel_button = ctk.CTkButton(action_frame, text="取消", command=self._cancel)
        self.cancel_button.grid(row=0, column=3, padx=(0, 8))

        self.edit_prompt_button = ctk.CTkButton(
            action_frame,
            text="編輯命名規則",
            command=self._edit_prompt,
        )
        self.edit_prompt_button.grid(row=0, column=4, padx=(16, 8))

        self.edit_config_button = ctk.CTkButton(
            action_frame,
            text="編輯設定",
            command=self._open_settings_dialog,
        )
        self.edit_config_button.grid(row=0, column=5)

        self.auto_csv_checkbox = ctk.CTkCheckBox(
            action_frame,
            text="完成後開啟對照表",
            variable=self.auto_csv_var,
        )
        self.auto_csv_checkbox.grid(row=0, column=7, sticky="e", padx=(8, 16))

        self.theme_segmented = ctk.CTkSegmentedButton(
            action_frame,
            values=["淺色", "深色", "系統"],
            command=self._change_appearance,
        )
        self.theme_segmented.grid(row=0, column=8, sticky="e")
        self.theme_segmented.set(_APPEARANCE_TO_SEGMENT.get(initial_appearance, "系統"))

        progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        progress_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        progress_frame.columnconfigure(0, weight=1)

        self.progressbar = ctk.CTkProgressBar(progress_frame)
        self.progressbar.grid(row=0, column=0, sticky="ew")
        self.progressbar.set(0)

        self.status_label = ctk.CTkLabel(self, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))

        table_frame = ctk.CTkFrame(self, fg_color="transparent")
        table_frame.grid(row=4, column=0, sticky="nsew", padx=12, pady=(0, 8))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        _style_treeview(initial_appearance)
        self.file_tree = ttk.Treeview(
            table_frame,
            columns=_TREE_COLUMNS,
            show="headings",
            style="PdfOcrer.Treeview",
        )
        self.file_tree.heading("source", text="原檔名")
        self.file_tree.heading("status", text="狀態")
        self.file_tree.heading("output", text="新檔名")
        self.file_tree.heading("ocr_pages", text="OCR頁數")
        self.file_tree.column("source", width=260, anchor="w")
        self.file_tree.column("status", width=150, anchor="w")
        self.file_tree.column("output", width=260, anchor="w")
        self.file_tree.column("ocr_pages", width=90, anchor="center")
        self.file_tree.grid(row=0, column=0, sticky="nsew")

        tree_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.file_tree.yview)
        tree_scrollbar.grid(row=0, column=1, sticky="ns")
        self.file_tree.configure(yscrollcommand=tree_scrollbar.set)

        self.log_text = ctk.CTkTextbox(self, height=130, wrap="word", state="disabled")
        self.log_text.grid(row=5, column=0, sticky="nsew", padx=12, pady=(0, 12))

    def _setup_drag_drop(self) -> None:
        if not _DND_AVAILABLE or TkinterDnD is None or DND_FILES is None:
            return

        try:
            self.TkdndVersion = TkinterDnD._require(self)
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            self._dnd_enabled = False
            return

        self._dnd_enabled = True

    def _on_drop(self, event: object) -> None:
        data = getattr(event, "data", "")
        try:
            items = self.tk.splitlist(data)
        except tk.TclError:
            folder = _folder_from_drop_data(data)
        else:
            folder = _folder_from_drop_items(items)

        if folder is not None:
            self.folder_var.set(str(folder))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self, title="選擇 PDF 資料夾")
        if selected:
            self.folder_var.set(selected)

    def _start(self) -> None:
        folder = Path(self.folder_var.get().strip())
        if not folder.exists() or not folder.is_dir():
            messagebox.showerror("資料夾錯誤", f"資料夾不存在：{folder}", parent=self)
            return

        self._clear_log()
        self._clear_file_rows()
        self.progressbar.set(0)
        self.status_var.set("準備開始")
        self._cancel_event = threading.Event()
        watch = bool(self.watch_var.get())
        force = False if watch else bool(self.force_var.get())
        self._watch_running = watch
        self._set_running(True)

        self._worker = threading.Thread(
            target=self._run_worker,
            args=(folder, self._cancel_event, force, watch),
            daemon=False,
        )
        self._worker.start()

    def _cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.cancel_button.configure(state="disabled")
        message = "正在停止監看..." if self._watch_running else "正在取消處理..."
        self._append_log(message)

    def _run_worker(
        self,
        folder: Path,
        cancel_event: threading.Event,
        force: bool | None = None,
        watch: bool | None = None,
    ) -> None:
        try:
            cfg = load_config(self._config_path)
            setup_logging(cfg.logging)
            log_cb = self._queue_log
            watch = self._bool_var("watch_var", False) if watch is None else watch
            force = self._bool_var("force_var", False) if force is None else force
            force = False if watch else force
            if watch and not cfg.output.incremental:
                raise ConfigError("監看模式需要增量處理（[output] incremental = true）")
            prompt_template = _load_prompt(
                resolve_prompt_path(cfg.naming.prompt_file, self._config_path)
            )
            engine = self._create_engine(cfg.ocr, log_cb)
            client = None
            if cfg.naming.enabled:
                client = (
                    self._client_factory(cfg.llm)
                    if self._client_factory is not None
                    else create_client(cfg.llm)
                )
            if watch:
                watch_summary = watch_loop(
                    folder,
                    cfg,
                    engine,
                    client,
                    prompt_template,
                    progress_cb=self._queue_progress,
                    log_cb=log_cb,
                    stop_event=cancel_event,
                    file_cb=self._queue_file_done,
                    cycle_cb=self._queue_watch_cycle,
                )
            else:
                summary = run_batch(
                    folder,
                    cfg,
                    engine,
                    client,
                    prompt_template,
                    progress_cb=self._queue_progress,
                    log_cb=log_cb,
                    cancel_event=cancel_event,
                    file_cb=self._queue_file_done,
                    force=force,
                )
        except Exception as exc:
            _logger.exception("GUI worker failed")
            self._queue.put(("error", f"{type(exc).__name__}: {exc}"))
        else:
            if watch:
                self._queue.put(("watch_done", watch_summary))
            else:
                self._queue.put(("done", summary))

    def _create_engine(
        self,
        cfg: OcrConfig,
        log_cb: Callable[[str], None],
    ) -> OcrEngineProtocol:
        if self._engine_factory is not None:
            return self._engine_factory(cfg)

        return create_engine(cfg, log_cb)

    def _queue_log(self, message: str) -> None:
        self._queue.put(("log", message))

    def _queue_progress(
        self,
        file_i: int,
        file_n: int,
        page_i: int,
        page_n: int,
        name: str,
    ) -> None:
        self._queue.put(("progress", file_i, file_n, page_i, page_n, name))

    def _queue_file_done(self, result: FileResult) -> None:
        self._queue.put(("file_done", result))

    def _queue_watch_cycle(self, index: int, ready_count: int, cumulative: int) -> None:
        self._queue.put(("watch_cycle", index, ready_count, cumulative))

    def _poll_queue(self) -> None:
        self._drain_queue()
        if not self._closed:
            self._after_id = self.after(100, self._poll_queue)

    def _drain_queue(self) -> None:
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break

            if not event:
                continue

            kind = event[0]
            if kind == "log":
                self._append_log(str(event[1]))
            elif kind == "progress":
                self._handle_progress(event)
            elif kind == "file_done":
                self._handle_file_done(event[1])
            elif kind == "done":
                self._handle_done(event[1])
            elif kind == "watch_cycle":
                self._handle_watch_cycle(event)
            elif kind == "watch_done":
                self._handle_watch_done(event[1])
            elif kind == "error":
                self._handle_error(str(event[1]))

    def _handle_progress(self, event: GuiEvent) -> None:
        file_i, file_n, page_i, page_n, name = event[1:6]
        denominator = int(file_n)
        fraction = 0.0 if denominator < 1 else int(file_i) / denominator
        self.progressbar.set(max(0.0, min(1.0, fraction)))
        self.status_var.set(f"第 {file_i}/{file_n} 檔 - 第 {page_i}/{page_n} 頁 - {name}")
        self._upsert_file_row(str(name), "處理中", "", "")

    def _handle_file_done(self, result: object) -> None:
        if not isinstance(result, FileResult):
            self._handle_error("GUI 收到未知的檔案完成事件。")
            return

        self._upsert_file_row(
            _result_source_name(result),
            result.status.value,
            _result_output_name(result),
            str(result.ocr_pages),
        )

    def _handle_done(self, summary: object) -> None:
        if not isinstance(summary, BatchSummary):
            self._handle_error("GUI 收到未知的完成事件。")
            return

        was_running = self._running
        self._set_running(False)
        self.progressbar.set(1.0 if summary.results else 0.0)
        self.status_var.set("已取消" if summary.cancelled else f"完成：{len(summary.results)} 檔")
        if self.auto_csv_var.get() and summary.csv_path is not None and summary.csv_path.exists():
            self._open_path(summary.csv_path)
        if was_running:
            self.after(0, lambda: self._show_completion(summary))

    def _handle_watch_cycle(self, event: GuiEvent) -> None:
        index, _ready_count, cumulative = event[1:4]
        self.status_var.set(f"監看中（第 {index} 輪，累計處理 {cumulative} 檔）")

    def _handle_watch_done(self, summary: object) -> None:
        if not isinstance(summary, WatchSummary):
            self._handle_error("GUI 收到未知的監看完成事件。")
            return

        self._set_running(False)
        self.progressbar.set(0)
        message = f"監看已停止：{summary.cycles} 輪，累計處理 {summary.total_processed} 檔"
        self.status_var.set(message)
        self._append_log(message)

    def _handle_error(self, message: str) -> None:
        was_running = self._running
        self._set_running(False)
        self.status_var.set("發生錯誤")
        self._append_log(message)
        if was_running:
            self.after(0, lambda: messagebox.showerror("錯誤", message, parent=self))

    def _set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self.start_button.configure(state="disabled")
            self.cancel_button.configure(
                state="normal",
                text="停止監看" if self._watch_running else "取消",
            )
        else:
            self.start_button.configure(state="normal")
            self.cancel_button.configure(state="disabled", text="取消")
        self._sync_mode_controls()
        if not running:
            self._watch_running = False

    def _toggle_watch_mode(self) -> None:
        self._sync_mode_controls()

    def _sync_mode_controls(self) -> None:
        watch_selected = bool(self.watch_var.get())
        if watch_selected:
            self.force_var.set(False)
        if self._running:
            force_state = "disabled"
            watch_state = "disabled"
        else:
            force_state = "disabled" if watch_selected else "normal"
            watch_state = "normal"
        self.force_checkbox.configure(state=force_state)
        self.watch_checkbox.configure(state=watch_state)

    def _bool_var(self, name: str, default: bool) -> bool:
        var = self.__dict__.get(name)
        if var is None:
            return default
        return bool(var.get())

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _clear_file_rows(self) -> None:
        for iid in self.file_tree.get_children():
            self.file_tree.delete(iid)
        self._file_rows.clear()

    def _upsert_file_row(
        self,
        source_name: str,
        status: str,
        output_name: str,
        ocr_pages: str,
    ) -> None:
        iid = self._file_rows.get(source_name)
        values = (source_name, status, output_name, ocr_pages)
        if iid is None:
            iid = f"file-{len(self._file_rows)}"
            self._file_rows[source_name] = iid
            self.file_tree.insert("", "end", iid=iid, values=values)
            return

        self.file_tree.item(iid, values=values)

    def _change_appearance(self, selected: str) -> None:
        mode = _SEGMENT_TO_APPEARANCE.get(selected, "system")
        ctk.set_appearance_mode(mode)
        _style_treeview(mode)

    def _show_completion(self, summary: BatchSummary) -> None:
        if self._closed:
            return
        if messagebox.askyesno("處理完成", self._completion_message(summary), parent=self):
            self._open_path(summary.output_dir)

    def _completion_message(self, summary: BatchSummary) -> str:
        failed = sum(result.status is FileStatus.FAILED for result in summary.results)
        skipped = sum(result.status is FileStatus.SKIPPED_ENCRYPTED for result in summary.results)
        skipped_done = sum(result.status is FileStatus.SKIPPED_DONE for result in summary.results)
        success = len(summary.results) - failed - skipped - skipped_done
        state = "已取消" if summary.cancelled else "完成"
        csv_path = "無" if summary.csv_path is None else str(summary.csv_path)
        csv_note = (
            "本次無新處理檔案\n"
            if summary.csv_path is None and not summary.cancelled
            else ""
        )
        return (
            f"狀態：{state}\n"
            f"成功：{success}\n"
            f"跳過（加密）：{skipped}\n"
            f"已跳過（先前已處理）：{skipped_done}\n"
            f"失敗：{failed}\n"
            f"CSV：{csv_path}\n\n"
            f"{csv_note}"
            "開啟輸出資料夾？"
        )

    def _edit_prompt(self) -> None:
        try:
            cfg = load_config(self._config_path)
        except ConfigError as exc:
            messagebox.showerror("設定錯誤", str(exc), parent=self)
            return

        prompt_path = resolve_prompt_path(cfg.naming.prompt_file, self._config_path)
        if not prompt_path.exists():
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(DEFAULT_NAMING_PROMPT, encoding="utf-8")
        self._open_path(prompt_path)

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self, self._config_path, open_path=self._open_path)
        dialog.grab_set()
        self.wait_window(dialog)

    def _open_path(self, path: Path) -> None:
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            self._append_log(f"無法自動開啟：{path}")
            return

        try:
            startfile(str(path))
        except OSError as exc:
            messagebox.showerror("開啟失敗", str(exc), parent=self)

    def _on_close(self) -> None:
        if self._running:
            title = "停止監看" if self._watch_running else "取消處理"
            message = (
                "監看仍在進行，要停止並關閉嗎？"
                if self._watch_running
                else "處理仍在進行，要取消並關閉嗎？"
            )
            confirmed = messagebox.askokcancel(
                title,
                message,
                parent=self,
            )
            if not confirmed:
                return
            if self._cancel_event is not None:
                self._cancel_event.set()
        self.destroy()


def _result_source_name(result: FileResult) -> str:
    return result.rel or result.source.name


def _result_output_name(result: FileResult) -> str:
    if result.output is None:
        return ""

    if not result.rel:
        return result.output.name

    parent = PurePosixPath(result.rel).parent
    if parent.as_posix() == ".":
        return result.output.name
    return (parent / result.output.name).as_posix()
