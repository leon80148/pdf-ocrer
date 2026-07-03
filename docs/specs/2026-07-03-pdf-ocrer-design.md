# pdf_ocrer 設計規格（SDD Spec）

日期：2026-07-03｜狀態：已核准｜版本：v1.0

## 1. 背景與目標

診所行政人員每天收到大量掃描 PDF（轉診單、檢驗報告、診斷證明、保險文件…），檔案無文字層無法搜尋、檔名是掃描器流水號。本工具：

1. 選擇資料夾 → 批次將掃描 PDF 轉為**可搜尋雙層 PDF**（原影像 + 隱形文字層）
2. 以 **LLM 依內容自動命名**輸出檔（prompt 使用者可編輯）
3. **原始檔案絕不修改**，輸出到子資料夾，附 CSV 對照表（可追溯）

**開源定位**：通用的「資料夾批次 OCR + AI 命名」工具，不綁定任何 LLM 供應商，預設在地化為繁體中文醫療行政情境，但 OCR 語言與命名 prompt 全部可設定。

### Non-goals（v1 不做）

- 不做版面結構還原（表格/Markdown 輸出）— 只做可搜尋文字層
- 不做歪斜行的旋轉貼合（v1 軸對齊插入，仍可搜尋）
- 不做浮水印移除、影像增強、GPU 支援（config 留 `device` 欄位）
- 不做遞迴子資料夾掃描
- 不處理需要密碼才能開啟的 PDF（記錄後跳過）

## 2. 架構總覽

```
┌─ gui.py (Tkinter) ─────┐     ┌─ cli.py (argparse) ─┐
│  選資料夾/進度/取消      │     │  headless 批次       │
└───────────┬────────────┘     └──────────┬──────────┘
            └──────────┬───────────────────┘
                 pipeline.py  批次協調（掃描、錯誤隔離、CSV、進度回呼）
        ┌──────────────┼───────────────────┐
  ocr_engine.py   pdf_processor.py    llm_namer.py ── llm_providers.py
  PaddleOCR封裝    渲染/座標換算/        prompt渲染/消毒/    LLMClient Protocol
  (PP-OCRv6)      隱形文字層(PyMuPDF)   碰撞/fallback      + provider registry
        └──────────────┴───────────────────┘
                    config.py  (dataclasses + tomllib)
```

**單檔資料流**：開 PDF → 逐頁判斷（已有文字層？）→ 無則 `render_page(dpi)` → OCR → 座標換算 → `insert_text(render_mode=3)` → 收集全文 → LLM 命名 → 消毒/碰撞處理 → 存到輸出資料夾 → append CSV。

## 3. 專案佈局

```
pdf_ocrer/
├── pyproject.toml          # 套件定義、依賴、pytest/ruff 設定、entry points
├── LICENSE                 # MIT
├── README.md               # 英文
├── README.zh-TW.md         # 繁中（主要使用者文件）
├── CONTRIBUTING.md
├── config.example.toml     # 完整註解的設定範本（config.toml 被 gitignore）
├── naming_prompt.txt       # 預設命名 prompt（使用者可編輯）
├── src/pdf_ocrer/
│   ├── __init__.py         # __version__ = "0.1.0"
│   ├── __main__.py         # python -m pdf_ocrer
│   ├── cli.py              # 入口：無參數→GUI；有資料夾→批次
│   ├── config.py
│   ├── ocr_engine.py
│   ├── pdf_processor.py
│   ├── llm_providers.py
│   ├── llm_namer.py
│   ├── pipeline.py
│   └── gui.py
├── tests/
│   ├── conftest.py         # session fixtures（呼叫 fixtures_gen）
│   ├── fixtures_gen.py     # 產生測試 PDF（無字層假掃描等）
│   ├── test_config.py
│   ├── test_llm_providers.py
│   ├── test_llm_namer.py
│   ├── test_ocr_engine.py
│   ├── test_pdf_processor.py
│   ├── test_pipeline.py
│   ├── test_cli.py
│   └── test_integration.py # @pytest.mark.integration（真 PaddleOCR，預設不跑）
└── docs/
    ├── specs/…（本文件）
    └── plans/…
```

## 4. 模組規格

### 4.1 config.py

Frozen dataclasses + `tomllib`（Python 3.11+ stdlib）。**config 檔不存在 → 全預設值；未知鍵 → 忽略並以 warnings.warn 提示；值域錯誤 → `ConfigError`（含欄位名）**。

```python
@dataclass(frozen=True)
class OcrConfig:
    dpi: int = 200                    # 72–600
    lang: str = "chinese_cht"
    min_confidence: float = 0.5       # 0–1
    skip_pages_with_text: bool = True
    min_existing_chars: int = 30
    device: str = "cpu"
    enable_mkldnn: bool = False       # paddle 3.3.0 oneDNN bug，見 api-facts §2
    det_limit_side_len: int | None = None   # None=函式庫預設；小字漏偵時調大
    det_model_name: str | None = None       # 如 "PP-OCRv6_small_det"（速度調校）
    rec_model_name: str | None = None       # 如 "PP-OCRv6_small_rec"

@dataclass(frozen=True)
class OutputConfig:
    subdir_name: str = "OCR輸出"
    csv_prefix: str = "對照表"

@dataclass(frozen=True)
class NamingConfig:
    enabled: bool = True
    rename_files_with_text: bool = True   # 已有文字層的檔也用其文字命名
    prompt_file: str = "naming_prompt.txt"
    max_chars_to_llm: int = 3000
    max_pages_to_llm: int = 2
    max_filename_length: int = 80         # 10–200
    fallback_suffix: str = "_OCR"

@dataclass(frozen=True)
class LlmConfig:
    provider: str = "openai_compatible"   # 或 "none"
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    api_key: str = ""                     # 空→讀環境變數 PDF_OCRER_API_KEY
    timeout_seconds: float = 60.0
    temperature: float = 0.1
    max_tokens: int = 1024                # 需容納 reasoning 模型的思考段

@dataclass(frozen=True)
class DebugConfig:
    visible_text: bool = False            # true→文字層可見紅字（對位除錯）

@dataclass(frozen=True)
class AppConfig:
    ocr: OcrConfig; output: OutputConfig; naming: NamingConfig
    llm: LlmConfig; debug: DebugConfig

class ConfigError(ValueError): ...
def load_config(path: Path | None = None) -> AppConfig
def resolve_api_key(cfg: LlmConfig) -> str   # cfg.api_key → env PDF_OCRER_API_KEY → ""
```

### 4.2 ocr_engine.py

```python
@dataclass(frozen=True)
class OcrLine:
    text: str
    poly: tuple[tuple[float, float], ...]  # 4 點 (x,y)，渲染影像像素座標，順序 TL,TR,BR,BL
    score: float

class OcrEngineProtocol(Protocol):
    def recognize(self, img_rgb: "np.ndarray") -> list[OcrLine]: ...

class PaddleOcrEngine:
    def __init__(self, cfg: OcrConfig, log: Callable[[str], None] | None = None)
    def recognize(self, img_rgb) -> list[OcrLine]   # 過濾 score < min_confidence 與空字串
```

規則：
- **paddleocr 只能在方法內 lazy import**（單元測試環境不裝 paddle 也要能 import 本模組）
- 首次 `recognize` 才建 `PaddleOCR(...)` 實例（模型載入慢，先呼叫 `log("正在載入 OCR 模型…")`）
- 初始化參數（座標正確性關鍵）：`lang=cfg.lang, ocr_version="PP-OCRv6", use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=True, device=cfg.device, enable_mkldnn=cfg.enable_mkldnn`
  - 前兩者**必須關**：否則座標落在「轉正後影像」空間，與我們渲染的頁面影像對不上
  - `enable_mkldnn` 預設 False（paddle 3.3.0 oneDNN bug，api-facts §2）
  - `cfg.det_model_name/rec_model_name` 非 None 時傳 `text_detection_model_name/text_recognition_model_name`
  - `cfg.det_limit_side_len` 非 None 時於 `predict(text_det_limit_side_len=...)` 傳入
- predict 回傳結構依 `docs/specs/paddleocr-api-facts.md`（環境冒煙實測產出）為準
- 單一實例、單一 worker thread 使用（不假設 thread-safe）

### 4.3 pdf_processor.py（技術核心）

```python
class EncryptedPdfError(Exception): ...

def has_text_layer(page, min_chars: int) -> bool      # len(page.get_text().strip()) >= min_chars
def render_page(page, dpi: int) -> np.ndarray          # RGB HxWx3 uint8；get_pixmap(dpi=dpi, alpha=False)
def add_text_layer(page, lines: list[OcrLine], dpi: int, visible: bool = False) -> int

@dataclass(frozen=True)
class PageReport:
    page_index: int
    action: str          # "ocr" | "kept_existing" | "empty"
    line_count: int

@dataclass
class PdfResult:
    doc: "pymupdf.Document"   # in-memory，呼叫端決定檔名後 save
    text: str                 # 全文（供命名；頁間以 \n\n 分隔，依頁序）
    reports: list[PageReport]
    total_pages: int
    ocr_pages: int

def process_pdf(src: Path, cfg: AppConfig, engine: OcrEngineProtocol,
                page_cb: Callable[[int, int], None] | None = None,
                cancel: "threading.Event | None" = None) -> PdfResult
```

**座標換算規格**（`add_text_layer` 內）：

```
scale = 72.0 / dpi
顯示座標 pt = (x_px * scale, y_px * scale)          # get_pixmap 已套用頁面 /Rotate
插入座標   = pymupdf.Point(x_pt, y_pt) * page.derotation_matrix   # rotation=0 時為恆等
```

**隱形文字插入規格**（逐 OcrLine）：

```
_FONT = pymupdf.Font("cjk")           # 模組層快取一份；Droid Sans Fallback（實測 2026-07-03：
                                      # ascender=1.04296875, descender=-0.265625，堃峯犇有字形）
# 每頁先註冊字型一次（insert_text 不接受 "cjk" 直接當 fontname——實測會拋
# "need font file or buffer"；PyMuPDF 對重複插入相同字型會自動重用 xref）：
page.insert_font(fontname="pdfocr-cjk", fontbuffer=_FONT.buffer)

h  = poly 高度(pt)（TL 到 BL 距離）；w = poly 寬度(pt)（TL 到 TR 距離）
fontsize = h / (_FONT.ascender - _FONT.descender)
baseline = Point(TL.x_pt, TL.y_pt + _FONT.ascender * fontsize)  # 先算顯示空間，再乘 derotation_matrix
natural_w = _FONT.text_length(text, fontsize);  sx = w / natural_w （natural_w<=0 則略過該行）
page.insert_text(baseline_derot, text, fontsize=fontsize, fontname="pdfocr-cjk",
                 render_mode=(0 if visible else 3),
                 color=(1,0,0) if visible else None,
                 morph=(baseline_derot, pymupdf.Matrix(sx, 1.0)))
```

**process_pdf 規則**：
- `doc.needs_pass` → 先試 `doc.authenticate("")`，仍鎖 → raise `EncryptedPdfError`
- 逐頁：`skip_pages_with_text and has_text_layer(page, min_existing_chars)` → action=`kept_existing`，全文取 `page.get_text()`；否則渲染+OCR+疊字，action=`ocr`（0 行則 `empty`）
- 每頁處理完釋放 pixmap 參照；`cancel.is_set()` 於頁間檢查，取消則 raise `BatchCancelled`（定義於 pipeline）
- 存檔由呼叫端執行：`doc.subset_fonts()` 後 `doc.save(dst, garbage=3, deflate=True)`（subset_fonts 由 pipeline 在 save 前呼叫）
- 來源檔以唯讀方式開啟，永不回寫

### 4.4 llm_providers.py（通用 LLM 連接層 — 開源重點）

**設計原則：不綁供應商。** 一個極小的同步介面 + 可註冊工廠，預設實作走 OpenAI 相容協定（涵蓋 OpenAI / Ollama / LM Studio / vLLM / Groq / OpenRouter / Gemini 相容端點 / Anthropic 相容端點…）。社群要接原生 SDK 只需註冊新 provider，不動其他程式碼。

```python
class LLMError(Exception): ...

class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...    # 回傳原始文字；失敗 raise LLMError

_PROVIDERS: dict[str, Callable[[LlmConfig], LLMClient]] = {}

def register_provider(name: str):                  # decorator，名稱 casefold 唯一
    ...

def create_client(cfg: LlmConfig) -> LLMClient | None:
    # provider=="none" → None（停用 LLM，走 fallback 命名）
    # 未知 provider → ConfigError，訊息列出可用 provider 清單

@register_provider("openai_compatible")
class OpenAICompatClient:
    # openai SDK；api_key = resolve_api_key(cfg) or "not-needed"（本機服務不驗但 SDK 需非空）
    # chat.completions.create(model, messages=[{"role":"user","content":prompt}],
    #                         temperature, max_tokens, timeout)
    # 空回應 / APIError / timeout → raise LLMError(原因)
```

openai SDK 為必要依賴（輕量）；未來原生 provider（如 anthropic）以 optional extras 提供。

### 4.5 llm_namer.py

```python
def build_prompt(template: str, text: str, original_name: str, today: str) -> str
    # string.Template.safe_substitute($text/$original_name/$today)——使用者模板含 {} 或 $ 不會炸

def sanitize_filename(raw: str, max_length: int) -> str | None   # 清洗失敗回 None
def resolve_collision(out_dir: Path, stem: str, used: set[str]) -> str
    # used 存 casefold；已存在（磁碟或本批次）→ stem_2, stem_3…；回傳最終 stem 並登記
def suggest_filename(text: str, original_stem: str, cfg: AppConfig,
                     client: LLMClient | None, prompt_template: str,
                     log: Callable[[str], None] | None = None) -> tuple[str, str]
    # 回傳 (stem, source)，source ∈ {"llm","fallback"}
    # client=None / text 全空白 / LLMError（重試 1 次後）/ 消毒後為 None → fallback: original_stem + fallback_suffix
```

**sanitize_filename 清洗順序**（Windows 規則，全部要有測試）：
1. 移除 `<think>…</think>` 區塊（reasoning 模型如 qwen3 的思考輸出；regex DOTALL）
2. 取第一個非空行；去除前後引號/反引號/`*`/markdown 標記
3. 去除結尾 `.pdf`（不分大小寫）
4. 刪除 `\ / : * ? " < > |` 與 ASCII 控制字元；壓縮連續空白為一個
5. 去除結尾的 `.` 與空白（Windows 限制）
6. 檢查 Windows 保留裝置名（CON PRN AUX NUL COM1-9 LPT1-9，不分大小寫、比對副檔名前的主幹）→ 前綴 `_`
7. 超過 max_length → 截斷（截斷後重跑步驟 5）
8. 結果為空字串 → 回 None

### 4.6 pipeline.py

```python
class BatchCancelled(Exception): ...

class FileStatus(str, Enum):
    SUCCESS_OCR = "OCR完成"
    SUCCESS_EXISTING_TEXT = "已有文字層-僅命名"
    NO_TEXT_FOUND = "無文字-原樣輸出"
    SKIPPED_ENCRYPTED = "加密-跳過"
    FAILED = "失敗"

@dataclass
class FileResult:
    source: Path; output: Path | None; status: FileStatus
    total_pages: int; ocr_pages: int; naming_source: str; note: str

@dataclass
class BatchSummary:
    results: list[FileResult]; csv_path: Path | None
    output_dir: Path; cancelled: bool

ProgressCb = Callable[[int, int, int, int, str], None]  # (file_i, file_n, page_i, page_n, filename)

def run_batch(folder: Path, cfg: AppConfig,
              engine: OcrEngineProtocol, client: LLMClient | None,
              prompt_template: str,
              progress_cb: ProgressCb | None = None,
              log_cb: Callable[[str], None] | None = None,
              cancel_event: "threading.Event | None" = None) -> BatchSummary
```

規則：
- 掃描 `folder.glob("*.pdf")`（不遞迴、不分大小寫副檔名），排序，排除輸出子資料夾內的檔案
- 無 PDF → 回空 summary（csv_path=None），log 提示
- 輸出資料夾 `folder / cfg.output.subdir_name`，`mkdir(exist_ok=True)`
- CSV `對照表_YYYYMMDD_HHMMSS.csv`，**utf-8-sig**，欄位：`原檔名,新檔名,狀態,總頁數,OCR頁數,命名來源,備註`；**每檔處理完立即 append + flush**（中途當機仍有紀錄）
- 逐檔 try/except：`EncryptedPdfError`→SKIPPED_ENCRYPTED；`BatchCancelled`→中止（cancelled=True，已完成的保留）；其他 Exception→FAILED（note 記 `type: message`），繼續下一檔
- 整份已有文字層（所有頁 kept_existing）→ 不重存 doc，`shutil.copy2` 原檔到輸出夾（新名）→ SUCCESS_EXISTING_TEXT；`rename_files_with_text=False` 時直接沿用原檔名（仍 copy）
- 命名：`naming.enabled` 且有文字 → `suggest_filename`（文字先截 `max_pages_to_llm` 頁、`max_chars_to_llm` 字）；否則 fallback
- 產出檔名 = `resolve_collision` 後的 stem + `.pdf`
- 原檔 sha256 不變（測試驗證用；pipeline 本身不寫原檔）

### 4.7 cli.py / __main__.py

```
pdf-ocrer                     # 無參數 → 啟動 GUI
pdf-ocrer <folder>            # 批次處理該資料夾
  --config PATH               # 預設：CWD 的 config.toml（無則全預設值）
  --no-llm                    # 強制停用 LLM 命名（覆寫 config）
  --dpi N                     # 覆寫 config
  --version
```
- 進度印 stdout（`[3/12] file.pdf 第 5/20 頁`），結尾印 summary 表 + CSV 路徑
- exit code：0 全成功；1 有 FAILED；2 資料夾不存在/無 PDF
- `main(argv: list[str] | None = None) -> int`（可測試）

### 4.8 gui.py

- `run_gui(config_path: Path | None = None) -> None`
- Tkinter + ttk：資料夾選擇、開始/取消、`ttk.Progressbar`（determinate 按檔數）、狀態列（`第 3/12 檔 - 第 5/20 頁`）、`ScrolledText` 日誌、完成摘要 messagebox +「開啟輸出資料夾」（`os.startfile`）、「編輯命名規則」「編輯設定」按鈕（`os.startfile` 開文字檔）
- **執行緒模型**：pipeline 跑 `threading.Thread(daemon=True)`；worker 不碰 widget — 事件丟 `queue.Queue`，主執行緒 `root.after(100, poll)` 消化；取消用 `threading.Event`
- 視窗標題含版本；關窗時若在跑先確認

## 5. naming_prompt.txt 預設內容

```
你是診所行政檔案命名助手。根據下方 OCR 文字，輸出一個檔名（不含副檔名）。
格式：日期_文件類型_對象
- 日期：文件內的日期，格式 YYYYMMDD；找不到就用 $today
- 文件類型：如 診斷證明書、轉診單、檢驗報告、保險申請書、公文、收據
- 對象：病患姓名或發文機關；無法確定就省略此段
規則：只輸出檔名本身，不要任何說明、引號或副檔名；不得包含 \ / : * ? " < > | 字元；40 字以內。
原檔名：$original_name
--- OCR 文字開始 ---
$text
--- OCR 文字結束 ---
```

## 6. 邊界情況（驗收必測）

| 情況 | 行為 |
|---|---|
| 加密 PDF | 試空密碼 → 仍鎖：SKIPPED_ENCRYPTED 記 CSV，批次續跑 |
| 頁面 /Rotate 90/180/270 | derotation_matrix 換算，搜尋位置正確 |
| 混合頁（部分頁有字） | 逐頁判斷；有字頁沿用文字不重疊 OCR 層 |
| 空白掃描頁/全檔無文字 | 原樣輸出，NO_TEXT_FOUND，fallback 命名 |
| LLM 斷線/timeout/空回應 | 重試 1 次 → fallback 命名，note 記錄，批次不停 |
| LLM 回傳含說明文字/引號/think 區塊/非法字元/超長 | sanitize 全套清洗 |
| 檔名碰撞（含大小寫差異） | casefold 比對，`_2` `_3` 流水號 |
| 損壞 PDF/假副檔名 | FAILED 記 CSV，續跑 |
| 0 個 PDF 的資料夾 | 空 summary，友善提示 |
| 取消 | 頁間響應；已完成檔保留；CSV 已寫入列保留；cancelled=True |

## 7. 相依與相容性

- Python ≥ 3.11（tomllib）；開發鎖 3.12
- 執行依賴：`pymupdf>=1.26`、`openai>=1.60,<3`、`paddleocr>=3.7`、`paddlepaddle==3.3.*`（CPU）
- 開發依賴（extras `dev`）：`pytest>=8`、`ruff`
- pytest 設定：`addopts = "-m 'not integration'"`、markers 註冊 `integration`；單元測試**不需要** paddle/網路/GUI
- ruff：line-length=100，target py311
- 授權 MIT；`pyproject.toml` 含完整 metadata（description、readme、classifiers、urls）

## 8. 驗收標準

1. `pytest`（單元，不含 integration）在乾淨環境全綠、不觸網、不載 paddle
2. `pytest -m integration` 在裝好 paddle 的環境全綠：真 OCR 對 fixtures，輸出 PDF `get_text()` 含植入字串；`search_for("診斷證明書")` rect 與已知位置差 < 20pt；**rotated.pdf 必須通過同樣斷言**
3. CLI 對 fixtures 資料夾跑完：原檔 sha256 不變、CSV 列數=檔數、各狀態正確、Excel 開 CSV 不亂碼
4. LLM 兩態：fake server/Ollama 有回應 → 檔名符合模板；無服務 → 全 fallback 且批次完成
5. `debug.visible_text=true` 輸出紅字與影像文字目視對位
6. Edge/Acrobat 開輸出檔 Ctrl+F 中文可搜尋、反白位置合理（人工）
7. `ruff check .` 無錯誤

## 9. 已查證技術事實（2026-07-03，含來源）

- PaddleOCR 3.7（2026-06-11）預設 PP-OCRv6（PPLCNetV4，tiny/small/medium）；繁中 `lang="chinese_cht"`；單模型 50 語言（[release](https://github.com/PaddlePaddle/PaddleOCR/releases)、[HF blog](https://huggingface.co/blog/paddlepaddle/pp-ocrv6)）
- PaddleOCR 僅輸出文字+座標（`rec_texts/rec_scores/rec_polys`），不產可搜尋 PDF（[pipeline 文件](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/OCR.html)）
- PyMuPDF `insert_text(render_mode=3)`=隱形；內建 `"cjk"` 字型（Droid Sans Fallback）涵蓋中日韓；插入座標一律「未旋轉頁面」空間，`page.derotation_matrix` 轉換；`get_pixmap` 渲染已套用旋轉（[Page docs](https://pymupdf.readthedocs.io/en/latest/page.html)）
- paddlepaddle 3.3.0 支援 Python 3.9–3.13 Windows x64；需 VC++ Redistributable 2019+
- **本機探測（pymupdf 1.28.0，2026-07-03）**：`insert_text(fontname="cjk")` 會拋 `need font file or buffer`——"cjk" 只是 `Font()` 建構子別名；正確做法是每頁 `page.insert_font(fontname=..., fontbuffer=Font("cjk").buffer)` 再 `insert_text`。`Font("cjk")`=Droid Sans Fallback（buffer 3.4MB），ascender=1.04296875、descender=-0.265625，`text_length("診斷證明書",12)==60.0`（CJK 全形=1em），罕用字堃/峯/犇/喆/玥皆有字形。`search_for` 驗證：fs20 基線 (72,100) → Rect(72.0, 79.16, 172.0, 105.30)，與公式一致。TextWriter 亦可行（`write_text(render_mode=3)`）但 morph 為整批套用，不適合逐行寬度貼合。保留名 `"china-ts"` 可直接給 insert_text（不需 buffer），但為確保度量與 `Font("cjk")` 一致，採 insert_font+buffer 方案

## 10. 待實測查證（環境冒煙 → 寫入 docs/specs/paddleocr-api-facts.md）

1. `predict(ndarray)` 通道順序（RGB vs BGR）
2. `rec_polys` 實際點序與 dtype；`rec_boxes` 格式
3. `text_det_limit_side_len` 預設值（若會縮圖需覆寫避免小字漏偵）
4. 模型快取目錄位置（離線部署說明用）
5. `subset_fonts()` 瘦身效果（字型事實已測得，見 §9；瘦身效果待整合驗證時量測）
