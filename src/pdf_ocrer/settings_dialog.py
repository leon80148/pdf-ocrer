from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from pdf_ocrer.config import (
    CommonSettings,
    ConfigError,
    apply_common_settings,
    ensure_config_file,
    read_common_settings,
)

ModelPair = tuple[str | None, str | None]

MODEL_SIZE_TINY_LABEL = "tiny"
MODEL_SIZE_SMALL_LABEL = "small"
MODEL_SIZE_MEDIUM_LABEL = "medium（預設）"
MODEL_SIZE_CUSTOM_LABEL = "自訂（保留現有值）"

MODEL_SIZE_LABELS = (
    MODEL_SIZE_TINY_LABEL,
    MODEL_SIZE_SMALL_LABEL,
    MODEL_SIZE_MEDIUM_LABEL,
)

MODEL_SIZE_PAIRS: dict[str, ModelPair] = {
    MODEL_SIZE_TINY_LABEL: ("PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec"),
    MODEL_SIZE_SMALL_LABEL: ("PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
    MODEL_SIZE_MEDIUM_LABEL: (None, None),
}
_MODEL_PAIR_LABELS = {pair: label for label, pair in MODEL_SIZE_PAIRS.items()}

# rapidocr execution device and model size (see docs/specs/rapidocr-api-facts.md).
DEVICE_VALUES = ("cpu", "cuda", "dml")
RAPIDOCR_MODEL_TYPES = ("mobile", "tiny", "small", "medium", "server")


def model_size_label_for_pair(det_model_name: str | None, rec_model_name: str | None) -> str:
    return _MODEL_PAIR_LABELS.get((det_model_name, rec_model_name), MODEL_SIZE_CUSTOM_LABEL)


def model_pair_for_label(label: str, current: ModelPair) -> ModelPair:
    if label == MODEL_SIZE_CUSTOM_LABEL:
        return current
    if label not in MODEL_SIZE_PAIRS:
        raise ValueError(f"未知模型大小：{label}")
    return MODEL_SIZE_PAIRS[label]


def model_size_dropdown_values(det_model_name: str | None, rec_model_name: str | None) -> list[str]:
    values = list(MODEL_SIZE_LABELS)
    if model_size_label_for_pair(det_model_name, rec_model_name) == MODEL_SIZE_CUSTOM_LABEL:
        values.append(MODEL_SIZE_CUSTOM_LABEL)
    return values


class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: object,
        config_path: Path,
        *,
        open_path: Callable[[Path], None],
    ) -> None:
        super().__init__(master)
        self._config_path = Path(config_path)
        self._open_path = open_path

        settings = read_common_settings(self._config_path)
        self._current_model_pair = (settings.det_model_name, settings.rec_model_name)

        self.title("設定")
        self.resizable(False, False)
        self.columnconfigure(0, weight=1)

        self._build_ocr_section(settings)
        self._build_naming_section(settings)
        self._build_llm_section(settings)
        self._build_buttons()

    def _build_ocr_section(self, settings: CommonSettings) -> None:
        frame = self._section_frame("OCR", row=0)

        self._label(frame, "OCR 引擎", row=0)
        self.engine_var = tk.StringVar(master=self, value=settings.engine)
        self.engine_menu = ctk.CTkOptionMenu(
            frame,
            values=["paddle", "rapidocr"],
            variable=self.engine_var,
        )
        self.engine_menu.grid(row=1, column=1, sticky="ew", padx=(8, 12), pady=4)
        self.engine_menu.set(settings.engine)

        self._label(frame, "執行裝置", row=1)
        self.device_var = tk.StringVar(master=self, value=settings.device)
        self.device_menu = ctk.CTkOptionMenu(
            frame,
            values=list(DEVICE_VALUES),
            variable=self.device_var,
        )
        self.device_menu.grid(row=2, column=1, sticky="ew", padx=(8, 12), pady=4)
        self.device_menu.set(settings.device)
        self.device_help_label = ctk.CTkLabel(
            frame, text="cuda/dml 僅適用 rapidocr（需另裝 GPU 套件）", anchor="w"
        )
        self.device_help_label.grid(row=3, column=1, sticky="w", padx=(8, 12))

        self._label(frame, "DPI", row=3)
        self.dpi_entry = ctk.CTkEntry(frame)
        self.dpi_entry.grid(row=4, column=1, sticky="ew", padx=(8, 12), pady=4)
        self._set_entry_text(self.dpi_entry, str(settings.dpi))

        self._label(frame, "最低信心分數", row=4)
        self.min_confidence_entry = ctk.CTkEntry(frame)
        self.min_confidence_entry.grid(row=5, column=1, sticky="ew", padx=(8, 12), pady=4)
        self._set_entry_text(self.min_confidence_entry, str(settings.min_confidence))

        self._label(frame, "模型大小（rapidocr）", row=5)
        self.model_type_var = tk.StringVar(master=self, value=settings.model_type)
        self.model_type_menu = ctk.CTkOptionMenu(
            frame,
            values=list(RAPIDOCR_MODEL_TYPES),
            variable=self.model_type_var,
        )
        self.model_type_menu.grid(row=6, column=1, sticky="ew", padx=(8, 12), pady=4)
        self.model_type_menu.set(settings.model_type)

        self._label(frame, "模型大小（paddle）", row=6)
        model_label = model_size_label_for_pair(settings.det_model_name, settings.rec_model_name)
        self.model_size_var = tk.StringVar(master=self, value=model_label)
        self.model_size_menu = ctk.CTkOptionMenu(
            frame,
            values=model_size_dropdown_values(settings.det_model_name, settings.rec_model_name),
            variable=self.model_size_var,
        )
        self.model_size_menu.grid(row=7, column=1, sticky="ew", padx=(8, 12), pady=4)
        self.model_size_menu.set(model_label)

        self._label(frame, "同時處理檔案數", row=7)
        self.workers_entry = ctk.CTkEntry(frame)
        self.workers_entry.grid(row=8, column=1, sticky="ew", padx=(8, 12), pady=4)
        self._set_entry_text(self.workers_entry, str(settings.workers))

        self.workers_help_label = ctk.CTkLabel(frame, text="0=自動、1=循序", anchor="w")
        self.workers_help_label.grid(row=9, column=1, sticky="w", padx=(8, 12), pady=(0, 12))

    def _build_naming_section(self, settings: CommonSettings) -> None:
        frame = self._section_frame("命名", row=1)

        self.naming_enabled_var = tk.BooleanVar(master=self, value=settings.naming_enabled)
        self.naming_enabled_checkbox = ctk.CTkCheckBox(
            frame,
            text="啟用 LLM 命名",
            variable=self.naming_enabled_var,
        )
        self.naming_enabled_checkbox.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            padx=12,
            pady=(4, 12),
        )

    def _build_llm_section(self, settings: CommonSettings) -> None:
        frame = self._section_frame("LLM", row=2)

        self._label(frame, "Provider", row=0)
        self.llm_provider_entry = ctk.CTkEntry(frame)
        self.llm_provider_entry.grid(row=1, column=1, sticky="ew", padx=(8, 12), pady=4)
        self._set_entry_text(self.llm_provider_entry, settings.llm_provider)

        self._label(frame, "Model", row=1)
        self.llm_model_entry = ctk.CTkEntry(frame)
        self.llm_model_entry.grid(row=2, column=1, sticky="ew", padx=(8, 12), pady=4)
        self._set_entry_text(self.llm_model_entry, settings.llm_model)

        self._label(frame, "Base URL", row=2)
        self.llm_base_url_entry = ctk.CTkEntry(frame)
        self.llm_base_url_entry.grid(row=3, column=1, sticky="ew", padx=(8, 12), pady=4)
        self._set_entry_text(self.llm_base_url_entry, settings.llm_base_url)

        self._label(frame, "API Key", row=3)
        self.llm_api_key_entry = ctk.CTkEntry(frame, show="*")
        self.llm_api_key_entry.grid(row=4, column=1, sticky="ew", padx=(8, 12), pady=(4, 12))
        self._set_entry_text(self.llm_api_key_entry, settings.llm_api_key)

    def _build_buttons(self) -> None:
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 12))
        frame.columnconfigure(0, weight=1)

        self.advanced_button = ctk.CTkButton(
            frame,
            text="進階設定（文字檔）",
            command=self._on_advanced,
        )
        self.advanced_button.grid(row=0, column=0, sticky="w")

        self.save_button = ctk.CTkButton(frame, text="儲存", command=self._on_save)
        self.save_button.grid(row=0, column=1, padx=(8, 0))

        self.cancel_button = ctk.CTkButton(frame, text="取消", command=self._on_cancel)
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))

    def _section_frame(self, title: str, row: int) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self)
        frame.grid(row=row, column=0, sticky="ew", padx=12, pady=(12, 0))
        frame.columnconfigure(1, weight=1)

        label = ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(weight="bold"))
        label.grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 4))
        return frame

    def _label(self, master: ctk.CTkFrame, text: str, row: int) -> None:
        label = ctk.CTkLabel(master, text=text, anchor="w")
        label.grid(row=row + 1, column=0, sticky="w", padx=(12, 0), pady=4)

    def _set_entry_text(self, entry: ctk.CTkEntry, text: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, text)

    def _on_save(self) -> None:
        try:
            det_model_name, rec_model_name = model_pair_for_label(
                self.model_size_var.get(),
                current=self._current_model_pair,
            )
            settings = CommonSettings(
                engine=self.engine_var.get(),
                dpi=int(self.dpi_entry.get().strip()),
                min_confidence=float(self.min_confidence_entry.get().strip()),
                device=self.device_var.get(),
                model_type=self.model_type_var.get(),
                det_model_name=det_model_name,
                rec_model_name=rec_model_name,
                workers=int(self.workers_entry.get().strip()),
                naming_enabled=bool(self.naming_enabled_var.get()),
                llm_provider=self.llm_provider_entry.get(),
                llm_model=self.llm_model_entry.get(),
                llm_base_url=self.llm_base_url_entry.get(),
                llm_api_key=self.llm_api_key_entry.get(),
            )
            apply_common_settings(self._config_path, settings)
        except (ConfigError, ValueError) as exc:
            messagebox.showerror("設定錯誤", str(exc), parent=self)
            return

        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()

    def _on_advanced(self) -> None:
        ensure_config_file(self._config_path)
        self._open_path(self._config_path)
