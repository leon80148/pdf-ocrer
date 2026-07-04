# GUI 設定視窗（SettingsDialog）

## Context

使用者反應目前只能靠開啟 `config.toml` 純文字檔來調整設定（OCR dpi/模型大小、命名開關、LLM 連線資訊等），希望改為圖形化介面。經過 brainstorming 討論確認：只曝露「常用設定」（OCR dpi/信心分數/模型大小、命名是否啟用、LLM provider/model/base_url/api_key），其餘冷門欄位維持文字編輯；同時保留「進階設定（文字檔）」入口。存檔時必須保留 `config.toml` 現有的中文說明註解，因此需引入可 round-trip 的 TOML 函式庫（`tomlkit`）取代目前唯讀的 `tomllib` 寫入路徑。

## 目標

新增 `SettingsDialog`，取代 `gui.py` 現有「編輯設定」按鈕行為（原本直接開記事本），改為彈出結構化表單；表單內同時提供「進階設定（文字檔）」次要按鈕保留原本開檔行為。

## 設計決策

- **模型大小下拉選單**：`tiny` / `small` / `medium（預設）` 三選項，同時控制 `det_model_name` + `rec_model_name` 這組 pair。若目前檔案內的 pair 不屬於這三種已知組合（含使用者手動編輯造成的不匹配組合），下拉選單會多出第 4 個選項「自訂（保留現有值）」並預設選取；只要使用者不主動切換，存檔時原樣寫回（不強制轉成 null 或覆蓋）。
- **medium（預設）** 對應 `det_model_name`/`rec_model_name` 都是 `None` → 寫回時要**刪除**這兩個 key（TOML 沒有 null，維持 `config.example.toml` 現有「未設定就整行註解掉/不存在」的慣例），而不是寫入空字串或 `null`。
- **LLM 欄位驗證**：確認維持現狀，不新增驗證邏輯。存檔時仍呼叫既有 `_validate_ocr`/`_validate_naming`/`_validate_llm`，但這幾個 validator 本來就不檢查 provider/model/base_url/api_key 這 4 個字串欄位（只檢查 timeout/temperature/max_tokens），所以空字串等仍可存檔——這是已知且接受的範圍限制，不在此次一併補強。
- **註解保留**：讀寫都改用 `tomlkit`（新依賴）。只覆寫 GUI 曝露的那幾個 key，其餘原封不動。
- **進階設定入口**：`SettingsDialog` 建構時注入 `open_path` callable（`gui.py` 傳入既有的 `App._open_path`），對話框內「進階設定（文字檔）」按鈕呼叫 `ensure_config_file(path)` 後再呼叫 `open_path(path)`，与目前 `_edit_config` 行為等價，避免兩份重複的「開檔」邏輯。

## 檔案變更

### `pyproject.toml`
新增 `"tomlkit>=0.12"` 到 `[project].dependencies`；之後需在 `.venv` 重新 `pip install -e .`。

### `src/pdf_ocrer/config.py`（新增，讀寫邏輯放這裡，不放 GUI 模組）
- `ensure_config_file(path: Path) -> None`：從 `gui.py` 現有 `_edit_config` 裡的「檔案不存在就從 `config.example.toml` 複製或建空檔」邏輯抽出，冪等。
- `CommonSettings`（frozen dataclass）：`dpi`, `min_confidence`, `det_model_name: str | None`, `rec_model_name: str | None`, `naming_enabled`, `llm_provider`, `llm_model`, `llm_base_url`, `llm_api_key`。
- `read_common_settings(path: Path) -> CommonSettings`：用既有 `load_config(path)` 讀出後投影成 `CommonSettings`，供對話框開啟時預先帶入欄位值。
- `apply_common_settings(path: Path, settings: CommonSettings) -> None`：
  1. `ensure_config_file(path)`。
  2. `tomlkit.parse()` 讀入現有內容（保留註解/格式）。
  3. 用 `settings` 建構暫時的 `OcrConfig`/`NamingConfig`/`LlmConfig` 呼叫既有 `_validate_ocr`/`_validate_naming`/`_validate_llm`；驗證失敗丟 `ConfigError`，**檔案不寫入**。
  4. 確保 `[ocr]`/`[naming]`/`[llm]` table 存在（新建時比照 `config.example.toml` 的 header 風格）。
  5. 覆寫這 8 個 key；`det_model_name`/`rec_model_name` 為 `None` 時用 `del` 移除 key（而非寫 null）。
  6. `tomlkit.dumps(doc)` 寫回檔案。

### `src/pdf_ocrer/settings_dialog.py`（新檔）
- 純邏輯（無 GUI 依賴，可獨立測試）：
  - `model_size_label_for_pair(det, rec) -> str`
  - `model_pair_for_label(label, current) -> tuple[str|None, str|None]`（選 `自訂（保留現有值）` 時原樣回傳 `current`）
  - `model_size_dropdown_values(det, rec) -> list[str]`（已知組合 3 選項；不匹配時加第 4 個自訂選項）
- `SettingsDialog(ctk.CTkToplevel)`：
  - `__init__(self, master, config_path: Path, *, open_path: Callable[[Path], None])`
  - 3 個分區（OCR / 命名 / LLM），widget 存成 `self.<name>_entry`/`_var`/`_menu` 供測試存取（比照 `gui.py` 慣例）
  - `_on_save`：組出 `CommonSettings` → `apply_common_settings`；`ConfigError` 時 `messagebox.showerror` 且不關窗；成功則 `self.destroy()`
  - `_on_cancel`：直接 `self.destroy()`，不寫檔
  - `_on_advanced`：`ensure_config_file(self._config_path)` 後呼叫注入的 `open_path`

### `src/pdf_ocrer/gui.py`
- import `SettingsDialog`
- 刪除 `_edit_config`（其開檔邏輯已抽到 `config.ensure_config_file` + `SettingsDialog._on_advanced`）
- `edit_config_button` 的 `command` 改指向新方法：
  ```python
  def _open_settings_dialog(self) -> None:
      dialog = SettingsDialog(self, self._config_path, open_path=self._open_path)
      dialog.grab_set()
      self.wait_window(dialog)
  ```
- `_edit_prompt`（編輯命名規則按鈕）不動。

## 測試

- **`tests/test_config.py`**（新增，無 tkinter 依賴）：
  - `apply_common_settings` 保留註解與未相關 key（用 `config.example.toml` 或最小 fixture）
  - 8 個欄位正確寫入且可被 `load_config` 讀回
  - 選 medium 時 `det_model_name`/`rec_model_name` key 被移除而非寫 null
  - 檔案不存在時會建立（無 example 檔 / 有 example 檔兩種情況）
  - 驗證失敗（如 dpi 超出 72–600）時拋 `ConfigError` 且檔案內容 byte-for-byte 不變
  - `ensure_config_file` 三種情境（建立/複製/已存在不動）
- **`tests/test_settings_dialog_logic.py`**（新檔，純函式、無 display）：
  - 已知 tiny/small/medium pair 對應的 label
  - 不匹配（含手動亂打）pair 回傳自訂 label
  - 下拉選單在已知/不匹配情況下分別回傳 3/4 個選項
  - `model_pair_for_label` 在自訂 label 時原樣回傳 `current`
- **`tests/test_settings_dialog.py`**（新檔，比照 `tests/test_gui.py` 的 display-probe fixture）：
  - 對話框開啟時正確帶入現有設定值（含模型大小下拉選中 tiny/自訂等情境）
  - 存檔驗證失敗時顯示錯誤且不關窗、檔案不變
  - 合法存檔後 `load_config` 反映新值且對話框關閉
  - 取消不寫檔
  - 「進階設定」按鈕呼叫注入的 `open_path`、不寫檔
- **`tests/test_gui.py`**（新增 1 個測試）：`edit_config_button` 觸發 `_open_settings_dialog`，monkeypatch `SettingsDialog` 驗證建構參數含正確 `config_path`。

## 實作順序

1. `pyproject.toml` 加 `tomlkit`，重裝 venv
2. `config.py`：`ensure_config_file` / `CommonSettings` / `apply_common_settings` / `read_common_settings` + `test_config.py` 對應測試
3. `settings_dialog.py` 純邏輯函式 + `test_settings_dialog_logic.py`
4. `settings_dialog.py`：`SettingsDialog` widget 本體
5. `test_settings_dialog.py` GUI smoke tests
6. `gui.py` 佈線改動 + `test_gui.py` 新增測試
7. 手動驗證（見下）

## 驗證方式

1. 執行 `pytest -q`（含新測試）全綠
2. 本機（有 display 環境）跑 `pdf-ocrer-gui`，點「編輯設定」確認彈出新視窗、欄位正確帶入目前 `config.toml` 內容
3. 改幾個欄位存檔後，用 `git diff config.toml` 確認：只有目標欄位變動、原本的中文註解仍在
4. 測試模型大小切到 tiny/small/medium 分別存檔，確認 `det_model_name`/`rec_model_name` 寫入或移除符合預期
5. 測試填入無效 dpi（如 9999）確認跳出錯誤且不關窗、檔案未被寫壞
6. 點「進階設定（文字檔）」確認開啟記事本行為與原本一致
