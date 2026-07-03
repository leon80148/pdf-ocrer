from __future__ import annotations

import sys
import types
from pathlib import Path

import pymupdf

from fixtures_gen import GT_LINES
from pdf_ocrer import __version__
from pdf_ocrer.cli import main
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
