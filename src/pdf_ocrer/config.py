from __future__ import annotations

import os
import shutil
import tomllib
import warnings
from collections.abc import MutableMapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TypeVar

import tomlkit
from tomlkit.exceptions import TOMLKitError


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
    engine: str = "paddle"
    cpu_threads: int = 0
    textline_orientation: bool = True


@dataclass(frozen=True)
class OutputConfig:
    subdir_name: str = "OCR輸出"
    csv_prefix: str = "對照表"
    export_txt: bool = False
    incremental: bool = True


@dataclass(frozen=True)
class InputConfig:
    recursive: bool = False
    image_extensions: tuple[str, ...] = ("jpg", "jpeg", "png", "tif", "tiff")


@dataclass(frozen=True)
class PerformanceConfig:
    workers: int = 1


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
class LoggingConfig:
    enabled: bool = True
    level: str = "INFO"
    dir: str = ""


@dataclass(frozen=True)
class AppConfig:
    ocr: OcrConfig
    output: OutputConfig
    naming: NamingConfig
    llm: LlmConfig
    debug: DebugConfig
    input: InputConfig = InputConfig()
    performance: PerformanceConfig = PerformanceConfig()
    gui: GuiConfig = GuiConfig()
    logging: LoggingConfig = LoggingConfig()


class ConfigError(ValueError):
    """Raised when TOML config values cannot be used by the application."""


@dataclass(frozen=True)
class CommonSettings:
    engine: str = "paddle"
    dpi: int = 200
    min_confidence: float = 0.5
    det_model_name: str | None = None
    rec_model_name: str | None = None
    workers: int = 1
    naming_enabled: bool = True
    llm_provider: str = "openai_compatible"
    llm_model: str = "qwen3:8b"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""


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
        "input": InputConfig,
        "performance": PerformanceConfig,
        "naming": NamingConfig,
        "llm": LlmConfig,
        "debug": DebugConfig,
        "gui": GuiConfig,
        "logging": LoggingConfig,
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
            input=_section(section_values, "input", InputConfig()),
            performance=_section(section_values, "performance", PerformanceConfig()),
            gui=_section(section_values, "gui", GuiConfig()),
            logging=_section(section_values, "logging", LoggingConfig()),
        )
    )


def ensure_config_file(path: Path) -> None:
    config_path = Path(path)
    if config_path.exists():
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    example_path = Path("config.example.toml")
    if example_path.exists():
        shutil.copyfile(example_path, config_path)
        return

    config_path.write_text("", encoding="utf-8")


def read_common_settings(path: Path) -> CommonSettings:
    cfg = load_config(path)
    return CommonSettings(
        engine=cfg.ocr.engine,
        dpi=cfg.ocr.dpi,
        min_confidence=cfg.ocr.min_confidence,
        det_model_name=cfg.ocr.det_model_name,
        rec_model_name=cfg.ocr.rec_model_name,
        workers=cfg.performance.workers,
        naming_enabled=cfg.naming.enabled,
        llm_provider=cfg.llm.provider,
        llm_model=cfg.llm.model,
        llm_base_url=cfg.llm.base_url,
        llm_api_key=cfg.llm.api_key,
    )


def apply_common_settings(path: Path, settings: CommonSettings) -> None:
    config_path = Path(path)
    _validate_common_settings(settings, OcrConfig(), NamingConfig(), LlmConfig(), PerformanceConfig())

    ensure_config_file(config_path)

    try:
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except TOMLKitError as exc:
        raise ConfigError(f"設定檔 TOML 格式錯誤: {exc}") from exc

    current = load_config(config_path)
    _validate_common_settings(settings, current.ocr, current.naming, current.llm, current.performance)

    ocr_table = _ensure_toml_table(doc, "ocr")
    performance_table = _ensure_toml_table(doc, "performance")
    naming_table = _ensure_toml_table(doc, "naming")
    llm_table = _ensure_toml_table(doc, "llm")

    ocr_table["engine"] = settings.engine
    ocr_table["dpi"] = settings.dpi
    ocr_table["min_confidence"] = settings.min_confidence
    _set_optional_toml_value(ocr_table, "det_model_name", settings.det_model_name)
    _set_optional_toml_value(ocr_table, "rec_model_name", settings.rec_model_name)
    performance_table["workers"] = settings.workers
    naming_table["enabled"] = settings.naming_enabled
    llm_table["provider"] = settings.llm_provider
    llm_table["model"] = settings.llm_model
    llm_table["base_url"] = settings.llm_base_url
    llm_table["api_key"] = settings.llm_api_key

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _validate_common_settings(
    settings: CommonSettings,
    ocr_base: OcrConfig,
    naming_base: NamingConfig,
    llm_base: LlmConfig,
    performance_base: PerformanceConfig,
) -> None:
    ocr = replace(
        ocr_base,
        engine=settings.engine,
        dpi=settings.dpi,
        min_confidence=settings.min_confidence,
        det_model_name=settings.det_model_name,
        rec_model_name=settings.rec_model_name,
    )
    naming = replace(naming_base, enabled=settings.naming_enabled)
    performance = replace(performance_base, workers=settings.workers)
    llm = replace(
        llm_base,
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
    )
    _validate_ocr(ocr)
    _validate_naming(naming)
    _validate_llm(llm)
    _validate_performance(performance)


def resolve_api_key(cfg: LlmConfig) -> str:
    return cfg.api_key or os.environ.get("PDF_OCRER_API_KEY", "")


def resolve_worker_count(perf: PerformanceConfig, cpu_count: int | None) -> int:
    if perf.workers >= 1:
        return min(perf.workers, 8)

    return min(3, max(1, (cpu_count or 2) // 4))


def resolve_cpu_threads(ocr: OcrConfig, workers: int, cpu_count: int | None) -> int:
    if ocr.cpu_threads > 0:
        return ocr.cpu_threads
    if workers > 1:
        return max(1, (cpu_count or 2) // (2 * workers))
    return 0


def _ensure_toml_table(
    doc: MutableMapping[str, object],
    section_name: str,
) -> MutableMapping[str, object]:
    section = doc.get(section_name)
    if section is None:
        section = tomlkit.table()
        doc[section_name] = section
    if not isinstance(section, MutableMapping):
        raise ConfigError(f"設定欄位 {section_name} 必須是 TOML 表格")
    return section


def _set_optional_toml_value(
    table: MutableMapping[str, object],
    key: str,
    value: str | None,
) -> None:
    if value is None:
        if key in table:
            del table[key]
        return

    table[key] = value


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
    ocr = _validate_ocr(cfg.ocr)
    output = _validate_output(cfg.output)
    _validate_naming(cfg.naming)
    _validate_llm(cfg.llm)
    return replace(
        cfg,
        ocr=ocr,
        output=output,
        input=_validate_input(cfg.input),
        performance=_validate_performance(cfg.performance),
        gui=_validate_gui(cfg.gui),
        logging=_validate_logging(cfg.logging),
    )


def _validate_ocr(cfg: OcrConfig) -> OcrConfig:
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

    if not isinstance(cfg.engine, str):
        raise ConfigError("設定欄位 engine 必須是 paddle 或 rapidocr")
    engine = cfg.engine.casefold()
    if engine not in {"paddle", "rapidocr"}:
        raise ConfigError("設定欄位 engine 必須是 paddle 或 rapidocr")

    _require_int("cpu_threads", cfg.cpu_threads)
    if not 0 <= cfg.cpu_threads <= 64:
        _range_error("cpu_threads", "0–64")

    _require_bool("textline_orientation", cfg.textline_orientation)

    return cfg if engine == cfg.engine else replace(cfg, engine=engine)


def _validate_output(cfg: OutputConfig) -> OutputConfig:
    _require_bool("export_txt", cfg.export_txt)
    _require_bool("incremental", cfg.incremental)
    return cfg


def _validate_input(cfg: InputConfig) -> InputConfig:
    _require_bool("recursive", cfg.recursive)
    image_extensions = _normalize_image_extensions(cfg.image_extensions)
    if image_extensions == cfg.image_extensions:
        return cfg
    return replace(cfg, image_extensions=image_extensions)


def _validate_performance(cfg: PerformanceConfig) -> PerformanceConfig:
    _require_int("workers", cfg.workers)
    if not 0 <= cfg.workers <= 8:
        _range_error("workers", "0–8")
    return cfg


def _normalize_image_extensions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ConfigError("設定欄位 image_extensions 必須是字串清單")

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ConfigError("設定欄位 image_extensions 必須是字串清單")
        extension = item.removeprefix(".").casefold()
        if extension not in seen:
            result.append(extension)
            seen.add(extension)

    return tuple(result)


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


def _validate_logging(cfg: LoggingConfig) -> LoggingConfig:
    _require_bool("enabled", cfg.enabled)
    if not isinstance(cfg.level, str):
        raise ConfigError("設定欄位 level 必須是 DEBUG/INFO/WARNING/ERROR")
    if not isinstance(cfg.dir, str):
        raise ConfigError("設定欄位 dir 必須是字串")

    level = cfg.level.casefold()
    levels = {
        "debug": "DEBUG",
        "info": "INFO",
        "warning": "WARNING",
        "error": "ERROR",
    }
    if level not in levels:
        raise ConfigError("設定欄位 level 必須是 DEBUG/INFO/WARNING/ERROR")

    normalized = levels[level]
    return cfg if normalized == cfg.level else replace(cfg, level=normalized)


def _require_int(field_name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"設定欄位 {field_name} 必須是整數")


def _require_number(field_name: str, value: object) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConfigError(f"設定欄位 {field_name} 必須是數字")


def _require_bool(field_name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise ConfigError(f"設定欄位 {field_name} 必須是布林值")


def _range_error(field_name: str, expected: str) -> None:
    raise ConfigError(f"設定欄位 {field_name} 超出範圍，應為 {expected}")
