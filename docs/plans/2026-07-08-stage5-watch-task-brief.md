# Codex 任務簡報：Stage 5 監看資料夾模式（WO-5.1 ~ WO-5.3，依序執行）

## 設計要點（先讀懂）

- **輪詢制、不引入 watchdog**：輸入資料夾常在 NAS/SMB（檔案系統事件不可靠），`poll_seconds`（預設 5 秒）列目錄負載可忽略。
- **就緒判定 = 快照相等**：檔案連續**兩輪** `(size, mtime_ns)` 相同才視為寫完（防掃描器寫一半；相等制免疫 NAS 時鐘偏移）。
- **去重靠 F1 manifest**：重啟後第一輪，先前完成的檔案會以 已處理-跳過 出現一次（使用者可見），之後由 watcher 的已處理集合靜音。
- **每輪 = 一次 `run_batch(files=ready)`**：懶 CSV 保證安靜輪不產檔；失敗檔由退避表控制重試次數。
- **單一 Event 雙用**：GUI/CLI 的停止信號同時作為 run_batch 的 cancel_event（批中停止 = 協作取消 + 跳出迴圈）。
- 單例防護 v1 不做（文件註明同一資料夾勿多開）。

共通約束：禁止 git add/commit；只改該工單 Allowed Files；TDD；
驗證 `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider` + `python -m ruff check src tests scripts`；
單元測試不得依賴真 paddle/rapidocr/網路/display、**不得真 sleep 長秒**（poll_seconds 用極小值）；回報格式同前。

---

## WO-5.1：`run_batch(files=...)` 參數

### 規格
1. `src/pdf_ocrer/pipeline.py` `run_batch` 加 keyword 參數 `files: list[ScanItem] | None = None`：
   提供時**跳過內部 `scan_inputs`**、直接用該清單（仍走 manifest 跳過、輸出目錄建立等其餘流程）；
   `None` 時行為完全不變。workers>1 分派時把 `files` 透傳給 `run_batch_parallel`（同樣加參數、同語意）。
2. 測試：`tests/test_pipeline.py` 傳入自組 ScanItem 清單（含子資料夾 rel）→ 只處理清單內檔案、掃描函式未被呼叫（monkeypatch scan_inputs 斷言）；`tests/test_parallel.py` 同樣驗透傳。

### Allowed Files
`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/parallel.py`、`tests/test_pipeline.py`、`tests/test_parallel.py`

---

## WO-5.2：watcher.py（輪詢邏輯）

### 規格
1. `src/pdf_ocrer/config.py`：新 `[watch]` section + `WatchConfig` frozen dataclass：
   `poll_seconds: float = 5.0`（驗證 > 0，≤ 3600）、`max_retries: int = 3`（驗證 0 ≤ x ≤ 100）；
   `AppConfig.watch: WatchConfig = WatchConfig()`；example.toml 中文註解。
2. 新 `src/pdf_ocrer/watcher.py`：
   ```python
   @dataclass(frozen=True)
   class WatchCycle:
       index: int                 # 第幾輪（1 起）
       ready: list[ScanItem]      # 本輪就緒待處理

   class FolderWatcher:
       def __init__(self, folder: Path, cfg: AppConfig): ...
       def poll(self) -> list[ScanItem]: ...
       def observe(self, results: list[FileResult]) -> None: ...
   ```
   - `poll()`：呼叫 `scanning.scan_inputs`（沿用 input 設定，遞迴/圖片自動生效）→ 對每個 item `os.stat`
     取 `(size, mtime_ns)` 快照 → 與上一輪快照比較：
     - 新出現或快照變動 → 記錄新快照、**本輪不回傳**（等下一輪穩定）
     - 快照與上一輪相同且未在「已處理集合」且未被退避凍結 → 回傳（就緒）
     - stat 失敗（消失/鎖定）→ 移除快照記錄、跳過
   - 已處理集合：`dict[rel, (size, mtime_ns)]`——`observe()` 把 status 非 FAILED 的結果（含 SKIPPED_DONE、
     加密-跳過）以其當時快照記入；來源之後變動（快照不同）→ 視為新工作
   - 退避表：`dict[rel, (size, mtime_ns, attempts)]`——`observe()` 對 FAILED 結果 attempts+1；
     attempts ≥ max_retries → 凍結（poll 不再回傳）直到快照改變（重置計數）；未凍結者下一輪照常回傳（重試）
   - 全部純記憶體狀態；不做持久化（重啟後靠 manifest 去重）
3. `watch_loop`（同檔）：
   ```python
   def watch_loop(folder, cfg, engine, client, prompt_template, *,
                  progress_cb=None, log_cb=None, file_cb=None, cycle_cb=None,
                  stop_event: threading.Event, run_batch_fn=run_batch) -> WatchSummary
   ```
   - 迴圈：`poll()` → 有就緒檔 → `run_batch_fn(..., files=ready, cancel_event=stop_event, ...)` →
     `observe(summary.results)`、累計處理數 → `cycle_cb(cycle_index, len(ready), cumulative)`；
     無就緒檔 → `cycle_cb(cycle_index, 0, cumulative)`（GUI 顯示心跳）
   - 每輪結束 `stop_event.wait(cfg.watch.poll_seconds)`；`stop_event.is_set()` → 跳出
   - 批次被取消（summary.cancelled）→ 直接跳出（stop 已按）
   - 回傳 `WatchSummary(cycles: int, total_processed: int, results: list[FileResult] 累計)`
   - logger + log_cb：每輪有工作時記一行（第 N 輪、就緒 M 檔）
4. 測試 `tests/test_watcher.py`（新）：
   - poll 穩定性：檔案第一輪出現不回傳、第二輪快照相同回傳；輪間變動（寫入中）持續不回傳
   - observe 靜音：處理過的（含 SKIPPED_DONE）不再回傳；來源變動後再回傳
   - 退避：FAILED 重試 max_retries 次後凍結；快照改變解凍重置
   - watch_loop：`run_batch_fn` 注入假函式、`poll_seconds=0.01`、跑 2~3 輪後 set stop_event →
     斷言每輪呼叫參數（files 清單）、cycle_cb 序列、累計數、WatchSummary

### Allowed Files
`src/pdf_ocrer/watcher.py`（新）、`src/pdf_ocrer/config.py`、`config.example.toml`、`tests/test_watcher.py`（新）、`tests/test_config.py`

---

## WO-5.3：CLI/GUI 佈線 + 文件 + v0.5.0

### 規格
1. `src/pdf_ocrer/cli.py`：`--watch` flag：
   - `cfg.output.incremental` 為 false → 印錯誤（監看模式需要增量處理（[output] incremental = true））exit 2
   - 與 `--force` 併用 → 同樣拒絕（exit 2）
   - 進入 watch_loop：`stop_event = threading.Event()`；捕捉 `KeyboardInterrupt` → set stop_event →
     等 watch_loop 返回 → 印累計 summary（輪數、處理數）→ exit 0
   - log_cb/print 流沿用批次模式
2. `src/pdf_ocrer/gui.py`：
   - 「監看模式」CTkCheckBox（開始按鈕區）；勾選時按「開始」→ worker thread 跑 `watch_loop`（沿用既有
     queue 事件模式；cycle_cb → queue → 狀態列「監看中（第 N 輪，累計處理 M 檔）」；idle 輪也更新）
   - 「取消」在監看時文字改「停止監看」（或沿用取消——實作簡單為準，но 行為 = set 同一 Event）
   - 監看結束（stop）→ 恢復按鈕狀態、log 一行累計 summary；**每輪不彈 messagebox**，停止時也不彈（log 即可）
   - `_on_close` 沿用既有確認流程（同一 Event）
   - 「全部重新處理」checkbox 在監看模式下忽略（force 恆 False）；可 disable 之（實作簡單為準）
3. `src/pdf_ocrer/__init__.py`：`__version__ = "0.5.0"`。
4. README×2：監看模式章節（用途：掃描器落地資料夾自動化；輪詢/穩定判定/退避行為；`--watch` 用法；
   GUI 勾選；「同一資料夾請勿同時執行多個監看」警告；需 incremental=true）。
5. 測試：`tests/test_cli.py`：--watch 與 incremental=false/--force 衝突 exit 2；--watch 正常路徑（monkeypatch
   watch_loop 假函式，斷言參數與 exit 0）。`tests/test_gui.py`：勾監看 → 開始 → worker 走 watch 路徑
   （monkeypatch watch_loop）；cycle 事件更新狀態列；停止恢復按鈕。

### Allowed Files
`src/pdf_ocrer/cli.py`、`src/pdf_ocrer/gui.py`、`src/pdf_ocrer/__init__.py`、`README.md`、`README.zh-TW.md`、`tests/test_cli.py`、`tests/test_gui.py`

---

## WO-5.4：Review 修正（Sonnet-5 review 仲裁後的窄修，最終輪）

### Objective
修正六個 Accepted findings，不做其他任何事。

### 規格
1. **P1（watch 迴圈例外隔離）**：`watcher.py` `watch_loop` 的每輪主體（poll + run_batch + observe）包
   try/except Exception → `logger.exception` + log_cb 一行（`監看第 N 輪發生錯誤，{poll_seconds} 秒後重試: {exc}`）
   → continue 下一輪（stop_event.wait 照常）。**KeyboardInterrupt/SystemExit 不攔**。
   測試：scan_inputs 拋一次 OSError（如 WinError 59）→ 迴圈存活、下一輪正常處理、錯誤有記錄。
2. **P1（檔案消失 crash 整批）**：`pipeline.py` 與 `parallel.py` 的 `FileIdentity.from_stat(src)` 呼叫
   改為 try/except OSError → 該檔直接產 `FAILED` FileResult（note=`{type}: {exc}`、進 CSV、不記 manifest）
   → continue 下一檔。測試：files= 清單含不存在路徑 → 該檔 FAILED、其他檔正常、批次不中斷（循序與平行各一測）。
3. **P2（監看強制單程序）**：`watcher.py` `watch_loop` 開頭：`resolve_worker_count(cfg.performance, os.cpu_count()) > 1`
   時 `cfg = dataclasses.replace(cfg, performance=replace(cfg.performance, workers=1))` + log_cb/logger 一行
   （`監看模式使用單一處理程序（每輪重建平行 worker 開銷過大），已忽略 workers 設定`）。README×2 監看章節註明。
   測試：workers=2 config 進 watch_loop → run_batch_fn 收到的 cfg.performance.workers == 1。
4. **P2（--watch exit code）**：`cli.py` watch 分支：`watch_summary.results` 含 FAILED → return 1，否則 0。
   測試：假 watch_loop 回傳含 FAILED 的 summary → exit 1。
5. **P3（GUI 檢查順序）**：`gui.py` `_run_worker` 的 watch incremental 檢查移到建 engine/client **之前**。
6. **P3（force checkbox 殘留）**：`gui.py` `_sync_mode_controls` 勾選監看時同步 `self.force_var.set(False)`。

### Allowed Files
`src/pdf_ocrer/watcher.py`、`src/pdf_ocrer/pipeline.py`、`src/pdf_ocrer/parallel.py`、`src/pdf_ocrer/cli.py`、`src/pdf_ocrer/gui.py`、`README.md`、`README.zh-TW.md`、`tests/test_watcher.py`、`tests/test_pipeline.py`、`tests/test_parallel.py`、`tests/test_cli.py`、`tests/test_gui.py`

### Acceptance Criteria
- [ ] 六項各有測試/文件；全 suite 綠 + ruff 過
