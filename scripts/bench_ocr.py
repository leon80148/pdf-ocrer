"""Benchmark OCR engine performance.

Usage examples:
    python scripts/bench_ocr.py --synthetic --pages 4 --repeat 1 --label local
    python scripts/bench_ocr.py --pdf sample.pdf --out bench_results.csv --dump-text text.txt
"""

from __future__ import annotations

import argparse
import csv
import difflib
import importlib.metadata
import math
import os
import platform
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import psutil
import pymupdf

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "tests"):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from fixtures_gen import GT_LINES, _append_scanned_page  # noqa: E402
from pdf_ocrer.config import OcrConfig  # noqa: E402
from pdf_ocrer.ocr_engine import OcrEngineProtocol, OcrLine, create_engine  # noqa: E402
from pdf_ocrer.pdf_processor import render_page  # noqa: E402

CSV_FIELDS = [
    "label",
    "engine",
    "dpi",
    "mkldnn",
    "textline",
    "cpu_threads",
    "det_limit",
    "det_model",
    "rec_model",
    "pages",
    "repeat",
    "paddlepaddle_ver",
    "paddleocr_ver",
    "cpu_model",
    "timestamp",
    "init_s",
    "render_ms_med",
    "ocr_s_med",
    "ocr_s_p95",
    "lines_mean",
    "conf_mean",
    "rss_peak_mb",
    "gt_recall",
    "trad_ok",
    "similarity",
]


@dataclass(frozen=True)
class Measurement:
    row: dict[str, str]
    summary_lines: list[str]


def build_engine(args: argparse.Namespace) -> OcrEngineProtocol:
    cfg = replace(
        OcrConfig(),
        engine=args.engine,
        dpi=args.dpi,
        lang="chinese_cht",
        enable_mkldnn=args.mkldnn == "on",
        cpu_threads=args.cpu_threads,
        textline_orientation=args.textline == "on",
        det_limit_side_len=args.det_limit or None,
        det_model_name=args.det_model,
        rec_model_name=args.rec_model,
    )
    return create_engine(cfg)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    try:
        measurement = run_benchmark(args)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")

    _append_csv(Path(args.out), measurement.row)
    for line in measurement.summary_lines:
        print(line)
    return 0


def run_benchmark(args: argparse.Namespace) -> Measurement:
    with ExitStack() as stack:
        pdf_paths = _resolve_pdf_paths(args, stack)
        stats = _measure_pdfs(args, pdf_paths)

    recognized_text = "\n".join(stats["texts"])
    if args.dump_text is not None:
        _write_text(Path(args.dump_text), recognized_text)

    similarity = ""
    if args.baseline is not None:
        baseline = Path(args.baseline).read_text(encoding="utf-8")
        similarity = _fmt(difflib.SequenceMatcher(None, baseline, recognized_text).ratio(), 4)

    gt_recall = ""
    trad_ok = ""
    if args.pdf is None:
        gt_recall = _gt_recall(recognized_text)
        trad_ok = str(_trad_ok(recognized_text))

    row = _build_row(args, stats, gt_recall, trad_ok, similarity)
    return Measurement(row=row, summary_lines=_summary_lines(args, row))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark pdf-ocrer OCR performance.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pdf", type=Path, help="PDF file or folder containing *.pdf files.")
    source.add_argument("--synthetic", action="store_true", help="Generate a synthetic scanned PDF.")
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument("--engine", default="paddle")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--mkldnn", choices=["off", "on"], default="off")
    parser.add_argument("--textline", choices=["on", "off"], default="on")
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--det-limit", type=int, default=0)
    parser.add_argument("--det-model")
    parser.add_argument("--rec-model")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path, default=Path("bench_results.csv"))
    parser.add_argument("--dump-text", type=Path)
    parser.add_argument("--baseline", type=Path)
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.pages <= 0:
        parser.error("--pages must be greater than 0")
    if args.dpi <= 0:
        parser.error("--dpi must be greater than 0")
    if args.cpu_threads < 0:
        parser.error("--cpu-threads must be 0 or greater")
    if args.det_limit < 0:
        parser.error("--det-limit must be 0 or greater")
    if args.repeat <= 0:
        parser.error("--repeat must be greater than 0")
    if args.baseline is not None and not args.baseline.is_file():
        parser.error(f"--baseline does not exist or is not a file: {args.baseline}")


def _resolve_pdf_paths(args: argparse.Namespace, stack: ExitStack) -> list[Path]:
    if args.pdf is None:
        tmp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        pdf_path = tmp_dir / "synthetic.pdf"
        _build_synthetic_pdf(pdf_path, args.pages)
        return [pdf_path]

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    if pdf_path.is_dir():
        paths = sorted(path for path in pdf_path.glob("*.pdf") if path.is_file())
        if not paths:
            raise ValueError(f"no PDF files found in folder: {pdf_path}")
        return paths
    if not pdf_path.is_file():
        raise ValueError(f"not a file: {pdf_path}")
    return [pdf_path]


def _build_synthetic_pdf(path: Path, pages: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    try:
        for _ in range(pages):
            _append_scanned_page(doc)
        doc.save(path, garbage=3, deflate=True)
    finally:
        doc.close()


def _measure_pdfs(args: argparse.Namespace, pdf_paths: list[Path]) -> dict[str, object]:
    process = psutil.Process()
    engine: OcrEngineProtocol | None = None
    init_s: float | None = None
    page_count = 0
    render_ms: list[float] = []
    ocr_s: list[float] = []
    line_counts: list[int] = []
    scores: list[float] = []
    texts: list[str] = []
    rss_samples: list[float] = []

    for pdf_path in pdf_paths:
        doc = pymupdf.open(pdf_path)
        try:
            if doc.needs_pass:
                doc.authenticate("")
                if doc.is_encrypted:
                    raise ValueError(f"PDF requires a password: {pdf_path}")

            for page_index in range(doc.page_count):
                page_count += 1
                page = doc[page_index]
                render_start = time.perf_counter()
                img_rgb = render_page(page, args.dpi)
                render_ms.append((time.perf_counter() - render_start) * 1000)

                timed_runs = args.repeat
                page_text_recorded = False
                if engine is None:
                    init_start = time.perf_counter()
                    engine = build_engine(args)
                    warmup_lines = engine.recognize(img_rgb)
                    init_s = time.perf_counter() - init_start
                    _record_lines(warmup_lines, line_counts, scores, texts)
                    page_text_recorded = True
                    timed_runs -= 1

                for _ in range(timed_runs):
                    ocr_start = time.perf_counter()
                    lines = engine.recognize(img_rgb)
                    ocr_s.append(time.perf_counter() - ocr_start)
                    _record_lines(
                        lines,
                        line_counts,
                        scores,
                        texts if not page_text_recorded else None,
                    )
                    page_text_recorded = True

                rss_samples.append(_rss_mb(process))
                del img_rgb
        finally:
            doc.close()

    if page_count == 0 or init_s is None:
        raise ValueError("no pages were available for benchmarking")

    return {
        "init_s": init_s,
        "render_ms": render_ms,
        "ocr_s": ocr_s,
        "line_counts": line_counts,
        "scores": scores,
        "texts": texts,
        "rss_peak_mb": max(rss_samples, default=_rss_mb(process)),
        "pages": page_count,
    }


def _record_lines(
    lines: list[OcrLine],
    line_counts: list[int],
    scores: list[float],
    texts: list[str] | None,
) -> None:
    line_counts.append(len(lines))
    scores.extend(line.score for line in lines)
    if texts is not None:
        texts.append("\n".join(line.text for line in lines))


def _rss_mb(process: psutil.Process) -> float:
    rss = _process_rss(process)
    for child in process.children(recursive=True):
        rss += _process_rss(child)
    return rss / (1024 * 1024)


def _process_rss(process: psutil.Process) -> int:
    try:
        return process.memory_info().rss
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return 0


def _build_row(
    args: argparse.Namespace,
    stats: dict[str, object],
    gt_recall: str,
    trad_ok: str,
    similarity: str,
) -> dict[str, str]:
    render_ms = stats["render_ms"]
    ocr_s = stats["ocr_s"]
    line_counts = stats["line_counts"]
    scores = stats["scores"]

    return {
        "label": args.label,
        "engine": args.engine,
        "dpi": str(args.dpi),
        "mkldnn": args.mkldnn,
        "textline": args.textline,
        "cpu_threads": str(args.cpu_threads),
        "det_limit": str(args.det_limit),
        "det_model": args.det_model or "",
        "rec_model": args.rec_model or "",
        "pages": str(stats["pages"]),
        "repeat": str(args.repeat),
        "paddlepaddle_ver": _package_version("paddlepaddle"),
        "paddleocr_ver": _package_version("paddleocr"),
        "cpu_model": _cpu_model(),
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "init_s": _fmt(float(stats["init_s"]), 4),
        "render_ms_med": _fmt(_median(render_ms), 2),
        "ocr_s_med": _fmt(_median(ocr_s), 4),
        "ocr_s_p95": _fmt(_percentile(ocr_s, 0.95), 4),
        "lines_mean": _fmt(_mean(line_counts), 2),
        "conf_mean": _fmt(_mean(scores), 4),
        "rss_peak_mb": _fmt(float(stats["rss_peak_mb"]), 1),
        "gt_recall": gt_recall,
        "trad_ok": trad_ok,
        "similarity": similarity,
    }


def _summary_lines(args: argparse.Namespace, row: dict[str, str]) -> list[str]:
    lines = [
        f"label={row['label']}",
        f"engine={row['engine']}",
        f"pages={row['pages']}",
        f"repeat={row['repeat']}",
        f"init_s={row['init_s']}",
        f"render_ms_med={row['render_ms_med']}",
        f"ocr_s_med={row['ocr_s_med']}",
        f"ocr_s_p95={row['ocr_s_p95']}",
        f"lines_mean={row['lines_mean']}",
        f"conf_mean={row['conf_mean']}",
        f"rss_peak_mb={row['rss_peak_mb']}",
        f"gt_recall={row['gt_recall']}",
        f"trad_ok={row['trad_ok']}",
        f"similarity={row['similarity']}",
        f"out={Path(args.out)}",
    ]
    warning = _paddle_only_option_warning(args)
    if warning is not None:
        lines.append(warning)
    return lines


def _paddle_only_option_warning(args: argparse.Namespace) -> str | None:
    if args.engine != "rapidocr":
        return None

    ignored = []
    if args.mkldnn == "on":
        ignored.append("mkldnn")
    if args.det_model:
        ignored.append("det_model")
    if args.rec_model:
        ignored.append("rec_model")
    if not ignored:
        return None

    return f"warning=paddle-only options ignored for rapidocr: {', '.join(ignored)}"


def _append_csv(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _gt_recall(text: str) -> str:
    normalized_text = _normalize_gt_text(text)
    matched = sum(1 for _point, _size, line in GT_LINES if _normalize_gt_text(line) in normalized_text)
    return f"{matched}/{len(GT_LINES)}"


def _trad_ok(text: str) -> bool:
    return "證" in text and "证" not in text and "診" in text and "诊" not in text


def _normalize_gt_text(text: str) -> str:
    return "".join(text.replace("：", ":").split())


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _cpu_model() -> str:
    values = [
        os.environ.get("PROCESSOR_IDENTIFIER", ""),
        platform.processor(),
        platform.uname().processor,
    ]
    return next((value for value in values if value), "unknown")


def _mean(values: object) -> float:
    numbers = list(values)
    if not numbers:
        return 0.0
    return float(sum(numbers)) / len(numbers)


def _median(values: object) -> float:
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return 0.0
    mid = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[mid]
    return (numbers[mid - 1] + numbers[mid]) / 2


def _percentile(values: object, percentile: float) -> float:
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return 0.0
    if len(numbers) == 1:
        return numbers[0]
    rank = (len(numbers) - 1) * percentile
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return numbers[low]
    weight = rank - low
    return numbers[low] * (1 - weight) + numbers[high] * weight


def _fmt(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


if __name__ == "__main__":
    raise SystemExit(main())
