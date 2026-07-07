from __future__ import annotations

import logging
import sys
import tomllib
import types
from pathlib import Path

import pymupdf

from fixtures_gen import GT_LINES
from pdf_ocrer import __version__
from pdf_ocrer.cli import main
from pdf_ocrer.config import ConfigError
from pdf_ocrer.ocr_engine import OcrLine

_DPI = 200
_FONT = pymupdf.Font("cjk")


class FakeEngine:
    def recognize(self, img_rgb) -> list[OcrLine]:  # noqa: ANN001
        return _gt_ocr_lines()


def test_main_without_folder_launches_gui(monkeypatch) -> None:
    called: list[bool] = []
    fake_gui = types.ModuleType("pdf_ocrer.gui")
    fake_gui.run_gui = lambda: called.append(True)
    monkeypatch.setitem(sys.modules, "pdf_ocrer.gui", fake_gui)

    assert main([]) == 0
    assert called == [True]


def test_main_version(capsys) -> None:
    assert main(["--version"]) == 0

    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_main_missing_folder_returns_2(tmp_path) -> None:
    assert main([str(tmp_path / "missing")]) == 2


def test_main_folder_no_llm_creates_csv_and_passes_dpi(work_folder, capsys) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})
    captured: dict[str, object] = {}

    def engine_factory(ocr_cfg):
        captured["dpi"] = ocr_cfg.dpi
        return FakeEngine()

    def client_factory(llm_cfg):
        captured["provider"] = llm_cfg.provider
        return None

    exit_code = main(
        [str(work_folder), "--no-llm", "--dpi", "300"],
        engine_factory=engine_factory,
        client_factory=client_factory,
    )

    assert exit_code == 0
    assert captured == {"dpi": 300, "provider": "none"}
    assert len(list((work_folder / "OCR輸出").glob("對照表_*.csv"))) == 1
    stdout = capsys.readouterr().out
    assert "CSV:" in stdout


def test_main_writes_batch_log_from_config_dir(work_folder, tmp_path) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})
    log_dir = tmp_path / "logs"
    log_dir_text = str(log_dir).replace("\\", "/")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[logging]\n"
        f'dir = "{log_dir_text}"\n'
        "[naming]\n"
        "enabled = false\n",
        encoding="utf-8",
    )

    exit_code = main(
        [str(work_folder), "--config", str(config_path)],
        engine_factory=lambda ocr_cfg: FakeEngine(),
        client_factory=lambda llm_cfg: None,
    )

    _flush_pdf_ocrer_file_handlers()
    text = (log_dir / "pdf_ocrer.log").read_text(encoding="utf-8")
    assert exit_code == 0
    assert "batch start" in text
    assert "source=native.pdf" in text
    assert "source=scanned.pdf" in text
    assert "batch end" in text


def test_main_engine_override_passes_to_engine_factory(work_folder, tmp_path) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[ocr]\nengine = "paddle"\n[naming]\nenabled = false\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def engine_factory(ocr_cfg):
        captured["engine"] = ocr_cfg.engine
        return FakeEngine()

    exit_code = main(
        [str(work_folder), "--config", str(config_path), "--engine", "rapidocr"],
        engine_factory=engine_factory,
        client_factory=lambda llm_cfg: None,
    )

    assert exit_code == 0
    assert captured == {"engine": "rapidocr"}


def test_main_create_engine_config_error_reports_install_hint(
    work_folder,
    monkeypatch,
    capsys,
) -> None:
    _keep_only(work_folder, {"scanned.pdf"})

    def fake_create_engine(ocr_cfg, log):  # noqa: ANN001
        raise ConfigError("rapidocr 引擎需要安裝額外套件：pip install pdf-ocrer[rapidocr]")

    monkeypatch.setattr("pdf_ocrer.cli.create_engine", fake_create_engine, raising=False)

    exit_code = main([str(work_folder), "--no-llm", "--engine", "rapidocr"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "pip install pdf-ocrer[rapidocr]" in captured.err


def test_main_config_error_writes_default_log(
    work_folder,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _keep_only(work_folder, {"scanned.pdf"})
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    config_path = tmp_path / "bad.toml"
    config_path.write_text('[logging]\nlevel = "TRACE"\n', encoding="utf-8")

    exit_code = main(
        [str(work_folder), "--config", str(config_path)],
        engine_factory=lambda ocr_cfg: FakeEngine(),
    )

    _flush_pdf_ocrer_file_handlers()
    captured = capsys.readouterr()
    log_path = tmp_path / "local" / "pdf_ocrer" / "logs" / "pdf_ocrer.log"
    assert exit_code == 1
    assert "設定錯誤:" in captured.err
    assert "設定錯誤:" in log_path.read_text(encoding="utf-8")


def test_main_naming_disabled_skips_llm_client_for_unknown_provider(work_folder, tmp_path) -> None:
    _keep_only(work_folder, {"scanned.pdf", "native.pdf"})
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[naming]\nenabled = false\n[llm]\nprovider = "bogus_provider"\n',
        encoding="utf-8",
    )

    def client_factory(llm_cfg):  # noqa: ANN001
        raise AssertionError("client_factory should not be called when naming is disabled")

    exit_code = main(
        [str(work_folder), "--config", str(config_path)],
        engine_factory=lambda ocr_cfg: FakeEngine(),
        client_factory=client_factory,
    )

    assert exit_code == 0
    assert len(list((work_folder / "OCR輸出").glob("對照表_*.csv"))) == 1


def test_main_rejects_dpi_override_before_creating_output(work_folder, capsys) -> None:
    _keep_only(work_folder, {"scanned.pdf"})

    exit_code = main(
        [str(work_folder), "--dpi", "10"],
        engine_factory=lambda ocr_cfg: FakeEngine(),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "設定錯誤: dpi 超出範圍，應為 72–600" in captured.err
    assert not (work_folder / "OCR輸出").exists()


def test_pyproject_paddle_cpu_extra_pins_3_2() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["optional-dependencies"]["paddle-cpu"] == [
        "paddlepaddle==3.2.*"
    ]


def _gt_ocr_lines() -> list[OcrLine]:
    return [OcrLine(text, _px_poly(point, fontsize, text), 0.99) for point, fontsize, text in GT_LINES]


def _px_poly(
    point: tuple[float, float],
    fontsize: float,
    text: str,
) -> tuple[tuple[float, float], ...]:
    baseline_x, baseline_y = point
    top = baseline_y - _FONT.ascender * fontsize
    bottom = baseline_y - _FONT.descender * fontsize
    right = baseline_x + _FONT.text_length(text, fontsize)
    scale = _DPI / 72.0
    return (
        (baseline_x * scale, top * scale),
        (right * scale, top * scale),
        (right * scale, bottom * scale),
        (baseline_x * scale, bottom * scale),
    )


def _keep_only(folder: Path, names: set[str]) -> None:
    for path in folder.iterdir():
        if path.is_file() and path.name not in names:
            path.unlink()


def setup_function() -> None:
    _remove_pdf_ocrer_file_handlers()


def teardown_function() -> None:
    _remove_pdf_ocrer_file_handlers()


def _remove_pdf_ocrer_file_handlers() -> None:
    logger = logging.getLogger("pdf_ocrer")
    for handler in list(logger.handlers):
        if getattr(handler, "_pdf_ocrer_file_handler", False):
            logger.removeHandler(handler)
            handler.close()


def _flush_pdf_ocrer_file_handlers() -> None:
    for handler in logging.getLogger("pdf_ocrer").handlers:
        if getattr(handler, "_pdf_ocrer_file_handler", False):
            handler.flush()
