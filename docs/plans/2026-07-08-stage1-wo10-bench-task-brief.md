# Codex 任務簡報 WO-1.0：OCR benchmark 工具

## 任務

在本 repo（cwd：`C:\Users\User\projects\pdf_ocrer`）新增 dev-only 效能量測腳本 `scripts/bench_ocr.py`。此腳本是後續「引擎加速實證閘門」的量測儀器，不進打包、不改 `src/`。

### 功能規格

CLI（argparse）：

```
python scripts/bench_ocr.py [--pdf PATH | --synthetic] [--pages 4] [--engine paddle]
    [--dpi 200] [--mkldnn off|on] [--textline on|off] [--cpu-threads 0]
    [--det-limit 0] [--det-model NAME] [--rec-model NAME]
    [--repeat 1] [--label TEXT] [--out bench_results.csv]
    [--dump-text PATH] [--baseline PATH]
```

- `--synthetic`（預設，若無 `--pdf`）：用 `tests/fixtures_gen.py` 的元件在暫存目錄組一份 **N 頁**（`--pages`，預設 4）掃描式 PDF：每頁 = `fixtures_gen._append_scanned_page(doc)`（200dpi 渲染影像頁，內容為 `GT_LINES` 三行繁中）。加 `sys.path` 進 `tests/`（模仿 `tests/conftest.py` 的做法）。
- `--pdf PATH`：單一 PDF 或資料夾（資料夾則取其中所有 `*.pdf`）。
- `--engine paddle`：目前僅支援 paddle，但以本地工廠函式 `build_engine(args) -> OcrEngineProtocol` 建構（import `pdf_ocrer.ocr_engine.PaddleOcrEngine` 與 `pdf_ocrer.config.OcrConfig`），保留日後加 rapidocr 的擴充點（未知 engine 名 → 明確報錯）。
- 引擎參數：以 `OcrConfig`（`dataclasses.replace` 預設值）帶入 dpi / enable_mkldnn / det_limit_side_len / det_model_name / rec_model_name / lang（固定 `chinese_cht`）；`--cpu-threads > 0` 與 `--textline` 需以 `PaddleOcrEngine` 現行建構參數能接受的方式傳入——**注意**：現行 `PaddleOcrEngine` 若未暴露 cpu_threads/textline 參數，本腳本先不傳（記為 TODO 註解並在 summary 印警告），**不得為此修改 `src/`**。

### 量測項目（每次執行 = 一列 CSV）

1. `init_s`：引擎冷初始化秒數（建構 + 第一次 `recognize` 的總時間，用首頁）
2. `render_ms_med`：`pdf_ocrer.pdf_processor.render_page` 每頁毫秒（median）
3. `ocr_s_med` / `ocr_s_p95`：**排除首頁（warmup）** 後所有（頁 × repeat）的 `recognize` 秒數 median / p95
4. `lines_mean`：每頁辨識行數平均；`conf_mean`：平均信心值
5. `rss_peak_mb`：psutil 每頁量測後取 max（含子行程 RSS 加總：`Process.memory_info().rss` + children）
6. 環境欄：`label, engine, dpi, mkldnn, textline, cpu_threads, det_limit, det_model, rec_model, pages, repeat, paddlepaddle_ver, paddleocr_ver, cpu_model, timestamp`

### 正確性檢核（synthetic 模式必跑，印在 summary 並寫入 CSV 欄）

- `gt_recall`：`GT_LINES` 三行文字是否全部出現在辨識結果串接文字中（比對前將全形冒號 `：` 正規化為半形 `:`，去除空白）→ `3/3` 格式
- `trad_ok`：繁體字形檢查——辨識文字包含「證」且不含「证」、包含「診」且不含「诊」→ bool
- `--dump-text PATH`：將全部辨識文字串接寫入 UTF-8 txt（跨引擎比對用）
- `--baseline PATH`：讀入既有 txt，算 `difflib.SequenceMatcher(None, a, b).ratio()` 寫入 `similarity` 欄

### 輸出

- `--out`（預設 `bench_results.csv`，相對 cwd）：append 模式，檔案不存在先寫 header；欄位順序固定
- stdout：人類可讀 summary（每個量測項一行）
- 同時新增 `docs/specs/benchmark-results.md` 骨架：標題、「決策 D1」空節、一個空的結果 markdown 表（欄位對齊 CSV）

### pyproject

`[project.optional-dependencies] dev` 加入 `"psutil"`（venv 已裝好，不需 pip install）。

## 約束

- 禁止 `git commit` / `git add`；只改工作區檔案。
- **絕不修改 `src/pdf_ocrer/` 任何檔案**；只新增 `scripts/bench_ocr.py`、`docs/specs/benchmark-results.md`，修改 `pyproject.toml`（僅 dev extras 一行）。
- 不要重構無關程式、不擴大 scope、發現額外問題只記錄在回報中。
- 腳本頂部 docstring 註明用法範例。
- ruff 必須過（line-length 100）：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m ruff check scripts`。

## 驗證迴圈

1. 語法/靜態：ruff 如上。
2. 煙霧測試（真 paddle，較慢屬預期）：
   `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe scripts/bench_ocr.py --synthetic --pages 2 --repeat 1 --det-model PP-OCRv6_small_det --rec-model PP-OCRv6_small_rec --label smoke --out %TEMP%\bench_smoke.csv`
   預期：跑完印 summary、CSV 有 header + 1 列、`gt_recall=3/3`、`trad_ok=True`。模型已快取在 `C:\Users\User\.paddlex\official_models`（PP-OCRv6_small_det/rec 存在），首次 recognize 約 10~30 秒/頁屬正常。
3. 既有測試套件不得受影響：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider`（基線 114 passed, 1 skipped；結束後清理自產暫存，不留 `.pytest_cache`）。

## 回報格式（精簡）

1. 變更檔案清單
2. 煙霧測試 stdout summary + CSV 該列內容
3. pytest 最後 3 行 + ruff 結果
4. 偏離簡報之處與原因（理想上無）
