"""RapidOCR adapter.

API facts source: docs/specs/rapidocr-api-facts.md, spike date 2026-07-08.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from typing import Any

import numpy as np

from pdf_ocrer.config import ConfigError, OcrConfig
from pdf_ocrer.ocr_engine import OcrLine


def lines_from_rapidocr(output: Any, min_confidence: float) -> list[OcrLine]:
    boxes = output.boxes
    texts = output.txts
    scores = output.scores
    if boxes is None or texts is None or scores is None:
        return []

    lines: list[OcrLine] = []
    for text, score, poly in zip(texts, scores, boxes, strict=True):
        if score < min_confidence or not text.strip():
            continue

        points = np.asarray(poly, dtype=np.float64)
        lines.append(
            OcrLine(
                text=text,
                poly=tuple((float(x), float(y)) for x, y in points),
                score=float(score),
            )
        )

    return lines


class RapidOcrEngine:
    def __init__(self, cfg: OcrConfig, log: Callable[[str], None] | None = None) -> None:
        if not _rapidocr_available():
            raise ConfigError("rapidocr 引擎需要安裝額外套件：pip install pdf-ocrer[rapidocr]")

        self._cfg = cfg
        self._log = log
        self._ocr: Any | None = None

    def recognize(self, img_rgb: np.ndarray) -> list[OcrLine]:
        result = self._get_ocr()(img_rgb)
        return lines_from_rapidocr(result, self._cfg.min_confidence)

    def _get_ocr(self) -> Any:
        if self._ocr is None:
            if self._log is not None:
                self._log("正在載入 OCR 模型…")

            from rapidocr import RapidOCR

            params = _rapidocr_params(self._cfg)
            if params:
                self._ocr = RapidOCR(params=params)
            else:
                self._ocr = RapidOCR()

        return self._ocr


def _rapidocr_params(cfg: OcrConfig) -> dict[str, object]:
    params: dict[str, object] = {}
    if cfg.cpu_threads > 0:
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = cfg.cpu_threads
    if cfg.det_limit_side_len is not None:
        params["Det.limit_side_len"] = cfg.det_limit_side_len
    if not cfg.textline_orientation:
        params["Global.use_cls"] = cfg.textline_orientation

    return params


def _rapidocr_available() -> bool:
    return _module_available("rapidocr") and _module_available("onnxruntime")


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return True

    return importlib.util.find_spec(name) is not None
