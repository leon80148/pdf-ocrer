# Codex 任務簡報：Stage 1b 引擎產品化（WO-1.1 ~ WO-1.4，依序執行）

背景：實證閘門已定案（見 `docs/specs/benchmark-results.md` 決策 D1、`docs/specs/rapidocr-api-facts.md`）。
本簡報含四個依序工單；每次派工訊息會指名執行哪一個工單，**只做該工單**。

共通約束（每個工單都適用）：

- 禁止 `git commit` / `git add`；只改工作區檔案。
- 只改該工單 Allowed Files；不重構無關程式；發現額外問題只記錄在回報中。
- TDD：先寫失敗測試再實作。
- 驗證：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q -p no:cacheprovider`（無 display 時 GUI 測試 skip 屬預期）+ `C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m ruff check src tests scripts`。
- 單元測試不得 import 真 paddle / rapidocr / 網路 / GUI；沿用既有 fake-module 模式（參考 `tests/test_ocr_engine.py`）。
- 回報格式：變更檔案清單、pytest 最後 3 行 + ruff 結果、每個驗收項的落實位置（檔:函式）、偏離處。

---

## WO-1.1：config 引擎欄位

### Objective
`OcrConfig` 新增三欄位 + 驗證 + 範例檔。

### 規格
- `src/pdf_ocrer/config.py`：`OcrConfig` 新增（放在既有欄位後）：
  - `engine: str = "paddle"`（驗證：casefold 後 ∈ {"paddle", "rapidocr"}，錯誤訊息列出合法值）
  - `cpu_threads: int = 0`（驗證：int，0 ≤ x ≤ 64；0 = 函式庫預設）
  - `textline_orientation: bool = True`
- `_validate_ocr` 增補相應檢查（沿用既有 ConfigError 訊息風格，中文）。
- `load_config` 的 `[ocr]` section 解析支援新鍵（沿用既有 pattern）。
- `config.example.toml` `[ocr]` 增加三鍵與中文註解：
  - `engine`：`# OCR 引擎："paddle"（預設）或 "rapidocr"（推薦，快 ~38x，需 pip install pdf-ocrer[rapidocr]）`
  - `cpu_threads`：`# 推論執行緒數。0 = 函式庫預設。paddle→cpu_threads；rapidocr→intra_op_num_threads`
  - `textline_orientation`：`# 文字行方向偵測（處理上下顛倒的行）。預設 true`
  - 並更新既有 `enable_mkldnn` 的註解為：`# oneDNN 加速。paddlepaddle 3.2.x 可設 true（medium 快 ~7x）；3.3.x 有 bug 必須 false（詳 docs/specs/paddleocr-api-facts.md §2）`
- `CommonSettings`（settings dialog 橋接）**本工單不動**（引擎下拉到 Stage 4 收尾才做）。

### Allowed Files
`src/pdf_ocrer/config.py`、`config.example.toml`、`tests/test_config.py`

### Acceptance Criteria
- [ ] 未知 engine 值 → ConfigError；cpu_threads 越界 → ConfigError
- [ ] 舊 config.toml（無新鍵）載入後 engine=="paddle"、cpu_threads==0、textline_orientation is True
- [ ] TOML 設 engine="rapidocr" / cpu_threads=4 / textline_orientation=false 正確載入
- [ ] 全 suite 綠 + ruff 過

---

## WO-1.2：create_engine 工廠 + Paddle kwargs 透傳

### Objective
`ocr_engine.py` 新增工廠函式；PaddleOcrEngine 接上新 config 欄位。

### 規格
- `src/pdf_ocrer/ocr_engine.py`：
  ```python
  def create_engine(cfg: OcrConfig, log: Callable[[str], None] | None = None) -> OcrEngineProtocol:
  ```
  - `cfg.engine == "paddle"` → `PaddleOcrEngine(cfg, log)`
  - `cfg.engine == "rapidocr"` → lazy `from pdf_ocrer.rapidocr_engine import RapidOcrEngine`；`ImportError`（rapidocr 套件缺）→ raise `ConfigError("rapidocr 引擎需要安裝額外套件：pip install pdf-ocrer[rapidocr]")`。注意：import `pdf_ocrer.rapidocr_engine` 模組本身**不得**觸發 rapidocr import（該模組 WO-1.3 才建立且是懶載入；本工單可先讓工廠在模組不存在時拋 ConfigError——用 try/except ImportError 包 module import，測試以 monkeypatch sys.modules 注入假模組驗證 dispatch）
  - 其他值 → ConfigError（防禦；config 驗證已擋）
- `PaddleOcrEngine._get_ocr()` kwargs 修改：
  - `"use_textline_orientation": self._cfg.textline_orientation`（取代 hardcode True）
  - `self._cfg.cpu_threads > 0` 時加 `"cpu_threads": self._cfg.cpu_threads`（已實測 PaddleOCR 3.7 接受此 kwarg）

### Allowed Files
`src/pdf_ocrer/ocr_engine.py`、`tests/test_ocr_engine.py`

### Acceptance Criteria
- [ ] create_engine dispatch 三分支測試（paddle 型別、rapidocr 假模組、rapidocr 缺套件 ConfigError 訊息含 pip install 指令）
- [ ] fake paddle 模組測試驗證 kwargs：textline_orientation=False 時 use_textline_orientation 為 False；cpu_threads=4 時 kwargs 含 cpu_threads=4；cpu_threads=0 時 kwargs 不含該鍵
- [ ] 全 suite 綠 + ruff 過

---

## WO-1.3：RapidOcrEngine

### Objective
新模組 `src/pdf_ocrer/rapidocr_engine.py`：轉換器 + 懶初始化引擎。**先讀 `docs/specs/rapidocr-api-facts.md`（單一事實來源，照 §5 映射表做）。**

### 規格
- `lines_from_rapidocr(output: Any, min_confidence: float) -> list[OcrLine]`：
  - `output.boxes` / `output.txts` / `output.scores` 任一為 None → `[]`（空結果契約，api-facts §3）
  - boxes 為 (N,4,2) ndarray TL/TR/BR/BL；逐條過濾 `score < min_confidence` 或 `not text.strip()`；組 `OcrLine(text, poly=tuple((float(x), float(y))...), score=float(score))`（結構仿 `ocr_engine.lines_from_prediction`）
- `class RapidOcrEngine`：建構子 `(cfg: OcrConfig, log=None)`，懶初始化（首次 recognize 才 `from rapidocr import RapidOCR`；log 訊息「正在載入 OCR 模型…」與 paddle 引擎一致）：
  - params dict（dotted-key，api-facts §5）：`cfg.cpu_threads > 0` → `"EngineConfig.onnxruntime.intra_op_num_threads"`；`cfg.det_limit_side_len is not None` → `"Det.limit_side_len"`；`"Global.use_cls": cfg.textline_orientation`
  - params 全空 → `RapidOCR()`；否則 `RapidOCR(params=params)`
  - `recognize(img_rgb)` → `self._ocr(img_rgb)` → `lines_from_rapidocr(result, cfg.min_confidence)`
- 模組頂部 docstring 註明 api-facts 出處與日期。
- `pyproject.toml`：`[project.optional-dependencies]` 加 `rapidocr = ["rapidocr>=3.9", "onnxruntime>=1.19"]`。
- 新測試檔 `tests/test_rapidocr_engine.py`（仿 `test_ocr_engine.py` 模式）：
  - 轉換器純測試：正常、None 空結果、過濾低信心、過濾空白文字、poly 轉 tuple float
  - 引擎測試：`monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=FakeRapidOCR))`，驗證懶初始化只建一次、params 組裝（cpu_threads=2 → intra_op 鍵；=0 → 無 params 走無參建構）、recognize 過濾
  - subprocess 測試：`import pdf_ocrer.rapidocr_engine` 在無 rapidocr 套件下成功（懶載入不觸發）——仿既有 `test_ocr_engine.py` 的 paddle-free import 測試寫法

### Allowed Files
`src/pdf_ocrer/rapidocr_engine.py`（新）、`tests/test_rapidocr_engine.py`（新）、`pyproject.toml`

### Acceptance Criteria
- [ ] 上述測試全綠；模組 import 不觸發 rapidocr
- [ ] 全 suite 綠 + ruff 過

---

## WO-1.4：CLI/GUI 佈線 + 文件

### Objective
入口改走 create_engine；CLI 加 `--engine`；README 更新；paddle-cpu extra 改釘。

### 規格
- `src/pdf_ocrer/cli.py`：`--engine {paddle,rapidocr}`（預設 None=用 config 值；提供時覆寫 `cfg.ocr.engine`，用 `dataclasses.replace`）；建引擎處改 `create_engine(cfg.ocr, log)`（保留既有 engine_factory 測試注入 seam）。
- `src/pdf_ocrer/gui.py`：建引擎處改 `create_engine(cfg.ocr, ...)`（同樣保留注入 seam）；其他不動。
- `pyproject.toml`：`paddle-cpu` extra 由 `paddlepaddle==3.3.*` 改 `paddlepaddle==3.2.*`（3.3.x oneDNN bug，見 api-facts §2）。
- `README.md` + `README.zh-TW.md`：
  - 效能章節改寫：引用 benchmark-results.md 重點表（現行 34.8s/頁 → rapidocr 0.90s/頁 ~38x；paddle 3.2.2+mkldnn medium 5.1s/頁）
  - 安裝章節加 `pip install pdf-ocrer[rapidocr]` 與 config `engine = "rapidocr"` 說明（標註推薦）
  - paddle 已知限制段落更新（3.3.x bug、3.2.x 可開 mkldnn）
- ConfigError 匯入注意：cli 對 ConfigError 的既有處理（印錯誤 exit 1）必須涵蓋 create_engine 拋的 ConfigError（在 try 範圍內建引擎，或於 main 內 catch——依既有結構最小改動）。

### Allowed Files
`src/pdf_ocrer/cli.py`、`src/pdf_ocrer/gui.py`、`pyproject.toml`、`README.md`、`README.zh-TW.md`、`tests/test_cli.py`、`tests/test_gui.py`

### Acceptance Criteria
- [ ] `--engine rapidocr` 覆寫 config（測試：assert 傳給 engine factory 的 cfg.engine）
- [ ] 缺 rapidocr 套件時 CLI 顯示含 `pip install pdf-ocrer[rapidocr]` 的錯誤並 exit 1（測試 monkeypatch create_engine 拋 ConfigError）
- [ ] 全 suite 綠 + ruff 過；README 兩語言均更新

---

## WO-1.5：Review 修正（Sonnet-5 review 仲裁後的窄修）

### Objective
修正三個 Accepted findings，不做其他任何事。

### 規格
1. **P1（fail-open）**：`src/pdf_ocrer/rapidocr_engine.py` 的 `_rapidocr_available()` 改為同時檢查 `rapidocr` 與 `onnxruntime` 兩個套件（兩者都 `find_spec` 非 None 才 True；沿用既有 sys.modules 快路徑邏輯，兩個套件都要）。理由：`pyproject.toml` 的 rapidocr extra 宣告兩者為一組；部分安裝（有 rapidocr 無 onnxruntime）時現況會在首次 recognize 拋原始 ImportError，且 run_batch 會把整批每檔標 FAILED（fail-open），應在建構時就 fail-fast 拋既有 ConfigError。
   - 測試：`tests/test_rapidocr_engine.py` 加一測試——monkeypatch `importlib.util.find_spec` 使 `onnxruntime` 回 None（`rapidocr` 正常）→ 建構 `RapidOcrEngine` 拋 ConfigError（match `pip install pdf-ocrer\[rapidocr\]`）。注意 sys.modules 快路徑：測試需確保檢查邏輯對「rapidocr 在 sys.modules 但 onnxruntime 缺」也 fail（用 monkeypatch.delitem(sys.modules, "onnxruntime", raising=False) 搭配 find_spec patch）。
2. **P3（死碼）**：`src/pdf_ocrer/ocr_engine.py` `create_engine` 的 rapidocr 分支移除 `try/except ImportError`（`pdf_ocrer.rapidocr_engine` 模組本身不依賴 rapidocr 套件，該 except 永不觸發且誤導維護者）；直接 import + 建構，唯一守門是 `RapidOcrEngine.__init__` 的 ConfigError。既有測試不需改（monkeypatch `_rapidocr_available` 的測試不受影響）。
3. **P2（bench 工具）**：`scripts/bench_ocr.py`：
   - `build_engine` 改用 `pdf_ocrer.ocr_engine.create_engine`；`--engine rapidocr` 可用（cfg 帶 `engine=args.engine`）
   - `--cpu-threads`/`--textline` 實際轉入 cfg（`cpu_threads=args.cpu_threads`、`textline_orientation=args.textline == "on"`）
   - 刪除 `_ignored_option_warnings` 與過時 TODO 註解（兩個 knob 現已生效）
   - paddle 專用參數（mkldnn/det_model/rec_model）在 engine=rapidocr 時無效——在 summary 印一行提示即可，不擋
   - 煙霧驗證（沙箱若無權讀模型快取則跳過並註明，宿主會補跑）：`--synthetic --pages 2 --engine rapidocr --label wo15-smoke --out %TEMP%\wo15.csv`

### Allowed Files
`src/pdf_ocrer/rapidocr_engine.py`、`src/pdf_ocrer/ocr_engine.py`、`scripts/bench_ocr.py`、`tests/test_rapidocr_engine.py`、`tests/test_ocr_engine.py`

### Acceptance Criteria
- [ ] onnxruntime 缺失時建構即拋 ConfigError（新測試綠）
- [ ] create_engine rapidocr 分支無 try/except ImportError
- [ ] bench 可跑 rapidocr、cpu-threads/textline 真的生效
- [ ] 全 suite 綠 + ruff 過
