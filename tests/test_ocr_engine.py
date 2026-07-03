from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from pdf_ocrer.config import OcrConfig
from pdf_ocrer.ocr_engine import PaddleOcrEngine, lines_from_prediction


def test_lines_from_prediction_filters_and_converts() -> None:
    poly = np.array([[0, 0], [10, 0], [10, 5], [0, 5]], dtype=np.int16)
    pred = {
        "rec_texts": ["高", "", "  ", "低分"],
        "rec_scores": [0.9, 0.9, 0.9, 0.3],
        "rec_polys": [poly, poly, poly, poly],
    }

    lines = lines_from_prediction(pred, 0.5)

    assert [line.text for line in lines] == ["高"]
    assert lines[0].poly == ((0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0))
    assert lines[0].score == 0.9


def test_module_import_is_paddle_free() -> None:
    code = "import pdf_ocrer.ocr_engine, sys; assert 'paddleocr' not in sys.modules"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_paddle_engine_lazy_init_and_predict_kwargs(monkeypatch) -> None:
    init_kwargs: dict[str, object] = {}
    predict_kwargs: list[dict[str, object]] = []
    logs: list[str] = []
    poly = np.array([[1, 2], [3, 2], [3, 4], [1, 4]], dtype=np.int16)

    class FakePaddleOCR:
        def __init__(self, **kwargs: object) -> None:
            init_kwargs.update(kwargs)

        def predict(self, img: np.ndarray, **kwargs: object) -> list[dict[str, object]]:
            predict_kwargs.append(kwargs)
            assert img.shape == (2, 2, 3)
            return [{"rec_texts": ["診斷證明書"], "rec_scores": [0.98], "rec_polys": [poly]}]

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=FakePaddleOCR))
    cfg = replace(
        OcrConfig(),
        det_limit_side_len=1280,
        det_model_name="PP-OCRv6_small_det",
        rec_model_name="PP-OCRv6_small_rec",
    )
    engine = PaddleOcrEngine(cfg, logs.append)

    assert "paddleocr" in sys.modules
    assert init_kwargs == {}

    lines = engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8))
    lines_again = engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8))

    assert [line.text for line in lines] == ["診斷證明書"]
    assert [line.text for line in lines_again] == ["診斷證明書"]
    assert logs == ["正在載入 OCR 模型…"]
    assert init_kwargs == {
        "lang": "chinese_cht",
        "ocr_version": "PP-OCRv6",
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
        "device": "cpu",
        "enable_mkldnn": False,
        "text_detection_model_name": "PP-OCRv6_small_det",
        "text_recognition_model_name": "PP-OCRv6_small_rec",
    }
    assert predict_kwargs == [
        {"text_det_limit_side_len": 1280},
        {"text_det_limit_side_len": 1280},
    ]


def test_paddle_engine_omits_optional_kwargs_and_handles_empty_result(monkeypatch) -> None:
    init_kwargs: dict[str, object] = {}
    predict_kwargs: list[dict[str, object]] = []

    class FakePaddleOCR:
        def __init__(self, **kwargs: object) -> None:
            init_kwargs.update(kwargs)

        def predict(self, img: np.ndarray, **kwargs: object) -> list[dict[str, object]]:
            predict_kwargs.append(kwargs)
            return []

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=FakePaddleOCR))
    engine = PaddleOcrEngine(OcrConfig())

    assert engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8)) == []
    assert "text_detection_model_name" not in init_kwargs
    assert "text_recognition_model_name" not in init_kwargs
    assert predict_kwargs == [{}]
