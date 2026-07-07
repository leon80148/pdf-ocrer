from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import numpy as np
import pytest

from pdf_ocrer.config import ConfigError, OcrConfig
from pdf_ocrer.ocr_engine import PaddleOcrEngine, create_engine, lines_from_prediction


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


def test_create_engine_dispatches_paddle() -> None:
    engine = create_engine(replace(OcrConfig(), engine="paddle"))

    assert isinstance(engine, PaddleOcrEngine)


def test_create_engine_dispatches_rapidocr_fake_module(monkeypatch) -> None:
    logs: list[str] = []

    class FakeRapidOcrEngine:
        def __init__(self, cfg: OcrConfig, log: object = None) -> None:
            self.cfg = cfg
            self.log = log

    fake_module = ModuleType("pdf_ocrer.rapidocr_engine")
    fake_module.RapidOcrEngine = FakeRapidOcrEngine
    monkeypatch.setitem(sys.modules, "pdf_ocrer.rapidocr_engine", fake_module)

    cfg = replace(OcrConfig(), engine="rapidocr")
    engine = create_engine(cfg, logs.append)

    assert isinstance(engine, FakeRapidOcrEngine)
    assert engine.cfg is cfg
    assert engine.log == logs.append


def test_create_engine_rapidocr_missing_dependency_raises_config_error(monkeypatch) -> None:
    import pdf_ocrer.rapidocr_engine as rapidocr_engine

    monkeypatch.setattr(rapidocr_engine, "_rapidocr_available", lambda: False)

    with pytest.raises(ConfigError, match=r"pip install pdf-ocrer\[rapidocr\]"):
        create_engine(replace(OcrConfig(), engine="rapidocr"))


def test_create_engine_rejects_unknown_engine_defensively() -> None:
    with pytest.raises(ConfigError, match="engine"):
        create_engine(replace(OcrConfig(), engine="unknown"))


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
        cpu_threads=4,
        textline_orientation=False,
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
        "use_textline_orientation": False,
        "device": "cpu",
        "enable_mkldnn": False,
        "cpu_threads": 4,
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
    assert init_kwargs["use_textline_orientation"] is True
    assert "cpu_threads" not in init_kwargs
    assert "text_detection_model_name" not in init_kwargs
    assert "text_recognition_model_name" not in init_kwargs
    assert predict_kwargs == [{}]


def test_bench_build_engine_uses_factory_and_forwards_selected_knobs(monkeypatch) -> None:
    import scripts.bench_ocr as bench_ocr

    created_configs: list[OcrConfig] = []
    sentinel = object()

    def fake_create_engine(cfg: OcrConfig) -> object:
        created_configs.append(cfg)
        return sentinel

    monkeypatch.setattr(bench_ocr, "create_engine", fake_create_engine)
    args = SimpleNamespace(
        engine="rapidocr",
        dpi=250,
        mkldnn="on",
        textline="off",
        cpu_threads=3,
        det_limit=960,
        det_model="det",
        rec_model="rec",
    )

    assert bench_ocr.build_engine(args) is sentinel

    assert created_configs == [
        replace(
            OcrConfig(),
            engine="rapidocr",
            dpi=250,
            lang="chinese_cht",
            enable_mkldnn=True,
            textline_orientation=False,
            cpu_threads=3,
            det_limit_side_len=960,
            det_model_name="det",
            rec_model_name="rec",
        )
    ]


def test_bench_summary_warns_about_paddle_only_options_for_rapidocr() -> None:
    import scripts.bench_ocr as bench_ocr

    args = SimpleNamespace(
        engine="rapidocr",
        mkldnn="on",
        det_model="det",
        rec_model=None,
        out=Path("bench.csv"),
    )
    row = {
        "label": "local",
        "engine": "rapidocr",
        "pages": "2",
        "repeat": "1",
        "init_s": "0.1000",
        "render_ms_med": "1.00",
        "ocr_s_med": "0.2000",
        "ocr_s_p95": "0.2000",
        "lines_mean": "1.00",
        "conf_mean": "0.9000",
        "rss_peak_mb": "100.0",
        "gt_recall": "1/1",
        "trad_ok": "True",
        "similarity": "",
    }

    warnings = [
        line
        for line in bench_ocr._summary_lines(args, row)
        if line.startswith("warning=")
    ]

    assert any("paddle-only" in line and "rapidocr" in line for line in warnings)
