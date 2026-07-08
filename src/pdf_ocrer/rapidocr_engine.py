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

            self._log_device_status()
            params = _rapidocr_params(self._cfg)
            if params:
                self._ocr = RapidOCR(params=params)
            else:
                self._ocr = RapidOCR()

        return self._ocr

    def _log_device_status(self) -> None:
        if self._log is None or self._cfg.device.casefold() == "cpu":
            return

        conflict = _onnxruntime_conflict_message(_installed_onnxruntime_runtimes())
        if conflict is not None:
            self._log(conflict)

        import onnxruntime

        message = _device_status_message(self._cfg.device, onnxruntime.get_available_providers())
        if message is not None:
            self._log(message)


_PROVIDER_FOR_DEVICE = {"cuda": "CUDAExecutionProvider", "dml": "DmlExecutionProvider"}
_EXTRA_FOR_DEVICE = {"cuda": "pdf-ocrer[rapidocr-gpu-cuda]", "dml": "pdf-ocrer[rapidocr-gpu-dml]"}


def _rapidocr_params(cfg: OcrConfig) -> dict[str, object]:
    params: dict[str, object] = {}
    if cfg.cpu_threads > 0:
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = cfg.cpu_threads
    if cfg.det_limit_side_len is not None:
        params["Det.limit_side_len"] = cfg.det_limit_side_len
    if not cfg.textline_orientation:
        params["Global.use_cls"] = cfg.textline_orientation

    device = cfg.device.casefold()
    if device == "cuda":
        params["EngineConfig.onnxruntime.use_cuda"] = True
    elif device == "dml":
        params["EngineConfig.onnxruntime.use_dml"] = True

    if cfg.model_type != "small":
        params["Det.model_type"] = cfg.model_type
        params["Rec.model_type"] = cfg.model_type

    return params


_ONNXRUNTIME_RUNTIMES = ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml")


def _device_status_message(device: str, available_providers: list[str]) -> str | None:
    device = device.casefold()
    provider = _PROVIDER_FOR_DEVICE.get(device)
    if provider is None:
        return None

    if provider in available_providers:
        # The provider is available in the installed onnxruntime build, but the
        # ONNX session may still fall back to CPU at creation (driver/DLL/runtime
        # mismatch). Report accurately rather than claiming active GPU execution.
        return f"OCR 已啟用 GPU provider {provider}（若實際不支援會自動回退 CPU）"

    return (
        f"警告：已設定 device={device}，但目前安裝的 onnxruntime 不支援 {provider}"
        f"（可用：{', '.join(available_providers)}），將改以 CPU 執行。"
        f"請安裝對應套件：{_EXTRA_FOR_DEVICE[device]}（需先移除已安裝的 CPU 版 onnxruntime）。"
    )


def _onnxruntime_conflict_message(installed_runtimes: list[str]) -> str | None:
    if len(installed_runtimes) <= 1:
        return None
    return (
        f"警告：偵測到多個 onnxruntime 套件同時安裝（{', '.join(installed_runtimes)}），"
        "它們共用同一匯入名稱會互相衝突、可能使用到非預期的執行後端。"
        "請只保留一個：pip uninstall " + " ".join(installed_runtimes) + " 後重新安裝需要的那一個。"
    )


def _installed_onnxruntime_runtimes() -> list[str]:
    import importlib.metadata as metadata

    found = []
    for name in _ONNXRUNTIME_RUNTIMES:
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        found.append(name)
    return found


def _rapidocr_available() -> bool:
    return _module_available("rapidocr") and _module_available("onnxruntime")


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return True

    return importlib.util.find_spec(name) is not None
