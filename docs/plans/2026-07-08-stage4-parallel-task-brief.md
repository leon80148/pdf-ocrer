# Codex 任務簡報：Stage 4 多檔平行 + LLM 命名重疊（WO-4.1 ~ WO-4.4，依序執行）

## 架構總覽（先讀懂再動工）

多檔平行採 **worker/協調器分工**（Windows spawn 相容）：

- **Worker 行程**（`worker.py`，每檔一任務）：開檔 → 每頁 render+OCR+文字層 → `subset_fonts` + save 到**暫存檔**（`~{uuid}.ocrtmp.pdf`，放該檔最終輸出目錄內）；全頁已有文字層則 `shutil.copy2` 原檔到暫存檔。回傳純資料 `WorkerOutcome`（絕不跨行程傳 Document/影像）。
- **協調器**（`parallel.py`，跑在既有 GUI worker thread / CLI 主執行緒）：manifest 跳過決策（提交前）、舊輸出取代刪除、**串行** LLM 命名 + collision + rename 暫存→最終 + txt 匯出 + CSV + manifest record/save + file_cb。**依提交順序 finalize**（CSV 列序/collision 後綴與循序模式一致）。命名（網路 IO）自然與其他檔案的 OCR 重疊。
- **事件流**：worker → `mp.Queue`（`("page", index, rel, page_i, page_n)` 與 `("log", msg)` tuple）→ 協調器 0.1s 輪詢 drain → 轉呼叫既有 progress_cb/log_cb → GUI 既有 queue + after(100) 機制，**GUI 零結構改動**。
- **取消**：GUI/CLI 的 `threading.Event` → 協調器偵測 → set `mp.Event`（worker 在頁間檢查）→ `executor.shutdown(cancel_futures=True)` → 已完成 OCR 的 outcome 用**後備命名**（不呼叫 LLM）保留成果、未完成刪暫存 → `cancelled=True`。
- **Warmup**：提交檔案任務前，先單獨提交 `warmup()` 並**阻塞等待**（單 worker 先載模型 → 杜絕 N worker 搶模型下載/首次快取寫入競態；也讓引擎設定錯誤一次乾淨爆出）。
- **共用 helpers**：`pipeline.py` 的 `_choose_stem`、`_write_csv_row`、`_write_txt_export`、`_remove_previous_output`、`_CSV_HEADER`、manifest 流程照舊 import 共用；循序路徑（workers=1）**原封不動**。

共通約束：禁止 git add/commit；只改該工單 Allowed Files；TDD；
驗證 `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider` + `ruff check src tests scripts`；
單元測試不得依賴真 paddle/rapidocr/網路/display；回報格式同前（變更清單/pytest+ruff/落實位置/偏離）。

---

## WO-4.1：performance config + CancelFlag Protocol

### 規格
1. `src/pdf_ocrer/config.py`：
   - 新 `PerformanceConfig` frozen dataclass + `[performance]` section：`workers: int = 1`（驗證 0 ≤ x ≤ 8；0=auto、1=循序、>1=平行）
   - `AppConfig.performance: PerformanceConfig = PerformanceConfig()`
   - 純函式（模組層級，config.py 內）：
     ```python
     def resolve_worker_count(perf: PerformanceConfig, cpu_count: int | None) -> int:
         # workers >= 1 → min(workers, 8)；workers == 0 → min(3, max(1, (cpu_count or 2) // 4))
     def resolve_cpu_threads(ocr: OcrConfig, workers: int, cpu_count: int | None) -> int:
         # ocr.cpu_threads > 0 → 原值；否則 workers > 1 → max(1, (cpu_count or 2) // (2 * workers))；workers <= 1 → 0（=函式庫預設）
     ```
   - `config.example.toml` `[performance]` 註解（`# 同時處理檔案數。1=循序（預設）；0=自動（約核心數/4，上限3）；>1 指定。每個 worker 各載一份 OCR 模型：rapidocr 約 0.7GB、paddle 約 1.3~2.5GB`）
2. `src/pdf_ocrer/pdf_processor.py`：定義
   ```python
   class CancelFlag(Protocol):
       def is_set(self) -> bool: ...
   ```
   `process_pdf`/`has_text_layer` 等 cancel 參數型別註記由 `threading.Event | None` 放寬為 `CancelFlag | None`（僅型別，行為不變；`threading.Event` 與 `multiprocessing.Event` 都滿足）。
3. 測試：resolve 兩函式真值表（含 cpu_count None、0、極端值）；config 解析/驗證/預設；CancelFlag duck-type（自訂物件帶 is_set 傳入 process_pdf 正常）。

### Allowed Files
`src/pdf_ocrer/config.py`、`src/pdf_ocrer/pdf_processor.py`、`config.example.toml`、`tests/test_config.py`、`tests/test_pdf_processor.py`

---

## WO-4.2：worker.py（可 pickle 邊界）

### 規格
新 `src/pdf_ocrer/worker.py`，**全部模組層級**（Windows spawn 可 pickle）：

```python
@dataclass(frozen=True)
class WorkerTask:
    index: int          # 提交序（finalize 排序鍵）
    source: str         # 來源絕對路徑（str）
    rel: str            # 相對輸入根 posix 路徑
    temp_output: str    # 暫存檔絕對路徑（~{uuid}.ocrtmp.pdf，位於最終輸出目錄）
    total_files: int

@dataclass(frozen=True)
class WorkerOutcome:
    index: int
    source: str
    rel: str
    kind: str                    # "ok" | "encrypted" | "failed" | "cancelled"
    temp_output: str | None      # kind=="ok" 時為實際存在的暫存檔；其他 None
    page_texts: tuple[str, ...]  # 每頁文字（naming/txt 匯出用；tuple 可 pickle 且 hashable）
    total_pages: int
    ocr_pages: int
    all_existing_text: bool      # 全頁已有文字層（決定 copy2 語意與狀態）
    note: str                    # 失敗/加密時的訊息

def init_worker(ocr_cfg: OcrConfig, debug_cfg: DebugConfig, cancel: Any, events: Any) -> None:
    # 存模組全域 _CANCEL/_EVENTS/_CFG…；mp 物件只能經 initializer/initargs 傳入
def warmup() -> str:
    # create_engine(_OCR_CFG) → recognize(8x8 白圖 np.uint8) → 回傳引擎描述字串
def process_file_task(task: WorkerTask) -> WorkerOutcome:
```

- `process_file_task`：懶初始化引擎（模組全域 `_engine_factory`，預設 `create_engine`，**測試可 monkeypatch**；引擎跨任務重用同一行程快取）；呼叫 `pdf_processor.process_pdf(Path(task.source), engine, cfg, cancel=_CANCEL, page_cb=推 ("page", index, rel, i, n) 進 _EVENTS)`——沿用 process_pdf 既有介面（讀清楚它現在的 callback/cancel 簽名再接）；
  - 全頁 kept_existing（`all_existing_text`）→ 關 doc、`shutil.copy2(source, temp_output)`（保留原 bytes 語意與循序路徑一致）
  - 否則 `subset_fonts()` + `save(temp_output, garbage=3, deflate=True)`（與 `_finalize_processed_file` 現行存檔參數一致——先讀該函式抄參數）
  - `EncryptedPdfError` → kind="encrypted"、note=訊息；`BatchCancelled` → kind="cancelled"；其他 Exception → kind="failed"、note=f"{type(exc).__name__}: {exc}"；三者 temp 檔若已建立要刪掉
  - events 推送包在 try/except（queue 壞掉不能讓 worker 崩）
- 模組 docstring 註明 pickle 邊界與 QueueHandler 日誌設計（`("log", msg)` 事件由協調器轉入 logger）。
- 測試 `tests/test_worker.py`（新，全部行程內執行，不開真子行程）：monkeypatch `worker._engine_factory` = FakeEngine；驗 ok 路徑（temp 檔存在且可搜尋、page_texts 對、事件推送順序）、native 檔 copy2 路徑（bytes 相同）、encrypted/corrupt/取消（cancel 預先 set 的 FakeCancel）三種 kind、事件 queue 用 `queue.Queue` 注入。

### Allowed Files
`src/pdf_ocrer/worker.py`（新）、`tests/test_worker.py`（新）

---

## WO-4.3：parallel.py 協調器

### 規格
新 `src/pdf_ocrer/parallel.py`：

```python
def run_batch_parallel(
    folder: Path,
    cfg: AppConfig,
    engine: OcrEngineProtocol | None,   # 未用（引擎在 worker 內各自建）；保留參數對齊 run_batch，docstring 註明
    client: LLMClient | None,
    prompt_template: str,
    progress_cb=None, log_cb=None, cancel_event=None, file_cb=None,
    force: bool = False,
    *,
    executor_factory: Callable[[], Executor] | None = None,  # 預設：spawn ProcessPoolExecutor + init_worker
    worker_fn: Callable[[WorkerTask], WorkerOutcome] | None = None,  # 預設 worker.process_file_task；測試注入
    warmup_fn: Callable[[], Any] | None = None,
    events_queue: Any | None = None,     # 預設 mp context Queue；測試 queue.Queue
    worker_cancel: Any | None = None,    # 預設 mp context Event；測試 threading.Event
) -> BatchSummary:
```

流程（照抄循序 run_batch 的既有語意，只是資料來源變 outcome）：
1. 掃描 `scan_inputs`；載 manifest；`should_skip` 命中者直接產 SKIPPED_DONE 結果（不提交）；建輸出目錄；**清掃殘留暫存**（`output_dir` rglob `~*.ocrtmp.pdf` 刪除 + logger warning 計數）
2. 預設 executor：`ProcessPoolExecutor(max_workers=resolve_worker_count(...), mp_context=multiprocessing.get_context("spawn"), initializer=worker.init_worker, initargs=(ocr_cfg_with_resolved_threads, cfg.debug, worker_cancel, events_queue))`；其中 ocr_cfg 的 cpu_threads 先經 `resolve_cpu_threads` 換算後以 `dataclasses.replace` 固化
3. **先提交 `warmup_fn` 並 `.result()` 阻塞**（log_cb「正在載入 OCR 模型…（平行模式，N 個工作行程）」）；warmup 失敗 → 全批 FAILED 語意：logger.exception + 每檔 FAILED 結果 + 正常收尾（不掛死）
4. 為每個未跳過檔案建 `WorkerTask`（index 按掃描序；temp_output 用 `uuid4().hex`；**先確保鏡像輸出子目錄存在**）並全部 submit
5. 主迴圈：`events_queue.get(timeout=0.1)` drain（"page" → progress_cb(files_done+1, file_n, page_i, page_n, rel)；"log" → log_cb+logger）；檢查 `cancel_event.is_set()` → set worker_cancel + `executor.shutdown(wait=False, cancel_futures=True)` 進入取消收尾；輪詢 done futures 收 outcome 入 dict；**連續的 next-index outcome 依序 finalize**
6. finalize（每個 outcome，串行）：
   - kind=="ok"：先 `_remove_previous_output`（manifest 舊 entry，沿用 pipeline 函式）→ `_choose_stem`（naming per-dir used_stems 沿用 pipeline 邏輯/函式）→ `Path(temp).rename(final)`（同目錄原子）→ `_write_txt_export`（用 outcome.page_texts）→ CSV 列（懶建 CSV，沿用 pipeline 寫法與 `_CSV_HEADER`）→ manifest.record+save → FileResult → file_cb
   - kind=="encrypted" → SKIPPED_ENCRYPTED（CSV 列 + manifest record 照循序語意）；"failed" → FAILED（CSV 列、不記 manifest）；"cancelled" → 刪 temp、不出 CSV，計入取消
   - 取消收尾：已完成 ok 的 outcome 用後備命名（`llm_namer` fallback 路徑：stem+fallback_suffix，經 collision）、note=「已取消-使用備用檔名」；pending/未完成刪 temp
7. `BrokenProcessPool` → 未完成檔 FAILED（note 註明）、logger.exception、正常回傳
8. BatchSummary 組裝與循序一致（csv_path 懶建可 None、cancelled 旗標、results 依 index 序）

`src/pdf_ocrer/pipeline.py`：`run_batch` 開頭 `workers = resolve_worker_count(cfg.performance, os.cpu_count())`；`workers > 1` 時 `from pdf_ocrer.parallel import run_batch_parallel`（函式內 import 避免循環）轉呼叫並回傳；`workers <= 1` 走既有路徑**一行都不改**。必要時把 finalize 內用到的私有 helper 保持模組層級可 import（現況已是）。

測試 `tests/test_parallel.py`（新）：
- 注入 `ThreadPoolExecutor` + 假 worker_fn（行程內）+ `queue.Queue` + `threading.Event`：
  - 亂序完成（用 event 控制假 worker 完成順序）→ CSV 列序 = 提交序、collision 後綴決定性
  - LLM 命名與 OCR 重疊語意不驗時序，只驗結果正確
  - 取消：兩檔完成後 set cancel_event → 已完成者後備命名保留、第三檔無輸出、temp 清空、summary.cancelled
  - warmup 先行（記錄呼叫順序）、warmup 失敗 → 全 FAILED 不掛死
  - 殘留暫存清掃、SKIPPED_DONE 不提交、manifest record 正確、page 事件 → progress_cb 轉譯
- **真多進程冒煙（不依賴 paddle）**：`run_batch`（`workers=2`，config 帶 performance）跑 `native.pdf`（kept_existing，引擎懶初始化**不會**被觸發）+ `corrupt.pdf`（failed）→ 驗真 spawn/initargs/Queue/Event 佈線與 rename/CSV 端到端。若單測太慢（>25s）改掛 `@pytest.mark.slow` 並於回報註明（不可掛 integration——這條必須進預設套件）。真引擎平行測試**不在本工單**（Stage 收尾由 PM 跑）。

### Allowed Files
`src/pdf_ocrer/parallel.py`（新）、`src/pdf_ocrer/pipeline.py`、`tests/test_parallel.py`（新）、`tests/test_pipeline.py`

---

## WO-4.4：佈線（CLI/GUI/設定視窗/日誌）

### 規格
1. `src/pdf_ocrer/cli.py`：`--workers N`（0~8，覆寫 `cfg.performance.workers`，dataclasses.replace）；help 註明 0=auto/1=循序。
2. `src/pdf_ocrer/settings_dialog.py` + `config.py` 的 `CommonSettings`/`read_common_settings`/`apply_common_settings`：
   - `CommonSettings` 加 `engine: str`、`workers: int`
   - OCR 區加「OCR 引擎」下拉（值 `paddle`/`rapidocr`，顯示可加註「rapidocr（推薦，較快）」但寫檔值必須是純 `paddle`/`rapidocr`）與「同時處理檔案數」數字欄（0~8，附說明 label「0=自動、1=循序」）
   - tomlkit round-trip 寫入 `[ocr] engine` 與 `[performance] workers`（沿用既有 apply 模式與驗證；`[performance]` section 不存在時建立）
3. QueueListener 日誌（Q3 seam 完成）：`src/pdf_ocrer/worker.py` 的 events 已含 ("log", msg)；本工單把 worker 內 `logging.getLogger("pdf_ocrer.*")` 的記錄轉送補全——**簡化實作**：`init_worker` 對 `pdf_ocrer` logger 掛一個自訂 Handler（模組層級 class `_EventQueueHandler(logging.Handler)`，emit → `_EVENTS.put(("log", record.getMessage()))`，level=INFO）；協調器把 ("log",...) 寫入本行程 logger（已在 WO-4.3 完成）＋log_cb。
4. README×2：平行處理章節（workers 設定、記憶體預算、預設循序、建議 ≥6 檔再開、每 worker 各載模型）；settings dialog 章節截圖文字更新（若 README 有列設定項清單）。
5. 測試：`tests/test_cli.py` `--workers` 傳遞；`tests/test_settings_dialog_logic.py`/`test_settings_dialog.py` 新欄位 round-trip（含 `[performance]` 新建、engine 值合法性驗證失敗檔案不變）；`tests/test_worker.py` `_EventQueueHandler` emit 進 queue。
6. **預設佈線 spawn 測試**（`tests/test_parallel.py` 補一條，進預設套件）：`run_batch`（不注入任何 factory，`[performance] workers=2`、預設 paddle 引擎設定）跑 `native.pdf`（全頁已有文字層 → 引擎只建構不 recognize，**不會**載入 paddle 模型）+ `corrupt.pdf`（failed）→ 驗證**真正的** `init_worker`/initargs（mp.Event、mp.Queue）/`worker.process_file_task` 生產佈線與 rename/CSV 端到端。斷言：native → 已有文字層-僅命名、corrupt → 失敗、CSV 兩列、無殘留 `~*.ocrtmp.pdf`。預期耗時 10~25 秒（spawn+import），若超過 25 秒改掛 `@pytest.mark.slow` 並回報。

### Allowed Files
`src/pdf_ocrer/cli.py`、`src/pdf_ocrer/settings_dialog.py`、`src/pdf_ocrer/config.py`、`src/pdf_ocrer/worker.py`、`README.md`、`README.zh-TW.md`、`tests/test_cli.py`、`tests/test_settings_dialog_logic.py`、`tests/test_settings_dialog.py`、`tests/test_config.py`、`tests/test_worker.py`、`tests/test_parallel.py`

---

## WO-4.5：warmup 語意回復（PM 駁回 WO-4.4 的偏離）

### 背景
WO-4.4 為了讓「預設佈線 spawn 測試」不載入 paddle 模型，把 `worker.warmup()` 弱化成只建構引擎不
`recognize`。**駁回**：warmup 的存在目的就是在派發檔案任務前，由單一 worker 先完成第一次模型
載入/下載（首次使用時多 worker 同時下載模型會有快取競態）。只建構＝什麼都沒預熱。

### 規格
1. `src/pdf_ocrer/worker.py`：`warmup()` 回復為「建構引擎 + `engine.recognize(np.full((8,8,3),255,uint8))` + 回傳引擎描述字串」。
2. `tests/test_parallel.py` 的預設佈線 spawn 測試改為：呼叫 `run_batch_parallel`（**不注入** executor_factory / worker_fn / events_queue / worker_cancel——全走生產預設），**只注入** `warmup_fn=` 測試模組頂層的 no-op 函式（可 pickle）。這樣 initargs（mp.Event/mp.Queue）、真 `worker.process_file_task`、rename/CSV 全都吃到真佈線，又不觸發模型載入（native.pdf 全頁已有文字層 → 引擎懶初始化不 recognize；corrupt.pdf 開檔即失敗）。
3. `tests/test_worker.py` 若有 warmup 相關斷言同步回復（warmup 呼叫 recognize 一次、重用引擎）。

### Allowed Files
`src/pdf_ocrer/worker.py`、`tests/test_parallel.py`、`tests/test_worker.py`

### Acceptance Criteria
- [ ] `warmup()` 會 recognize 白圖（測試以 FakeEngine 斷言）
- [ ] 預設佈線 spawn 測試仍不依賴真模型且通過
- [ ] 全 suite 綠 + ruff 過

---

## WO-4.6：Review 修正（Sonnet-5 review 仲裁後的窄修）

### Objective
修正五個 Accepted findings（全在 `parallel.py` 及其測試），不做其他任何事。

### 規格
1. **P1（BrokenProcessPool 誤殺已完成檔）**：`_collect_done_futures` 改為**逐 future 獨立收割**：
   對快照中每個 `done()` 的 future 個別 try `result()`；`BrokenProcessPool`/其他例外 → 只把**該**
   pending 轉 `_exception_outcome`；絕不讓第一個例外中斷整趟迭代或往外拋。外層偵測 pool 已壞
   （任一 future 拿到 BrokenProcessPool）後：對**尚未 done** 的 pending 才標 FAILED；已完成的
   outcome 正常 finalize（暫存不刪）。測試：直接用假 future 物件重現 reviewer 場景（broken 在前、
   成功在後）→ 成功檔正常輸出、僅 broken 檔 FAILED。
2. **P2（取消不等在途 worker）**：取消分支改為：set `worker_cancel` → `executor.shutdown(wait=False,
   cancel_futures=True)` → 對仍在跑的 futures `concurrent.futures.wait(..., timeout=60)`（worker 頁間
   檢查 cancel，正常秒級返回）→ 再收割 outcome（在途完成者以 cancelled/ok 收）→ 才清暫存與
   finalize → 最後 `executor.shutdown(wait=True)`。逾時仍未歸的極端情況：log warning 後照原路收尾。
   測試：假 executor 模擬「取消當下仍在跑、稍後完成」的 future → 其暫存不被提前刪、等待後正確收割。
3. **P2（進度條倒退）**：`_handle_event` 的 "page" 轉譯改發**單調** file 索引：`file_i = min(已 finalize
   數 + 1, total_files)`（顯示名稱仍用該事件的 rel）。測試：亂序 page 事件 → progress_cb 收到的
   file_i 序列單調不減。
4. **P3（尾端事件遺失）**：主迴圈結束後的最終 drain 改為有界重試（例如迴圈 `get(timeout=0.05)` 直到
   連續兩次空或累計 0.5 秒）。
5. **P3（空鏡像目錄）**：暫存檔改放**輸出根目錄**（同卷、rename 跨目錄仍原子）；submit 迴圈**不再**
   預建鏡像子目錄；`_finalize_ok_outcome` rename 前才 `final_parent.mkdir(parents=True, exist_ok=True)`。
   殘留清掃 rglob 不受影響。測試：遞迴輸入含只有加密檔的子資料夾 → 平行模式輸出樹無空目錄（與循序一致）。
6. **文件**：README×2 平行章節加一句：「若手動設定 `cpu_threads`，平行模式下每個 worker 都沿用該值
   （總執行緒 ≈ workers × cpu_threads），建議平行時保留 `cpu_threads = 0` 讓程式自動分配。」

### Allowed Files
`src/pdf_ocrer/parallel.py`、`tests/test_parallel.py`、`README.md`、`README.zh-TW.md`

### Acceptance Criteria
- [ ] 六項各有對應測試/文件變更
- [ ] 全 suite 綠 + ruff 過
