from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pdf_ocrer import __version__
from pdf_ocrer.config import ConfigError, LlmConfig, OcrConfig, load_config
from pdf_ocrer.llm_providers import LLMClient, create_client
from pdf_ocrer.ocr_engine import OcrEngineProtocol
from pdf_ocrer.pipeline import BatchSummary, FileStatus, run_batch

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

    try:
        cfg = load_config(args.config)
        if args.no_llm:
            cfg = replace(cfg, llm=replace(cfg.llm, provider="none"))
        if args.dpi is not None:
            cfg = replace(cfg, ocr=replace(cfg.ocr, dpi=args.dpi))

        prompt_template = _load_prompt(Path(cfg.naming.prompt_file))
        engine = (
            engine_factory(cfg.ocr)
            if engine_factory is not None
            else _create_default_engine(cfg.ocr)
        )
        client = None
        if cfg.naming.enabled:
            client = client_factory(cfg.llm) if client_factory is not None else create_client(cfg.llm)
        summary = run_batch(
            folder,
            cfg,
            engine,
            client,
            prompt_template,
            progress_cb=_print_progress,
            log_cb=print,
        )
    except ConfigError as exc:
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
    parser.add_argument("--version", action="store_true")
    return parser


def _create_default_engine(cfg: OcrConfig) -> OcrEngineProtocol:
    from pdf_ocrer.ocr_engine import PaddleOcrEngine

    return PaddleOcrEngine(cfg)


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
                    result.source.name,
                    "" if result.output is None else result.output.name,
                    result.status.value,
                    str(result.total_pages),
                    str(result.ocr_pages),
                    result.naming_source,
                    result.note,
                ]
            )
        )
    print(f"CSV: {summary.csv_path if summary.csv_path is not None else '無'}")
