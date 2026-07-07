from __future__ import annotations

from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
ctk = pytest.importorskip("customtkinter")

from pdf_ocrer.config import load_config  # noqa: E402
from pdf_ocrer.settings_dialog import (  # noqa: E402
    MODEL_SIZE_CUSTOM_LABEL,
    MODEL_SIZE_SMALL_LABEL,
    MODEL_SIZE_TINY_LABEL,
    SettingsDialog,
)


@pytest.fixture()
def root():
    try:
        probe = ctk.CTk()
    except tk.TclError:
        pytest.skip("no display")
    else:
        probe.withdraw()
        probe.destroy()

    instance = ctk.CTk()
    instance.withdraw()
    try:
        yield instance
    finally:
        _destroy_if_exists(instance)


def test_dialog_loads_existing_settings(root, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        engine="rapidocr",
        dpi=321,
        min_confidence=0.66,
        det_model_name="PP-OCRv6_tiny_det",
        rec_model_name="PP-OCRv6_tiny_rec",
        workers=4,
        naming_enabled=False,
        llm_provider="none",
        llm_model="demo-model",
        llm_base_url="https://example.test/v1",
        llm_api_key="sk-demo",
    )
    dialog = _make_dialog(root, config_path)

    try:
        assert dialog.engine_var.get() == "rapidocr"
        assert dialog.dpi_entry.get() == "321"
        assert dialog.min_confidence_entry.get() == "0.66"
        assert dialog.model_size_var.get() == MODEL_SIZE_TINY_LABEL
        assert dialog.workers_entry.get() == "4"
        assert dialog.naming_enabled_var.get() is False
        assert dialog.llm_provider_entry.get() == "none"
        assert dialog.llm_model_entry.get() == "demo-model"
        assert dialog.llm_base_url_entry.get() == "https://example.test/v1"
        assert dialog.llm_api_key_entry.get() == "sk-demo"
        assert dialog.llm_api_key_entry.cget("show") == "*"
    finally:
        _destroy_if_exists(dialog)


def test_dialog_preselects_tiny_model_pair(root, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        det_model_name="PP-OCRv6_tiny_det",
        rec_model_name="PP-OCRv6_tiny_rec",
    )
    dialog = _make_dialog(root, config_path)

    try:
        assert dialog.model_size_var.get() == MODEL_SIZE_TINY_LABEL
    finally:
        _destroy_if_exists(dialog)


def test_dialog_preserves_custom_model_choice_for_mismatched_pair(root, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(
        config_path,
        det_model_name="PP-OCRv6_tiny_det",
        rec_model_name="PP-OCRv6_small_rec",
    )
    dialog = _make_dialog(root, config_path)

    try:
        assert dialog.model_size_var.get() == MODEL_SIZE_CUSTOM_LABEL
        assert MODEL_SIZE_CUSTOM_LABEL in dialog.model_size_menu.cget("values")
    finally:
        _destroy_if_exists(dialog)


def test_invalid_save_shows_error_keeps_window_and_does_not_write(
    root,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path, dpi=200)
    original = config_path.read_bytes()
    dialog = _make_dialog(root, config_path)
    errors: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        "pdf_ocrer.settings_dialog.messagebox.showerror",
        lambda title, message, **kwargs: errors.append((title, message, kwargs.get("parent"))),
    )

    try:
        _replace_entry_text(dialog.dpi_entry, "9999")
        dialog._on_save()

        assert errors
        assert errors[0][2] is dialog
        assert _winfo_exists(dialog) == 1
        assert config_path.read_bytes() == original
    finally:
        _destroy_if_exists(dialog)


def test_valid_save_updates_config_and_closes_dialog(root, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path, naming_enabled=False)
    dialog = _make_dialog(root, config_path)

    dialog.engine_var.set("rapidocr")
    _replace_entry_text(dialog.dpi_entry, "275")
    _replace_entry_text(dialog.min_confidence_entry, "0.42")
    dialog.model_size_var.set(MODEL_SIZE_SMALL_LABEL)
    _replace_entry_text(dialog.workers_entry, "0")
    dialog.naming_enabled_var.set(True)
    _replace_entry_text(dialog.llm_provider_entry, "openai_compatible")
    _replace_entry_text(dialog.llm_model_entry, "saved-model")
    _replace_entry_text(dialog.llm_base_url_entry, "https://llm.example/v1")
    _replace_entry_text(dialog.llm_api_key_entry, "sk-saved")

    dialog._on_save()

    cfg = load_config(config_path)
    assert _winfo_exists(dialog) == 0
    assert cfg.ocr.engine == "rapidocr"
    assert cfg.ocr.dpi == 275
    assert cfg.ocr.min_confidence == 0.42
    assert cfg.ocr.det_model_name == "PP-OCRv6_small_det"
    assert cfg.ocr.rec_model_name == "PP-OCRv6_small_rec"
    assert cfg.performance.workers == 0
    assert cfg.naming.enabled is True
    assert cfg.llm.provider == "openai_compatible"
    assert cfg.llm.model == "saved-model"
    assert cfg.llm.base_url == "https://llm.example/v1"
    assert cfg.llm.api_key == "sk-saved"


def test_cancel_closes_dialog_without_writing(root, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path, dpi=200)
    original = config_path.read_bytes()
    dialog = _make_dialog(root, config_path)

    _replace_entry_text(dialog.dpi_entry, "300")
    dialog._on_cancel()

    assert _winfo_exists(dialog) == 0
    assert config_path.read_bytes() == original


def test_advanced_button_opens_config_path_without_writing(root, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path, dpi=200)
    original = config_path.read_bytes()
    opened: list[Path] = []
    dialog = _make_dialog(root, config_path, open_path=opened.append)

    try:
        dialog._on_advanced()

        assert opened == [config_path]
        assert config_path.read_bytes() == original
    finally:
        _destroy_if_exists(dialog)


def _make_dialog(root, config_path: Path, open_path=None) -> SettingsDialog:
    dialog = SettingsDialog(root, config_path, open_path=open_path or (lambda path: None))
    dialog.withdraw()
    dialog.update_idletasks()
    return dialog


def _write_config(
    path: Path,
    *,
    engine: str = "paddle",
    dpi: int = 200,
    min_confidence: float = 0.5,
    det_model_name: str | None = None,
    rec_model_name: str | None = None,
    workers: int = 1,
    naming_enabled: bool = True,
    llm_provider: str = "openai_compatible",
    llm_model: str = "qwen3:8b",
    llm_base_url: str = "http://localhost:11434/v1",
    llm_api_key: str = "",
) -> None:
    ocr_lines = [
        "[ocr]",
        f'engine = "{engine}"',
        f"dpi = {dpi}",
        f"min_confidence = {min_confidence}",
    ]
    if det_model_name is not None:
        ocr_lines.append(f'det_model_name = "{det_model_name}"')
    if rec_model_name is not None:
        ocr_lines.append(f'rec_model_name = "{rec_model_name}"')

    text = "\n".join(
        [
            *ocr_lines,
            "",
            "[performance]",
            f"workers = {workers}",
            "",
            "[naming]",
            f"enabled = {_toml_bool(naming_enabled)}",
            "",
            "[llm]",
            f'provider = "{llm_provider}"',
            f'model = "{llm_model}"',
            f'base_url = "{llm_base_url}"',
            f'api_key = "{llm_api_key}"',
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _replace_entry_text(entry, text: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, text)


def _winfo_exists(widget) -> int:
    try:
        return int(widget.winfo_exists())
    except tk.TclError:
        return 0


def _destroy_if_exists(widget) -> None:
    if _winfo_exists(widget):
        widget.destroy()
