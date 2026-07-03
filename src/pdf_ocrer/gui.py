from __future__ import annotations

import os
import queue
import shutil
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from pdf_ocrer import __version__
from pdf_ocrer.cli import DEFAULT_NAMING_PROMPT, _load_prompt
from pdf_ocrer.config import ConfigError, LlmConfig, OcrConfig, load_config
from pdf_ocrer.llm_providers import LLMClient, create_client
from pdf_ocrer.ocr_engine import OcrEngineProtocol
from pdf_ocrer.pipeline import BatchSummary, FileStatus, run_batch

EngineFactory = Callable[[OcrConfig], OcrEngineProtocol]
ClientFactory = Callable[[LlmConfig], LLMClient | None]
GuiEvent = tuple[object, ...]


def run_gui(config_path: Path | None = None) -> None:
    app = App(config_path=config_path)
    app.mainloop()


class App(tk.Tk):
    def __init__(
        self,
        config_path: Path | None = None,
        engine_factory: EngineFactory | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__()
        self._config_path = Path("config.toml") if config_path is None else Path(config_path)
        self._engine_factory = engine_factory
        self._client_factory = client_factory
        self._queue: queue.Queue[GuiEvent] = queue.Queue()
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._running = False
        self._closed = False
        self._after_id: str | None = None

        self.folder_var = tk.StringVar()
        self.status_var = tk.StringVar(value="選擇資料夾後開始")

        self.title(f"pdf-ocrer {__version__}")
        self.minsize(720, 520)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_widgets()
        self._set_running(False)
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

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        folder_frame = ttk.Frame(self, padding=(12, 12, 12, 6))
        folder_frame.grid(row=0, column=0, sticky="ew")
        folder_frame.columnconfigure(0, weight=1)

        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.choose_button = ttk.Button(
            folder_frame,
            text="選擇資料夾",
            command=self._choose_folder,
        )
        self.choose_button.grid(row=0, column=1)

        action_frame = ttk.Frame(self, padding=(12, 0, 12, 6))
        action_frame.grid(row=1, column=0, sticky="ew")

        self.start_button = ttk.Button(action_frame, text="開始", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))

        self.cancel_button = ttk.Button(action_frame, text="取消", command=self._cancel)
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))

        self.edit_prompt_button = ttk.Button(
            action_frame,
            text="編輯命名規則",
            command=self._edit_prompt,
        )
        self.edit_prompt_button.grid(row=0, column=2, padx=(16, 8))

        self.edit_config_button = ttk.Button(
            action_frame,
            text="編輯設定",
            command=self._edit_config,
        )
        self.edit_config_button.grid(row=0, column=3)

        progress_frame = ttk.Frame(self, padding=(12, 0, 12, 6))
        progress_frame.grid(row=2, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)

        self.progressbar = ttk.Progressbar(progress_frame, mode="determinate", maximum=1)
        self.progressbar.grid(row=0, column=0, sticky="ew")

        self.status_label = ttk.Label(self, textvariable=self.status_var, padding=(12, 0, 12, 6))
        self.status_label.grid(row=3, column=0, sticky="ew")

        self.log_text = scrolledtext.ScrolledText(self, height=16, wrap="word", state="disabled")
        self.log_text.grid(row=4, column=0, sticky="nsew", padx=12, pady=(0, 12))

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
        self.progressbar.configure(maximum=1, value=0)
        self.status_var.set("準備開始")
        self._cancel_event = threading.Event()
        self._set_running(True)

        self._worker = threading.Thread(
            target=self._run_worker,
            args=(folder, self._cancel_event),
            daemon=False,
        )
        self._worker.start()

    def _cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.cancel_button.state(["disabled"])
        self._append_log("正在取消處理...")

    def _run_worker(self, folder: Path, cancel_event: threading.Event) -> None:
        try:
            cfg = load_config(self._config_path)
            log_cb = self._queue_log
            prompt_template = _load_prompt(Path(cfg.naming.prompt_file))
            engine = self._create_engine(cfg.ocr, log_cb)
            client = None
            if cfg.naming.enabled:
                client = (
                    self._client_factory(cfg.llm)
                    if self._client_factory is not None
                    else create_client(cfg.llm)
                )
            summary = run_batch(
                folder,
                cfg,
                engine,
                client,
                prompt_template,
                progress_cb=self._queue_progress,
                log_cb=log_cb,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            self._queue.put(("error", f"{type(exc).__name__}: {exc}"))
        else:
            self._queue.put(("done", summary))

    def _create_engine(
        self,
        cfg: OcrConfig,
        log_cb: Callable[[str], None],
    ) -> OcrEngineProtocol:
        if self._engine_factory is not None:
            return self._engine_factory(cfg)

        from pdf_ocrer.ocr_engine import PaddleOcrEngine

        return PaddleOcrEngine(cfg, log=log_cb)

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
            elif kind == "done":
                self._handle_done(event[1])
            elif kind == "error":
                self._handle_error(str(event[1]))

    def _handle_progress(self, event: GuiEvent) -> None:
        file_i, file_n, page_i, page_n, name = event[1:6]
        self.progressbar.configure(maximum=max(1, int(file_n)), value=int(file_i))
        self.status_var.set(f"第 {file_i}/{file_n} 檔 - 第 {page_i}/{page_n} 頁 - {name}")

    def _handle_done(self, summary: object) -> None:
        if not isinstance(summary, BatchSummary):
            self._handle_error("GUI 收到未知的完成事件。")
            return

        was_running = self._running
        self._set_running(False)
        self.progressbar.configure(maximum=max(1, len(summary.results)), value=len(summary.results))
        self.status_var.set("已取消" if summary.cancelled else f"完成：{len(summary.results)} 檔")
        if was_running:
            self.after(0, lambda: self._show_completion(summary))

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
            self.start_button.state(["disabled"])
            self.cancel_button.state(["!disabled"])
        else:
            self.start_button.state(["!disabled"])
            self.cancel_button.state(["disabled"])

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _show_completion(self, summary: BatchSummary) -> None:
        if self._closed:
            return
        if messagebox.askyesno("處理完成", self._completion_message(summary), parent=self):
            self._open_path(summary.output_dir)

    def _completion_message(self, summary: BatchSummary) -> str:
        failed = sum(result.status is FileStatus.FAILED for result in summary.results)
        skipped = sum(result.status is FileStatus.SKIPPED_ENCRYPTED for result in summary.results)
        success = len(summary.results) - failed - skipped
        state = "已取消" if summary.cancelled else "完成"
        csv_path = "無" if summary.csv_path is None else str(summary.csv_path)
        return (
            f"狀態：{state}\n"
            f"成功：{success}\n"
            f"跳過：{skipped}\n"
            f"失敗：{failed}\n"
            f"CSV：{csv_path}\n\n"
            "開啟輸出資料夾？"
        )

    def _edit_prompt(self) -> None:
        try:
            cfg = load_config(self._config_path)
        except ConfigError as exc:
            messagebox.showerror("設定錯誤", str(exc), parent=self)
            return

        prompt_path = Path(cfg.naming.prompt_file)
        if not prompt_path.exists():
            prompt_path.write_text(DEFAULT_NAMING_PROMPT, encoding="utf-8")
        self._open_path(prompt_path)

    def _edit_config(self) -> None:
        if not self._config_path.exists():
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            example_path = Path("config.example.toml")
            if example_path.exists():
                shutil.copyfile(example_path, self._config_path)
            else:
                self._config_path.write_text("", encoding="utf-8")
        self._open_path(self._config_path)

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
            confirmed = messagebox.askokcancel(
                "取消處理",
                "處理仍在進行，要取消並關閉嗎？",
                parent=self,
            )
            if not confirmed:
                return
            if self._cancel_event is not None:
                self._cancel_event.set()
        self.destroy()
