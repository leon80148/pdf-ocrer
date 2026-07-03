from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TypeVar


@dataclass(frozen=True)
class OcrConfig:
    dpi: int = 200
    lang: str = "chinese_cht"
    min_confidence: float = 0.5
    skip_pages_with_text: bool = True
    min_existing_chars: int = 30
    device: str = "cpu"
    enable_mkldnn: bool = False
    det_limit_side_len: int | None = None
    det_model_name: str | None = None
    rec_model_name: str | None = None


@dataclass(frozen=True)
class OutputConfig:
    subdir_name: str = "OCR輸出"
    csv_prefix: str = "對照表"


@dataclass(frozen=True)
class NamingConfig:
    enabled: bool = True
    rename_files_with_text: bool = True
    prompt_file: str = "naming_prompt.txt"
    max_chars_to_llm: int = 3000
    max_pages_to_llm: int = 2
    max_filename_length: int = 80
    fallback_suffix: str = "_OCR"


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "openai_compatible"
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    api_key: str = ""
    timeout_seconds: float = 60.0
    temperature: float = 0.1
    max_tokens: int = 1024


@dataclass(frozen=True)
class DebugConfig:
    visible_text: bool = False


@dataclass(frozen=True)
class GuiConfig:
    appearance: str = "system"


@dataclass(frozen=True)
class AppConfig:
    ocr: OcrConfig
    output: OutputConfig
    naming: NamingConfig
    llm: LlmConfig
    debug: DebugConfig
    gui: GuiConfig = GuiConfig()


class ConfigError(ValueError):
    """Raised when TOML config values cannot be used by the application."""


_T = TypeVar("_T")


def load_config(path: Path | None = None) -> AppConfig:
    config_path = Path("config.toml") if path is None else path
    if not config_path.exists():
        return _validate(AppConfig(OcrConfig(), OutputConfig(), NamingConfig(), LlmConfig(), DebugConfig()))

    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"設定檔 TOML 格式錯誤: {exc}") from exc

    sections: dict[str, type[object]] = {
        "ocr": OcrConfig,
        "output": OutputConfig,
        "naming": NamingConfig,
        "llm": LlmConfig,
        "debug": DebugConfig,
        "gui": GuiConfig,
    }
    section_values: dict[str, object] = {}

    for section_name, section_data in raw.items():
        section_cls = sections.get(section_name)
        if section_cls is None:
            _warn_unknown(section_name)
            continue
        if not isinstance(section_data, dict):
            raise ConfigError(f"設定欄位 {section_name} 必須是 TOML 表格")
        section_values[section_name] = _build_section(section_cls, section_name, section_data)

    return _validate(
        AppConfig(
            ocr=_section(section_values, "ocr", OcrConfig()),
            output=_section(section_values, "output", OutputConfig()),
            naming=_section(section_values, "naming", NamingConfig()),
            llm=_section(section_values, "llm", LlmConfig()),
            debug=_section(section_values, "debug", DebugConfig()),
            gui=_section(section_values, "gui", GuiConfig()),
        )
    )


def resolve_api_key(cfg: LlmConfig) -> str:
    return cfg.api_key or os.environ.get("PDF_OCRER_API_KEY", "")


def _build_section(cls: type[_T], section_name: str, data: dict[str, object]) -> _T:
    allowed = {field.name for field in fields(cls)}
    values: dict[str, object] = {}

    for key, value in data.items():
        if key not in allowed:
            _warn_unknown(f"{section_name}.{key}")
            continue
        values[key] = value

    return cls(**values)


def _section(values: dict[str, object], key: str, default: _T) -> _T:
    return values.get(key, default)  # type: ignore[return-value]


def _warn_unknown(name: str) -> None:
    warnings.warn(f"未知設定鍵 {name}，已忽略。", UserWarning, stacklevel=3)


def _validate(cfg: AppConfig) -> AppConfig:
    _validate_ocr(cfg.ocr)
    _validate_naming(cfg.naming)
    _validate_llm(cfg.llm)
    return replace(cfg, gui=_validate_gui(cfg.gui))


def _validate_ocr(cfg: OcrConfig) -> None:
    _require_int("dpi", cfg.dpi)
    if not 72 <= cfg.dpi <= 600:
        _range_error("dpi", "72–600")

    _require_number("min_confidence", cfg.min_confidence)
    if not 0 <= cfg.min_confidence <= 1:
        _range_error("min_confidence", "0–1")

    _require_int("min_existing_chars", cfg.min_existing_chars)
    if cfg.min_existing_chars < 0:
        _range_error("min_existing_chars", "0 以上")

    if cfg.det_limit_side_len is not None:
        _require_int("det_limit_side_len", cfg.det_limit_side_len)
        if cfg.det_limit_side_len <= 0:
            _range_error("det_limit_side_len", "大於 0")


def _validate_naming(cfg: NamingConfig) -> None:
    _require_int("max_chars_to_llm", cfg.max_chars_to_llm)
    if cfg.max_chars_to_llm <= 0:
        _range_error("max_chars_to_llm", "大於 0")

    _require_int("max_pages_to_llm", cfg.max_pages_to_llm)
    if cfg.max_pages_to_llm <= 0:
        _range_error("max_pages_to_llm", "大於 0")

    _require_int("max_filename_length", cfg.max_filename_length)
    if not 10 <= cfg.max_filename_length <= 200:
        _range_error("max_filename_length", "10–200")


def _validate_llm(cfg: LlmConfig) -> None:
    _require_number("timeout_seconds", cfg.timeout_seconds)
    if cfg.timeout_seconds <= 0:
        _range_error("timeout_seconds", "大於 0")

    _require_number("temperature", cfg.temperature)
    if cfg.temperature < 0:
        _range_error("temperature", "0 以上")

    _require_int("max_tokens", cfg.max_tokens)
    if cfg.max_tokens <= 0:
        _range_error("max_tokens", "大於 0")


def _validate_gui(cfg: GuiConfig) -> GuiConfig:
    if not isinstance(cfg.appearance, str):
        raise ConfigError("設定欄位 appearance 必須是 system/light/dark")

    appearance = cfg.appearance.casefold()
    if appearance not in {"system", "light", "dark"}:
        raise ConfigError("設定欄位 appearance 必須是 system/light/dark")

    return cfg if appearance == cfg.appearance else GuiConfig(appearance=appearance)


def _require_int(field_name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"設定欄位 {field_name} 必須是整數")


def _require_number(field_name: str, value: object) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConfigError(f"設定欄位 {field_name} 必須是數字")


def _range_error(field_name: str, expected: str) -> None:
    raise ConfigError(f"設定欄位 {field_name} 超出範圍，應為 {expected}")
