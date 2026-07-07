# Codex 任務簡報：Stage 3 掃描與增量（WO-3.1 ~ WO-3.4，依序執行）

共通約束（每個工單都適用）：

- 禁止 `git commit` / `git add`；只改該工單 Allowed Files；不重構無關程式；額外問題只記錄。
- TDD：先寫失敗測試再實作。
- 驗證：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider` + `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m ruff check src tests scripts`。
- 單元測試不得依賴真 paddle/rapidocr/網路/display；沿用 tests/test_pipeline.py 的 FakeEngine/StaticClient 模式。
- 回報格式：變更檔案清單、pytest 最後 3 行 + ruff、驗收項落實位置（檔:函式）、偏離處。

---

## WO-3.1：遞迴掃描（scanning.py + rel 管線）

### 規格
1. 新 `src/pdf_ocrer/scanning.py`：
   ```python
   @dataclass(frozen=True)
   class ScanItem:
       src: Path
       rel: str    # POSIX 風格相對輸入根的路徑；頂層檔 = 純檔名（CSV 向後相容）

   def scan_inputs(folder: Path, output_subdir_name: str, cfg: InputConfig) -> list[ScanItem]:
   ```
   - 非遞迴（`cfg.recursive=False`，預設）：行為與現行 `_scan_pdfs` 等價（頂層 `*.pdf`、排除輸出子資料夾、大小寫不敏感排序）——排序鍵改 `rel.casefold()`（頂層時等價）
   - 遞迴：`os.walk(folder, topdown=True)`，**剪枝任何名稱等於 `output_subdir_name` 的目錄**（任意深度，含歷史巢狀輸出）；另保留現行 `resolve().is_relative_to(輸出根)` 防衛
   - 副檔名過濾：本工單僅 `.pdf`（casefold）；結構上把「允許副檔名集合」做成模組內函式（WO-3.2 會擴充）
2. `src/pdf_ocrer/config.py`：新 `[input]` section + `InputConfig` frozen dataclass：`recursive: bool = False`；`AppConfig.input: InputConfig = InputConfig()`（預設欄位）；`config.example.toml` 註解（`# 是否掃描子資料夾（輸出鏡像原資料夾結構）。預設 false`）。
3. `src/pdf_ocrer/pipeline.py`：
   - `run_batch` 改用 `scan_inputs`；刪除/改寫 `_scan_pdfs`（保留名稱轉發亦可，測試不得再依賴舊私有函式）
   - `FileResult` 加 `rel: str = ""`（預設空字串，既有建構相容；填入 ScanItem.rel）
   - 輸出鏡像：每檔輸出目錄 = `output_dir / PurePosixPath(rel).parent`，`mkdir(parents=True, exist_ok=True)` 惰性建立
   - collision per-directory：`used_stems` 改 `dict[str, set[str]]`（鍵 = rel 的 parent posix 字串；`resolve_collision` 呼叫不變、傳對應目錄與集合）
   - CSV：`原檔名` 欄寫 `rel`、`新檔名` 欄寫相對輸出根的 posix 路徑（頂層檔兩者都是純檔名 → 向後相容）
   - progress_cb 的 name 參數與 log 行改用 rel
4. `src/pdf_ocrer/gui.py`：狀態表格列鍵與顯示改用 `FileResult.rel` / progress 事件的 rel（修同名檔在不同子資料夾撞列問題）。
5. `src/pdf_ocrer/cli.py`：`--recursive` flag → `dataclasses.replace(cfg.input, recursive=True)`。
6. 測試：新 `tests/test_scanning.py`（pdf 過濾、輸出目錄排除＋巢狀 decoy、rel 計算、排序、遞迴 on/off）；`tests/test_pipeline.py` 巢狀樹 e2e（fixture 複製成 `sub/a.pdf` 等，驗輸出鏡像、CSV rel、per-dir collision：兩個子資料夾同名檔互不加後綴）；`tests/test_cli.py` `--recursive`；`tests/test_gui.py` 列鍵 rel。README×2 遞迴章節。

### Allowed Files
`src/pdf_ocrer/scanning.py`（新）、`src/pdf_ocrer/config.py`、`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/gui.py`、`src/pdf_ocrer/cli.py`、`config.example.toml`、`tests/test_scanning.py`（新）、`tests/test_pipeline.py`、`tests/test_config.py`、`tests/test_cli.py`、`tests/test_gui.py`、`README.md`、`README.zh-TW.md`

### Acceptance Criteria
- [ ] recursive=false 時輸出行為與 v0.3.0 完全一致（既有測試不改斷言即綠）
- [ ] recursive=true：巢狀輸入 → 鏡像輸出、CSV rel 正確、任意深度輸出目錄被剪枝
- [ ] 全 suite 綠 + ruff 過

---

## WO-3.2：圖片檔輸入（jpg/png/tiff → 可搜尋 PDF）

### 背景（已實測，照做）
`pymupdf.open(影像檔)` → `convert_to_pdf()` → `pymupdf.open("pdf", bytes)` 可得可處理文件；
內嵌 DPI 受尊重（200dpi PNG → A4 尺寸頁）；**多頁 TIFF 原生支援**（2 frame → page_count 2）；
缺 DPI 標記時 MuPDF 假設 96（真 96 無法區分 → 不做啟發式，僅文件註明）。

### 規格
1. `src/pdf_ocrer/config.py`：`InputConfig` 加 `image_extensions: tuple[str, ...] = ("jpg", "jpeg", "png", "tif", "tiff")`：TOML list[str] → 正規化（去前導點、casefold、去重保序）、全字串驗證；`[]` = 停用圖片輸入。example.toml 註解。
2. `src/pdf_ocrer/scanning.py`：允許副檔名 = `{"pdf"} ∪ set(cfg.image_extensions)`。
3. `src/pdf_ocrer/pdf_processor.py`：
   ```python
   def open_document(src: Path) -> pymupdf.Document:
   ```
   - `.pdf`（casefold）→ `pymupdf.open(src)`；其他 → `pymupdf.open(src)` → `convert_to_pdf()` → `pymupdf.open("pdf", pdf_bytes)`（原 doc 記得 close）
   - `process_pdf` 開檔處改用 `open_document`；加密檢查等其餘流程不動（影像轉出的 PDF `needs_pass=False` 自然通過）
   - 壞影像 → pymupdf 拋例外 → 既有 FAILED 路徑（不需新處理）
4. 輸出檔名：沿用 `src.stem`（例：`掃描.jpg` → `掃描_OCR.pdf`；LLM 命名照常）。輸出一律 `.pdf`。
5. `tests/fixtures_gen.py`：`build_image_png(path)`、`build_image_jpg(path)`（用 `_new_native_doc` 頁 `get_pixmap(dpi=200)` 的 `pix.tobytes("png"/"jpeg")` 寫檔）、`build_tiff_multipage(path)`（Pillow 兩 frame，`save(..., save_all=True, append_images=[...], dpi=(200,200))`）。`pyproject.toml` dev extras 加 `"pillow"`（venv 已有，不需安裝）。
6. 測試：`tests/test_pdf_processor.py`：png → process_pdf 出 1 頁、頁尺寸≈A4（dpi 尊重）、文字層可搜尋（FakeEngine）；tiff → total_pages==2。`tests/test_scanning.py`：資料夾混 pdf+jpg+png 全掃到、`image_extensions=[]` 只掃 pdf。`tests/test_pipeline.py`：影像 e2e（輸出 `X_OCR.pdf` + CSV 列）。README×2：輸入格式章節 + 96dpi 假設註明。

### Allowed Files
`src/pdf_ocrer/config.py`、`src/pdf_ocrer/scanning.py`、`src/pdf_ocrer/pdf_processor.py`、`pyproject.toml`、`config.example.toml`、`tests/fixtures_gen.py`、`tests/test_fixtures_gen.py`、`tests/test_pdf_processor.py`、`tests/test_scanning.py`、`tests/test_pipeline.py`、`tests/test_config.py`、`README.md`、`README.zh-TW.md`

### Acceptance Criteria
- [ ] jpg/png/多頁 tiff → 可搜尋 PDF 輸出 + CSV 列；`image_extensions=[]` 恢復僅 PDF
- [ ] 全 suite 綠 + ruff 過

---

## WO-3.3：增量 manifest 模組（純邏輯）

### 規格
新 `src/pdf_ocrer/manifest.py`（不碰 pipeline；純模組 + 測試）：

```python
MANIFEST_NAME = ".pdf_ocrer_manifest.json"

@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int
    @classmethod
    def from_stat(cls, path: Path) -> "FileIdentity": ...

@dataclass(frozen=True)
class ManifestEntry:
    size: int
    mtime_ns: int
    output: str | None      # 相對輸出根的 posix 路徑；SKIPPED_ENCRYPTED 為 None
    status: str             # FileStatus.value
    completed_at: str       # isoformat

class Manifest:
    @classmethod
    def load(cls, path: Path) -> "Manifest":   # 不存在/壞 JSON/version!=1 → 空 manifest + logger.warning
    def should_skip(self, rel: str, identity: FileIdentity, output_root: Path) -> ManifestEntry | None:
    def record(self, rel: str, identity: FileIdentity, status: str, output: str | None) -> None:
    def save(self, path: Path) -> None:        # tmp 檔 + os.replace 原子寫；寫失敗 logger.warning 不拋
```

- JSON schema：`{"version": 1, "entries": {rel: {"size", "mtime_ns", "output", "status", "completed_at"}}}`（UTF-8、indent=1 或 2、ensure_ascii=False）
- `should_skip` 規則（全要件）：entry 存在且 `size`/`mtime_ns` 完全相等，且：
  - status ∈ {"OCR完成", "已有文字層-僅命名", "無文字-原樣輸出"} 時需 `(output_root / entry.output).exists()`
  - status == "加密-跳過" → 不需 output 存在
  - 其他 status（含 "失敗"）→ None（record 端本來就不會寫入失敗，防禦性排除）
- `record`：status == "失敗" 或 "已取消..."（任何非上述四者）→ **不寫入**（保持每次重試）；成功類覆寫舊 entry
- `completed_at`：`datetime.now().astimezone().isoformat(timespec="seconds")`
- 測試 `tests/test_manifest.py`（新）：round-trip、壞 JSON/缺版本容忍、決策矩陣（身分不符/輸出被刪/加密特例/失敗不記錄）、原子寫（save 後 tmp 檔不殘留）、record 失敗狀態被忽略。

### Allowed Files
`src/pdf_ocrer/manifest.py`（新）、`tests/test_manifest.py`（新）

### Acceptance Criteria
- [ ] 決策矩陣測試全綠；全 suite 綠 + ruff 過

---

## WO-3.4：增量整合（pipeline / CLI / GUI）

### 規格
1. `src/pdf_ocrer/pipeline.py`：
   - `FileStatus` 加 `SKIPPED_DONE = "已處理-跳過"`
   - `run_batch`：`incremental = cfg.output.incremental and not force`（`force` 為 run_batch 新參數 `force: bool = False`）；開始時 `Manifest.load(output_dir / MANIFEST_NAME)`；每檔處理前 `should_skip(rel, FileIdentity.from_stat(src), output_dir)` 命中 → 產 `FileResult(status=SKIPPED_DONE, output=entry.output 的檔名, rel=..., note="")`、進 summary/results、呼叫 file_cb、**不寫 CSV**、continue；處理完成後 `manifest.record(...)` + `manifest.save(...)`（每檔，含加密-跳過；失敗不記錄）
   - **CSV 惰性建立**：第一次真正要寫列才開檔寫 header；整批無寫列 → 不產 CSV、`BatchSummary.csv_path = None`（檢查既有欄型別與消費端：`cli.py` 印 CSV 行、`gui.py` 完成訊息/開啟對照表——None 時 CLI 不印、GUI 不開並在訊息註明「本次無新處理檔案」）
   - log：跳過檔輸出一行 log_cb（`已處理-跳過（先前已完成）: {rel}`）與 logger INFO
2. `src/pdf_ocrer/config.py`：`OutputConfig.incremental: bool = True` + `[output]` 解析 + example.toml 註解（`# 增量處理：跳過 manifest 記錄且輸出仍存在的已完成檔。預設 true`）。
3. `src/pdf_ocrer/cli.py`：`--force` flag（說明「忽略增量記錄，全部重新處理」）→ 傳 `run_batch(force=True)`；exit code 邏輯：全部 SKIPPED_DONE 視為成功（現行以 FAILED 數判斷者維持）。
4. `src/pdf_ocrer/gui.py`：開始按鈕旁加 CTkCheckBox「全部重新處理」（預設不勾）→ worker 呼叫 `run_batch(force=checkbox)`；完成 messagebox 統計加「已跳過（先前已處理）：N」；狀態表格 SKIPPED_DONE 列正常顯示（狀態欄顯示枚舉值）。
5. 測試：`tests/test_pipeline.py`：第二次跑全 SKIPPED_DONE 且無新 CSV；touch 來源（改 mtime_ns）→ 重處理；刪輸出 → 重處理；`force=True` → 全重跑且 manifest 更新；加密檔第二次跑仍 SKIPPED_DONE（無輸出要求）；失敗檔第二次跑重試。`tests/test_cli.py`：`--force` 傳遞、全跳過 exit 0、csv None 不印。`tests/test_gui.py`：checkbox 傳遞（monkeypatch run_batch 斷言 force）、完成訊息含跳過數、csv None 不呼叫 startfile。README×2 增量章節（含 `--force`）。

### Allowed Files
`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/config.py`、`src/pdf_ocrer/cli.py`、`src/pdf_ocrer/gui.py`、`config.example.toml`、`tests/test_pipeline.py`、`tests/test_config.py`、`tests/test_cli.py`、`tests/test_gui.py`、`README.md`、`README.zh-TW.md`

### Acceptance Criteria
- [ ] 重跑同資料夾：已完成檔秒回 SKIPPED_DONE、不產新 CSV；中斷後重跑從未完成檔續作
- [ ] `--force`/GUI checkbox 全重跑；來源變動/輸出被刪自動重做
- [ ] 全 suite 綠 + ruff 過

---

## WO-3.5：Review 修正（Sonnet-5 review 仲裁後的窄修）

（仲裁紀錄：NAS mtime_ns 穩定性已在實際 NAS 實測——重複 stat 值完全一致且保留次秒精度，
身分比對維持精確等值不放寬；TOCTOU 風險 Rejected——下輪自癒。）

### Objective
修正七個 Accepted findings，不做其他任何事。

### 規格
1. **P0（重跑改檔留舊版孤兒）**：來源變更或輸出遺失導致重處理時，必須**取代**先前輸出而非產生 `_2` 副本：
   - `src/pdf_ocrer/manifest.py`：加公開方法 `get(rel) -> ManifestEntry | None`
   - `src/pdf_ocrer/pipeline.py` `run_batch`：檔案未命中 should_skip 但 `manifest.get(rel)` 有舊 entry 且 `entry.output` 非 None 時，於 `process_pdf` 成功後、finalize 命名前：刪除 `(output_dir / entry.output)` 與其 `.with_suffix(".txt")`（存在才刪；`missing_ok=True`）；log_cb + logger INFO「取代舊輸出: {entry.output}」；刪除失敗（PermissionError 等）→ logger warning、照舊流程繼續（頂多回到 `_2` 行為）
   - 測試：來源改 mtime 重跑 → 輸出目錄仍只有一個 `X_OCR.pdf`（無 `_2`）、內容為新版、舊 txt 一併清掉；輸出被刪重跑 → 正常單一輸出
2. **P2（junction 迴圈）**：`src/pdf_ocrer/scanning.py` 遞迴走訪加已訪集合：每個 `dirpath` 以 `Path(dirpath).resolve()` 記錄，`dirnames` 過濾掉 resolve 後已在集合中的子目錄（防 NTFS junction/symlink 迴圈）。測試：monkeypatch `Path.resolve` 或以 `os.walk` 假資料模擬重複 resolve 路徑（不需真 junction）驗證同一實體目錄不重複產出。
3. **P2（剪枝無警告）**：`scan_inputs` 剪掉**非頂層**的同名輸出目錄時，logger.warning 一行（含被剪路徑；頂層輸出目錄剪枝為預期不記）。測試：巢狀 decoy 被剪時有 warning 記錄（caplog）。
4. **P3（取消訊息）**：`src/pdf_ocrer/gui.py` `_completion_message`：`csv_path is None` 且 `summary.cancelled` → 顯示取消語意（沿用既有取消文案），不顯示「本次無新處理檔案」。
5. **P3（死碼）**：刪除 `pipeline._scan_pdfs`。
6. **P3（日誌計數）**：`pipeline._log_batch_end` 加 `skipped_done=N`（已處理-跳過計數）。
7. **P3（組合測試）**：`tests/test_pipeline.py` 加遞迴+增量組合測試：巢狀樹跑兩輪 → 第二輪全 SKIPPED_DONE（驗 `entry.output` 巢狀相對路徑的存在檢查）。

### Allowed Files
`src/pdf_ocrer/manifest.py`、`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/scanning.py`、`src/pdf_ocrer/gui.py`、`tests/test_manifest.py`、`tests/test_pipeline.py`、`tests/test_scanning.py`、`tests/test_gui.py`

### Acceptance Criteria
- [ ] 改檔重跑不留舊版（單一輸出、txt 同步清理）
- [ ] 迴圈防護與剪枝警告有測試
- [ ] 全 suite 綠 + ruff 過
