# Codex 任務簡報：GUI 設定視窗（SettingsDialog）

## 任務

在本 repo（cwd：`C:\Users\User\projects\pdf_ocrer`）實作「GUI 設定視窗」功能。先完整閱讀 `docs/plans/2026-07-04-settings-dialog-gui-plan.md`（本任務的唯一計畫，照做，不要重新設計），並參考 `docs/specs/` 內既有規格與 `src/pdf_ocrer/config.py`、`src/pdf_ocrer/gui.py`、`tests/test_gui.py` 的既有模式。

實作範圍（計畫內已詳述，此為摘要）：

1. `pyproject.toml` dependencies 加 `"tomlkit>=0.12"`（venv 已裝好 tomlkit 0.15，不需 pip install）。
2. `src/pdf_ocrer/config.py` 新增：`ensure_config_file(path)`、`CommonSettings` frozen dataclass、`read_common_settings(path)`、`apply_common_settings(path, settings)`（tomlkit round-trip、保留註解、寫檔前先用既有 `_validate_ocr`/`_validate_naming`/`_validate_llm` 驗證、det/rec model name 為 None 時刪 key 而非寫 null、驗證失敗時檔案 byte-for-byte 不變）。
3. `src/pdf_ocrer/settings_dialog.py` 新檔：純邏輯函式 `model_size_label_for_pair` / `model_pair_for_label` / `model_size_dropdown_values`（tiny/small/medium（預設）三組 pair，未知或不匹配 pair 顯示「自訂（保留現有值）」第 4 選項並原樣 round-trip），以及 `SettingsDialog(ctk.CTkToplevel)`（OCR/命名/LLM 三區、api_key 用 `show="*"`、「儲存」「取消」「進階設定（文字檔）」三按鈕、`open_path` callable 由建構子注入）。
4. `src/pdf_ocrer/gui.py`：刪除 `_edit_config`，新增 `_open_settings_dialog`（`grab_set` + `wait_window`），`edit_config_button` 改指向它；`_edit_prompt` 不動。
5. 測試（TDD：每步先寫測試再實作）：
   - `tests/test_config.py` 增補 `apply_common_settings`/`ensure_config_file` 測試（含註解保留、medium 刪 key、驗證失敗檔案不變、無/有 config.example.toml 兩種 bootstrap）
   - 新檔 `tests/test_settings_dialog_logic.py`（純函式、絕不 import tkinter/customtkinter）
   - 新檔 `tests/test_settings_dialog.py`（照抄 `tests/test_gui.py` 的 display-probe-skip fixture 模式）
   - `tests/test_gui.py` 加一個測試驗證按鈕改開 SettingsDialog（monkeypatch）

## 約束

- 禁止 `git commit` / `git add`；只改工作區檔案。
- 不得改動計畫未提及的檔案或行為（`_edit_prompt`、appearance 控制、pipeline 等都不動）。
- 中文 UI 字串與計畫一致：「儲存」「取消」「進階設定（文字檔）」「自訂（保留現有值）」「medium（預設）」。
- 跑測試一律用 `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider`，結束前清掉自己產生的暫存目錄；不要留下 `.pytest_cache`。
- 若環境無 display，GUI 測試會經 fixture 自動 skip，屬預期；純邏輯與 config 測試必須全綠。

## 驗證迴圈

每完成一步就跑對應測試檔；全部完成後跑完整 suite（同上 pytest 指令）確認無回歸（基線：81 passed，GUI 測試在無 display 時可 skip）。ruff 檢查：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m ruff check src tests`（line-length 100）。

## 回報格式（精簡）

1. 變更檔案清單（新增/修改）
2. 完整 pytest 輸出最後 3 行 + ruff 結果
3. 計畫中每個設計決策點的落實位置（檔名:函式）
4. 任何偏離計畫之處與原因（理想上無）
