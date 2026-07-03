from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from pdf_ocrer.config import OcrConfig


@dataclass(frozen=True)
class OcrLine:
    text: str
    poly: tuple[tuple[float, float], ...]
    score: float


class OcrEngineProtocol(Protocol):
    def recognize(self, img_rgb: np.ndarray) -> list[OcrLine]: ...


def lines_from_prediction(pred: Mapping[str, Any], min_confidence: float) -> list[OcrLine]:
    lines: list[OcrLine] = []
    texts = pred["rec_texts"]
    scores = pred["rec_scores"]
    polys = pred["rec_polys"]

    for text, score, poly in zip(texts, scores, polys, strict=True):
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


class PaddleOcrEngine:
    def __init__(self, cfg: OcrConfig, log: Callable[[str], None] | None = None):
        self._cfg = cfg
        self._log = log
        self._ocr: Any | None = None

    def recognize(self, img_rgb: np.ndarray) -> list[OcrLine]:
        ocr = self._get_ocr()
        predict_kwargs: dict[str, object] = {}
        if self._cfg.det_limit_side_len is not None:
            predict_kwargs["text_det_limit_side_len"] = self._cfg.det_limit_side_len

        results = ocr.predict(img_rgb, **predict_kwargs)
        if not results:
            return []

        return lines_from_prediction(results[0], self._cfg.min_confidence)

    def _get_ocr(self) -> Any:
        if self._ocr is None:
            if self._log is not None:
                self._log("正在載入 OCR 模型…")

            from paddleocr import PaddleOCR

            kwargs: dict[str, object] = {
                "lang": self._cfg.lang,
                "ocr_version": "PP-OCRv6",
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": True,
                "device": self._cfg.device,
                "enable_mkldnn": self._cfg.enable_mkldnn,
            }
            if self._cfg.det_model_name is not None:
                kwargs["text_detection_model_name"] = self._cfg.det_model_name
            if self._cfg.rec_model_name is not None:
                kwargs["text_recognition_model_name"] = self._cfg.rec_model_name

            self._ocr = PaddleOCR(**kwargs)

        return self._ocr
