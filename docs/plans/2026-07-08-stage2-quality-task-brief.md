# Codex 任務簡報：Stage 2 品質快贏（WO-2.1 ~ WO-2.3，依序執行）

共通約束（每個工單都適用）：

- 禁止 `git commit` / `git add`；只改該工單 Allowed Files；不重構無關程式；額外問題只記錄。
- TDD：先寫失敗測試再實作。
- 驗證：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider` + `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m ruff check src tests scripts`（無 display 時 GUI 測試 skip 屬預期）。
- 單元測試不得依賴真 paddle/rapidocr/網路/display。
- 回報格式：變更檔案清單、pytest 最後 3 行 + ruff 結果、驗收項落實位置（檔:函式）、偏離處。

---

## WO-2.1：側躺頁反白修正（/Rotate 頁文字層方向）

### 背景（已用 pymupdf 1.28 實測定位，照做不需重新研究）
`add_text_layer` 的 `morph=(baseline, Matrix(sx, 1.0))` 只有 x 縮放、未補償頁面旋轉：
/Rotate 90/270 的頁面反白框在顯示空間變成垂直（w/h 對調）、180 反向。
**修法一行**：`src/pdf_ocrer/pdf_processor.py` 的 insert_text morph 改為
`morph=(baseline, pymupdf.Matrix(sx, 1.0) * pymupdf.Matrix(page.rotation))`。
rotation=0 時 `Matrix(0)` 為恆等矩陣、零回歸；旋轉保長度，fontsize/x-scale 計算不變。

### 規格
1. `tests/fixtures_gen.py`：`build_rotated(path: Path, rotation: int = 90)` 泛化（`set_rotation(rotation)`）；`build_all` 產出 `rotated.pdf`（=90，檔名別名保留）與 `rotated_180.pdf`、`rotated_270.pdf`。`tests/test_fixtures_gen.py` 對應斷言（三檔存在、rotation 值正確）。
2. `tests/test_pdf_processor.py`：以 rotation 參數化（90/180/270 至少，含 0 對照）新增測試取代既有 `test_add_text_layer_rotated_page_search_rect_is_unrotated`（該測試只釘起點、現況也過，太弱）。驗收斷言：
   - 對旋轉 fixture 跑 render→假引擎或直接以已知 display-space poly 呼叫 `add_text_layer`（沿用該檔既有測試的做法——用合成 OcrLine，poly 為 display 空間墨跡 bbox）
   - save→reopen→`page.get_text("words")` 取每字 rect→經 `page.rotation_matrix` 映回顯示空間並正規化（x0>x1 時交換）
   - 斷言顯示空間 bbox 水平（height < width，對橫排行）且與墨跡 bbox 誤差 <3pt（x0/y0/寬）
3. `src/pdf_ocrer/pdf_processor.py`：套用一行 morph 修正；模組頂部 docstring 的 probe 筆記補一行（2026-07-08 rotation 補償）。
4. `README.md` + `README.zh-TW.md`：「已知限制」中移除「旋轉顯示頁反白偏移」項（保留 skew 與無 /Rotate 標記掃描件的敘述）。
5. `tests/test_integration.py`：既有 rotated 整合測試若有斷言與新行為衝突，更新為顯示空間正確性斷言（integration marker，預設不跑，宿主會驗）。

### Allowed Files
`src/pdf_ocrer/pdf_processor.py`、`tests/fixtures_gen.py`、`tests/test_fixtures_gen.py`、`tests/test_pdf_processor.py`、`tests/test_integration.py`、`README.md`、`README.zh-TW.md`

### Acceptance Criteria
- [ ] 新參數化測試在修正前失敗（90/270 至少一項紅）、修正後全綠
- [ ] rotation=0 路徑既有測試不變綠
- [ ] 全 suite 綠 + ruff 過

---

## WO-2.2：檔案日誌（logging 落地）

### 規格
1. 新 `src/pdf_ocrer/app_logging.py`：
   ```python
   def setup_logging(cfg: LoggingConfig) -> Path | None:
   ```
   - `cfg.enabled=False` → 不裝 handler、回 None
   - 目標 logger：`logging.getLogger("pdf_ocrer")`（**不碰 root**，避免 paddle/openai 噪音進檔）；`logger.setLevel(cfg.level)`；`propagate` 維持預設
   - Handler：`RotatingFileHandler(log_dir / "pdf_ocrer.log", maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")`，formatter `"%(asctime)s %(levelname)s %(name)s: %(message)s"`
   - 目錄：`cfg.dir` 非空用之；空 → `%LOCALAPPDATA%\pdf_ocrer\logs`（`os.environ.get("LOCALAPPDATA")` 缺失 → `Path.home() / ".pdf_ocrer" / "logs"`）；`mkdir(parents=True, exist_ok=True)`
   - **冪等**：以 handler 自訂屬性（如 `_pdf_ocrer_file_handler = True`）辨識，已存在同類 handler 就不重複裝（回傳現有路徑）
   - 目錄建立/寫入失敗 → 不拋例外，回 None（日誌不可用不能擋主流程）
2. `src/pdf_ocrer/config.py`：新 `LoggingConfig` frozen dataclass + `[logging]` section：
   - `enabled: bool = True`、`level: str = "INFO"`（casefold 驗證 ∈ {DEBUG, INFO, WARNING, ERROR}，存正規大寫）、`dir: str = ""`
   - `AppConfig` 加 `logging: LoggingConfig = LoggingConfig()`（預設值欄位，既有建構不破）
   - `config.example.toml` 增 `[logging]` 三鍵中文註解（含預設路徑說明）
3. 接線：
   - `src/pdf_ocrer/cli.py` `main`：`load_config` 成功後呼叫 `setup_logging(cfg.logging)`；`ConfigError` 分支改為先 `setup_logging(LoggingConfig())` 再 `logging.getLogger("pdf_ocrer").error(...)` + 既有 print + exit 1
   - `src/pdf_ocrer/gui.py`：`run_gui` 載入 config 後同樣呼叫；worker 例外處理處加 `logging.getLogger(__name__).exception(...)`（既有 error 事件流不變）
   - `src/pdf_ocrer/pipeline.py`：模組級 `_logger = logging.getLogger(__name__)`；`run_batch` 開始（資料夾、檔數）、每個 FileResult（source.name、status.value、output_name、note）、結束 summary 各一行 INFO；**log_cb 呼叫與內容完全不動**
   - `src/pdf_ocrer/llm_namer.py`：LLM 失敗 fallback 處加 `_logger.warning`（既有 log_cb 不動）
4. 測試 `tests/test_app_logging.py`（新）：dir 指到 tmp_path 驗證檔案產生與內容含 level/訊息；level 過濾（DEBUG 訊息在 INFO 設定下不落地）；冪等（呼叫兩次 handler 只一個）；enabled=false 回 None 不建檔；LOCALAPPDATA 缺失 fallback（monkeypatch.delenv）。`tests/test_config.py` 增 `[logging]` 解析/驗證/預設測試。`tests/test_pipeline.py` 增：跑一個 batch 後 log 檔含每檔一行狀態（dir 指 tmp_path）。
5. 多進程注意（只寫註解不實作）：`app_logging.py` docstring 註明「worker 行程請用 QueueHandler → 主行程 QueueListener 餵同一 handler（Stage 4 實作）」。

### Allowed Files
`src/pdf_ocrer/app_logging.py`（新）、`src/pdf_ocrer/config.py`、`src/pdf_ocrer/cli.py`、`src/pdf_ocrer/gui.py`、`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/llm_namer.py`、`config.example.toml`、`tests/test_app_logging.py`（新）、`tests/test_config.py`、`tests/test_pipeline.py`、`tests/test_cli.py`

### Acceptance Criteria
- [ ] CLI 一次批跑後 log 檔存在且含 batch 開始/每檔/結束行（測試用 `[logging] dir` 指 tmp_path）
- [ ] GUI/CLI 顯示輸出（log_cb 流）與 Stage 1 完全相同（既有測試不需改斷言）
- [ ] 全 suite 綠 + ruff 過

---

## WO-2.3：全文 txt 匯出

### 規格
1. `src/pdf_ocrer/pdf_processor.py`：`PdfResult` 加欄位 `page_texts: list[str]`（每頁一項，含 kept_existing 頁的 `page.get_text()` 內容；OCR 頁同理；順序=頁序）。既有 `text` 欄位維持（= `"\n\n".join(非空頁)` 或既有組法，行為不變）。
2. `src/pdf_ocrer/pipeline.py`：`_text_for_naming` 改用 `page_texts[:cfg.naming.max_pages_to_llm]` 切頁再 join + 截斷 `max_chars_to_llm`（**修正既有 bug**：現行用 `text.split("\n\n")` 數頁，頁內空行會誤計頁數）。斷言行為以新測試釘住：3 頁文件 max_pages_to_llm=2 → 只用前兩頁文字。
3. `src/pdf_ocrer/config.py`：`OutputConfig` 加 `export_txt: bool = False`；`[output]` 解析 + `config.example.toml` 註解（`# 每檔輸出 PDF 之外另存 .txt 純文字（utf-8-sig）。預設 false`）。
4. `src/pdf_ocrer/pipeline.py` `_finalize_processed_file`：PDF 落地後若 `cfg.output.export_txt` 且 `any(t.strip() for t in page_texts)`：寫 `final_output.with_suffix(".txt")`，encoding `utf-8-sig`，格式：每頁前一行 `--- 第 {i} 頁 ---`（i 從 1），頁間空一行；全空則不寫。copy2 路徑（已有文字層檔）同樣適用。txt 寫入失敗 → log 警告不中斷（沿用檔案級 try/except 之外自行 try）。
5. 測試：`tests/test_pdf_processor.py` 驗 `page_texts` 長度=頁數；`tests/test_pipeline.py` 驗 export_txt on/off、內容含分隔行與兩頁文字、全空不產檔、已有文字層檔也產出、`_text_for_naming` 修正（頁內含空行時 max_pages 正確）。

### Allowed Files
`src/pdf_ocrer/pdf_processor.py`、`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/config.py`、`config.example.toml`、`tests/test_pdf_processor.py`、`tests/test_pipeline.py`、`tests/test_config.py`

### Acceptance Criteria
- [ ] export_txt=true 時輸出 `<新檔名>.txt`（utf-8-sig、頁分隔正確）；false 時不產
- [ ] `_text_for_naming` 頁數計算 bug 修正有測試釘住
- [ ] 全 suite 綠 + ruff 過

---

## WO-2.4：Review 修正（Sonnet-5 review 仲裁後的窄修）

### Objective
修正四個 Accepted findings，不做其他任何事。

### 規格
1. **P1（txt 撞名蓋檔）**：`src/pdf_ocrer/llm_namer.py` `_stem_unavailable` 改為同時檢查
   `(out_dir / f"{stem}.pdf").exists()` **或** `(out_dir / f"{stem}.txt").exists()` 或 `stem.casefold() in used`。
   理由：txt 匯出與 PDF 共用 stem，collision 解析必須把 txt 命名空間一起佔住，否則
   `_write_txt_export` 會無聲覆蓋不相關的既有 .txt。
   測試（tests/test_llm_namer.py）：輸出目錄先放 `X.txt` → resolve_collision("X") 回 "X_2"。
2. **P2（測試汙染真實 LOCALAPPDATA）**：`tests/conftest.py` 加 **autouse** fixture：
   - `monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory 產生的專用目錄))`（session 或 function 級皆可，function 級較乾淨）
   - teardown：從 `logging.getLogger("pdf_ocrer")` 移除並 close 所有帶 `_pdf_ocrer_file_handler` 標記的 handler
   驗收：跑 `tests/test_gui.py` 或任一 CLI 測試後，真實 `%LOCALAPPDATA%\pdf_ocrer\logs` 不產生/不更新（測試內以 monkeypatch 後的路徑斷言）。
3. **P2（Unicode 例外）**：`src/pdf_ocrer/pipeline.py` `_write_txt_export` 的 `write_text`
   加 `errors="replace"`（lone surrogate 等編碼問題以替代字元落地，不拋例外）；except 範圍維持 OSError。
   測試：page_texts 含 `"bad\udc80x"` → 不拋、txt 產出含替代字元、FileResult 仍為成功狀態。
4. **P3（命名輸入尾隨換行）**：`src/pdf_ocrer/pipeline.py` `_text_for_naming` 對每頁先 `.strip()` 再 join
   （空頁 strip 後為空字串時仍保留頁序切片語意：先切 `[:max_pages]` 再 strip/join）。
   測試：頁尾含 `\n` 的 page_texts → 命名輸入無多餘空行（與 v0.2.0 行為一致）。

### Allowed Files
`src/pdf_ocrer/llm_namer.py`、`src/pdf_ocrer/pipeline.py`、`tests/conftest.py`、`tests/test_llm_namer.py`、`tests/test_pipeline.py`

### Acceptance Criteria
- [ ] 四項各有測試釘住且綠
- [ ] 全 suite 綠 + ruff 過
