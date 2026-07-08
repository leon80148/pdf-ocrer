from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pdf_ocrer import __version__
from pdf_ocrer.app_logging import setup_logging
from pdf_ocrer.config import (
    AppConfig,
    ConfigError,
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
from pdf_ocrer.watcher import WatchSummary, watch_loop

DEFAULT_NAMING_PROMPT = """你是診所行政檔案命名助手。根據下方 OCR 文字，輸出一個檔名（不含副檔名）。
格式：日期_文件類型_對象
- 日期：文件內的日期，格式 YYYYMMDD；找不到就用 $today
- 文件類型：如 診斷證明書、轉診單、檢驗報告、保險申請書、公文、收據
- 對象：病患姓名或發文機關；無法確定就省略此段
規則：只輸出檔名本身，不要任何說明、引號或副檔名；不得包含 \\ / : * ? " < > | 字元；40 字以內。
原檔名：$original_name
--- OCR 文字開始 ---
$text
--- OCR 文字結束 ---
"""

EngineFactory = Callable[[OcrConfig], OcrEngineProtocol]
ClientFactory = Callable[[LlmConfig], LLMClient | None]


def main(
    argv: list[str] | None = None,
    *,
    engine_factory: EngineFactory | None = None,
    client_factory: ClientFactory | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.dpi is not None and not 72 <= args.dpi <= 600:
        print("設定錯誤: dpi 超出範圍，應為 72–600", file=sys.stderr)
        return 1

    if args.version:
        print(f"pdf-ocrer {__version__}")
        return 0

    if args.folder is None:
        from pdf_ocrer.gui import run_gui

        run_gui()
        return 0

    folder = Path(args.folder)
    if not folder.exists() or not folder.is_dir():
        print(f"資料夾不存在: {folder}", file=sys.stderr)
        return 2

    if args.watch and args.force:
        print("監看模式不能與 --force 併用", file=sys.stderr)
        return 2

    try:
        config_path = args.config if args.config is not None else default_config_path()
        bootstrap_frozen_config(config_path)
        cfg = load_config(config_path)
        setup_logging(cfg.logging)
        if args.no_llm:
            cfg = replace(cfg, llm=replace(cfg.llm, provider="none"))
        if args.dpi is not None:
            cfg = replace(cfg, ocr=replace(cfg.ocr, dpi=args.dpi))
        if args.engine is not None:
            cfg = replace(cfg, ocr=replace(cfg.ocr, engine=args.engine))
        if args.workers is not None:
            cfg = replace(cfg, performance=replace(cfg.performance, workers=args.workers))
        if args.recursive:
            cfg = replace(cfg, input=replace(cfg.input, recursive=True))
        if args.watch and not cfg.output.incremental:
            print("監看模式需要增量處理（[output] incremental = true）", file=sys.stderr)
            return 2

        prompt_template = _load_prompt(resolve_prompt_path(cfg.naming.prompt_file, config_path))
        log_cb = print
        engine = (
            engine_factory(cfg.ocr)
            if engine_factory is not None
            else _create_default_engine(cfg.ocr, log_cb)
        )
        client = None
        if cfg.naming.enabled:
            client = client_factory(cfg.llm) if client_factory is not None else create_client(cfg.llm)
        if args.watch:
            watch_summary = _run_watch_cli(
                folder,
                cfg,
                engine,
                client,
                prompt_template,
                progress_cb=_print_progress,
                log_cb=log_cb,
            )
            _print_watch_summary(watch_summary)
            if any(result.status is FileStatus.FAILED for result in watch_summary.results):
                return 1
            return 0
        summary = run_batch(
            folder,
            cfg,
            engine,
            client,
            prompt_template,
            progress_cb=_print_progress,
            log_cb=log_cb,
            force=args.force,
        )
    except ConfigError as exc:
        setup_logging(LoggingConfig())
        logging.getLogger("pdf_ocrer").error("設定錯誤: %s", exc)
        print(f"設定錯誤: {exc}", file=sys.stderr)
        return 1

    _print_summary(summary)
    if not summary.results:
        return 2
    if any(result.status is FileStatus.FAILED for result in summary.results):
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdf-ocrer")
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--dpi", type=int)
    parser.add_argument("--engine", choices=("paddle", "rapidocr"), default=None)
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(0, 9),
        metavar="N",
        default=None,
        help="覆寫同時處理檔案數；0=auto，1=循序",
    )
    parser.add_argument("--recursive", action="store_true", help="遞迴掃描子資料夾")
    parser.add_argument("--force", action="store_true", help="忽略增量記錄，全部重新處理")
    parser.add_argument("--watch", action="store_true", help="持續監看資料夾並處理穩定的新檔案")
    parser.add_argument("--version", action="store_true")
    return parser


def _create_default_engine(
    cfg: OcrConfig,
    log: Callable[[str], None] | None = None,
) -> OcrEngineProtocol:
    return create_engine(cfg, log)


def _load_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_NAMING_PROMPT


def _print_progress(file_i: int, file_n: int, page_i: int, page_n: int, filename: str) -> None:
    print(f"[{file_i}/{file_n}] {filename} 第 {page_i}/{page_n} 頁", flush=True)


def _print_summary(summary: BatchSummary) -> None:
    print("原檔名\t新檔名\t狀態\t總頁數\tOCR頁數\t命名來源\t備註")
    for result in summary.results:
        print(
            "\t".join(
                [
                    result.rel or result.source.name,
                    "" if result.output is None else _summary_output_name(result, summary.output_dir),
                    result.status.value,
                    str(result.total_pages),
                    str(result.ocr_pages),
                    result.naming_source,
                    result.note,
                ]
            )
        )
    if summary.csv_path is not None:
        print(f"CSV: {summary.csv_path}")


def _run_watch_cli(
    folder: Path,
    cfg: AppConfig,
    engine: OcrEngineProtocol,
    client: LLMClient | None,
    prompt_template: str,
    *,
    progress_cb: Callable[[int, int, int, int, str], None] | None,
    log_cb: Callable[[str], None] | None,
) -> WatchSummary:
    stop_event = threading.Event()
    summaries: list[WatchSummary] = []
    errors: list[Exception] = []

    def target() -> None:
        try:
            summaries.append(
                watch_loop(
                    folder,
                    cfg,
                    engine,
                    client,
                    prompt_template,
                    progress_cb=progress_cb,
                    log_cb=log_cb,
                    stop_event=stop_event,
                )
            )
        except Exception as exc:  # pragma: no cover - exercised through caller behavior
            errors.append(exc)

    worker = threading.Thread(target=target, daemon=False)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.1)
    except KeyboardInterrupt:
        stop_event.set()
        worker.join()

    if errors:
        raise errors[0]
    if summaries:
        return summaries[0]
    return WatchSummary(cycles=0, total_processed=0, results=[])


def _print_watch_summary(summary: WatchSummary) -> None:
    print(f"監看輪數\t{summary.cycles}")
    print(f"累計處理檔案\t{summary.total_processed}")


def _summary_output_name(result: FileResult, output_dir: Path) -> str:
    if result.output is None:
        return ""
    try:
        return result.output.relative_to(output_dir).as_posix()
    except ValueError:
        return result.output.name
