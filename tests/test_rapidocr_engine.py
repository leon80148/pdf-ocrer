from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pdf_ocrer.config import ConfigError, OcrConfig
from pdf_ocrer.rapidocr_engine import (
    RapidOcrEngine,
    _device_status_message,
    _rapidocr_params,
    lines_from_rapidocr,
)


def test_lines_from_rapidocr_converts_output_to_ocr_lines() -> None:
    output = SimpleNamespace(
        boxes=np.array(
            [
                [[1, 2], [11, 2], [11, 7], [1, 7]],
                [[3, 4], [13, 4], [13, 9], [3, 9]],
            ],
            dtype=np.float32,
        ),
        txts=("診斷證明書", "身分證字號"),
        scores=(0.98, 0.88),
    )

    lines = lines_from_rapidocr(output, 0.5)

    assert [line.text for line in lines] == ["診斷證明書", "身分證字號"]
    assert lines[0].poly == ((1.0, 2.0), (11.0, 2.0), (11.0, 7.0), (1.0, 7.0))
    assert lines[0].score == 0.98


@pytest.mark.parametrize(
    ("boxes", "txts", "scores"),
    [
        (None, ("診斷證明書",), (0.98,)),
        (np.array([[[1, 2], [11, 2], [11, 7], [1, 7]]], dtype=np.float32), None, (0.98,)),
        (np.array([[[1, 2], [11, 2], [11, 7], [1, 7]]], dtype=np.float32), ("診斷證明書",), None),
    ],
)
def test_lines_from_rapidocr_returns_empty_when_any_output_field_is_none(
    boxes: np.ndarray | None,
    txts: tuple[str, ...] | None,
    scores: tuple[float, ...] | None,
) -> None:
    assert lines_from_rapidocr(SimpleNamespace(boxes=boxes, txts=txts, scores=scores), 0.5) == []


def test_lines_from_rapidocr_filters_low_confidence_and_blank_text() -> None:
    poly = np.array([[0, 0], [10, 0], [10, 5], [0, 5]], dtype=np.int16)
    output = SimpleNamespace(
        boxes=np.array([poly, poly, poly], dtype=np.int16),
        txts=("保留", "低分", "  "),
        scores=(0.91, 0.49, 0.99),
    )

    lines = lines_from_rapidocr(output, 0.5)

    assert [line.text for line in lines] == ["保留"]
    assert lines[0].poly == ((0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0))


def test_rapidocr_engine_lazy_init_builds_params_once_and_filters(monkeypatch) -> None:
    init_calls: list[dict[str, object]] = []
    call_count = 0
    logs: list[str] = []
    poly = np.array([[0, 0], [10, 0], [10, 5], [0, 5]], dtype=np.float32)

    class FakeRapidOCR:
        def __init__(self, **kwargs: object) -> None:
            init_calls.append(kwargs)

        def __call__(self, img: np.ndarray) -> SimpleNamespace:
            nonlocal call_count
            call_count += 1
            assert img.shape == (2, 2, 3)
            return SimpleNamespace(
                boxes=np.array([poly, poly], dtype=np.float32),
                txts=("高分", "低分"),
                scores=(0.95, 0.2),
            )

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=FakeRapidOCR))
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    cfg = replace(
        OcrConfig(),
        cpu_threads=2,
        det_limit_side_len=960,
        textline_orientation=False,
        min_confidence=0.5,
    )
    engine = RapidOcrEngine(cfg, logs.append)

    assert init_calls == []

    lines = engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8))
    lines_again = engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8))

    assert [line.text for line in lines] == ["高分"]
    assert [line.text for line in lines_again] == ["高分"]
    assert call_count == 2
    assert logs == ["正在載入 OCR 模型…"]
    assert init_calls == [
        {
            "params": {
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "Det.limit_side_len": 960,
                "Global.use_cls": False,
            }
        }
    ]


def test_rapidocr_engine_uses_no_arg_constructor_when_params_are_empty(monkeypatch) -> None:
    init_calls: list[dict[str, object]] = []

    class FakeRapidOCR:
        def __init__(self, **kwargs: object) -> None:
            init_calls.append(kwargs)

        def __call__(self, img: np.ndarray) -> SimpleNamespace:
            return SimpleNamespace(boxes=None, txts=None, scores=None)

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=FakeRapidOCR))
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    engine = RapidOcrEngine(OcrConfig())

    assert engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8)) == []
    assert init_calls == [{}]


def test_rapidocr_engine_requires_onnxruntime_even_when_rapidocr_is_loaded(monkeypatch) -> None:
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "onnxruntime":
            return None
        return original_find_spec(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=object))
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    with pytest.raises(ConfigError, match=r"pip install pdf-ocrer\[rapidocr\]"):
        RapidOcrEngine(OcrConfig())


def test_module_import_is_rapidocr_free() -> None:
    code = "import pdf_ocrer.rapidocr_engine, sys; assert 'rapidocr' not in sys.modules"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_pyproject_defines_rapidocr_extra() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["optional-dependencies"]["rapidocr"] == [
        "rapidocr>=3.9",
        "onnxruntime>=1.19",
    ]


def test_pyproject_defines_gpu_extras() -> None:
    extras = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]

    assert extras["rapidocr-gpu-cuda"] == ["rapidocr>=3.9", "onnxruntime-gpu>=1.19"]
    assert extras["rapidocr-gpu-dml"] == ["rapidocr>=3.9", "onnxruntime-directml>=1.19"]


def test_rapidocr_params_maps_cuda_device() -> None:
    params = _rapidocr_params(replace(OcrConfig(), device="cuda"))

    assert params["EngineConfig.onnxruntime.use_cuda"] is True
    assert "EngineConfig.onnxruntime.use_dml" not in params


def test_rapidocr_params_maps_dml_device() -> None:
    params = _rapidocr_params(replace(OcrConfig(), device="dml"))

    assert params["EngineConfig.onnxruntime.use_dml"] is True
    assert "EngineConfig.onnxruntime.use_cuda" not in params


def test_rapidocr_params_cpu_device_sets_no_gpu_keys() -> None:
    params = _rapidocr_params(replace(OcrConfig(), device="cpu"))

    assert not any(key.endswith("use_cuda") or key.endswith("use_dml") for key in params)


def test_rapidocr_params_maps_non_small_model_type() -> None:
    params = _rapidocr_params(replace(OcrConfig(), model_type="server"))

    assert params["Det.model_type"] == "server"
    assert params["Rec.model_type"] == "server"


def test_rapidocr_params_small_model_type_omits_model_keys() -> None:
    params = _rapidocr_params(replace(OcrConfig(), model_type="small"))

    assert "Det.model_type" not in params
    assert "Rec.model_type" not in params


def test_device_status_message_cpu_is_silent() -> None:
    assert _device_status_message("cpu", ["CPUExecutionProvider"]) is None


def test_device_status_message_reports_active_gpu() -> None:
    msg = _device_status_message("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"])

    assert msg is not None
    assert "CUDAExecutionProvider" in msg
    # Must not over-claim actual execution; onnxruntime can still fall back at
    # session creation, so the message stays accurate about that possibility.
    assert "CPU" in msg


def test_onnxruntime_conflict_message_none_for_single_runtime() -> None:
    from pdf_ocrer.rapidocr_engine import _onnxruntime_conflict_message

    assert _onnxruntime_conflict_message(["onnxruntime"]) is None
    assert _onnxruntime_conflict_message(["onnxruntime-directml"]) is None
    assert _onnxruntime_conflict_message([]) is None


def test_onnxruntime_conflict_message_warns_on_multiple_runtimes() -> None:
    from pdf_ocrer.rapidocr_engine import _onnxruntime_conflict_message

    msg = _onnxruntime_conflict_message(["onnxruntime", "onnxruntime-directml"])

    assert msg is not None
    assert "onnxruntime" in msg
    assert "onnxruntime-directml" in msg


def test_device_status_message_warns_when_provider_unavailable() -> None:
    msg = _device_status_message("cuda", ["CPUExecutionProvider"])

    assert msg is not None
    assert "CUDAExecutionProvider" in msg
    assert "rapidocr-gpu-cuda" in msg


def test_device_status_message_dml_warns_with_dml_extra() -> None:
    msg = _device_status_message("dml", ["CPUExecutionProvider"])

    assert msg is not None
    assert "rapidocr-gpu-dml" in msg


def test_rapidocr_engine_logs_gpu_status_on_init(monkeypatch) -> None:
    logs: list[str] = []

    class FakeRapidOCR:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __call__(self, img: np.ndarray) -> SimpleNamespace:
            return SimpleNamespace(boxes=None, txts=None, scores=None)

    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=FakeRapidOCR))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]),
    )
    engine = RapidOcrEngine(replace(OcrConfig(), device="cuda"), logs.append)
    engine.recognize(np.zeros((2, 2, 3), dtype=np.uint8))

    assert "正在載入 OCR 模型…" in logs
    assert any("CUDAExecutionProvider" in line for line in logs)
