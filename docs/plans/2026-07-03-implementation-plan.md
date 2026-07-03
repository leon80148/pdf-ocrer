# pdf_ocrer Implementation Plan

> **For agentic workers:** 本計畫由指揮者（Claude）逐任務派工給 Codex 實作，任務間由指揮者審查。
> 每個任務採 TDD：先寫測試 → 確認失敗 → 最小實作 → 測試通過 → ruff 乾淨。
> 規格唯一事實來源：`docs/specs/2026-07-03-pdf-ocrer-design.md`（下稱 SPEC）。
> PaddleOCR 實測事實：`docs/specs/paddleocr-api-facts.md`（下稱 FACTS）。

**Goal:** 批次把掃描 PDF 轉成可搜尋雙層 PDF 並以 LLM 自動命名的 Windows 桌面工具（開源）。

**Architecture:** PaddleOCR（PP-OCRv6 辨識）+ PyMuPDF（隱形文字層）+ 通用 LLM 連接層（OpenAI 相容協定，provider registry）。Tkinter GUI / CLI 雙入口，pipeline 批次協調。

**Tech Stack:** Python 3.12（相容 3.11+）、paddleocr 3.7、pymupdf 1.28、openai SDK 2.x、pytest 9、ruff。

## Global Constraints（每個任務都適用）

- 工作目錄：`C:\Users\User\projects\pdf_ocrer`（本機開發副本。原 NAS 位置 `X:\1.掛號.行政\pdf_ocrer` 為最終同步目標——Codex 行程無法存取網路磁碟，SMB 開發 IO 也慢；完成後由指揮者 robocopy 同步回去）
- 測試直譯器（venv 在本機碟）：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe`
- 跑單元測試：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m pytest -q`（pyproject 已設 `-m "not integration"`）
- 跑 lint：`C:\Users\User\.venvs\pdf_ocrer\Scripts\python.exe -m ruff check .`（必須 0 錯誤）
- **單元測試絕不觸網、絕不 import paddleocr/paddlepaddle、絕不開 GUI 視窗**
- 型別註記必寫；docstring 與程式內註解用英文；使用者可見字串用繁體中文
- **不要執行 git commit**（最後由指揮者統一 commit）
- 遵循 SPEC 的介面簽名，一字不差；改介面須先回報指揮者

---

### Task C1: 專案骨架 + pyproject + config 模組

**Files:**
- Create: `pyproject.toml`, `src/pdf_ocrer/__init__.py`, `src/pdf_ocrer/config.py`,
  `config.example.toml`, `naming_prompt.txt`, `tests/test_config.py`, `tests/__init__.py`（空）

**Interfaces（Produces，SPEC §4.1 全文照抄）:**
`OcrConfig / OutputConfig / NamingConfig / LlmConfig / DebugConfig / AppConfig`（frozen dataclasses，欄位與預設值見 SPEC §4.1）、`ConfigError(ValueError)`、`load_config(path: Path | None = None) -> AppConfig`、`resolve_api_key(cfg: LlmConfig) -> str`（優先序：cfg.api_key → 環境變數 `PDF_OCRER_API_KEY` → `""`）。

**pyproject 要點:** hatchling build backend；`[project]` name=`pdf-ocrer`、requires-python `>=3.11`、dependencies=`["paddleocr>=3.7", "pymupdf>=1.26", "openai>=1.60,<3"]`、optional-dependencies `paddle-cpu=["paddlepaddle==3.3.*"]`、`dev=["pytest>=8", "ruff"]`；`[project.scripts] pdf-ocrer = "pdf_ocrer.cli:main"`；`[project.gui-scripts] pdf-ocrer-gui = "pdf_ocrer.gui:run_gui"`；pytest 設定 `testpaths=["tests"]`、`addopts = "-m 'not integration'"`、markers 註冊 `integration: needs real PaddleOCR models`；ruff `line-length=100`、`target-version="py311"`。

**Steps:**
- [ ] 寫 `tests/test_config.py`，至少涵蓋：
```python
def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.ocr.dpi == 200 and cfg.llm.provider == "openai_compatible"

def test_toml_overrides(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[ocr]\ndpi = 300\n', encoding="utf-8")
    assert load_config(p).ocr.dpi == 300

def test_unknown_key_warns(tmp_path, recwarn):
    p = tmp_path / "c.toml"; p.write_text('[ocr]\nbogus = 1\n', encoding="utf-8")
    load_config(p); assert any("bogus" in str(w.message) for w in recwarn.list)

def test_invalid_dpi_raises(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[ocr]\ndpi = 10\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="dpi"): load_config(p)

def test_api_key_env_fallback(monkeypatch):
    monkeypatch.setenv("PDF_OCRER_API_KEY", "sk-x")
    assert resolve_api_key(LlmConfig()) == "sk-x"
```
- [ ] 跑測試確認 import error 失敗 → 實作 `config.py` → 測試綠
- [ ] `config.example.toml`：所有欄位含註解與預設值，並附雲端 provider 切換範例（Ollama/OpenAI/Gemini/Anthropic 相容端點的 base_url 註解）
- [ ] `naming_prompt.txt`：SPEC §5 內容
- [ ] `pip install -e . --no-deps` 讓 `import pdf_ocrer` 生效
- [ ] `pytest -q` 全綠、`ruff check .` 乾淨

---

### Task C2: 測試 fixtures 產生器

**Files:**
- Create: `tests/fixtures_gen.py`, `tests/conftest.py`

**Interfaces（Produces）:**
```python
# tests/fixtures_gen.py — 純 pymupdf，不依賴 src
GT_LINES: list[tuple[tuple[float, float], float, str]] = [
    ((72, 100), 20, "診斷證明書"),
    ((72, 140), 12, "病患:王小明 日期:2026年6月15日"),
    ((300, 400), 14, "高雄市安家診所"),
]  # (基線插入點 pt, fontsize, 文字)；與 SPEC §8 驗收斷言共用
def build_native(path: Path) -> None      # 原生文字層 PDF：先 page.insert_font(fontname="cjkF",
                                          #   fontbuffer=pymupdf.Font("cjk").buffer) 再 insert_text
                                          #   （"cjk" 不能直接當 insert_text 的 fontname，見 SPEC §9）
def build_scanned(path: Path) -> None     # 無文字層假掃描：native 渲染 dpi=200 成圖 → 新頁 insert_image
def build_rotated(path: Path) -> None     # scanned + page.set_rotation(90)
def build_mixed(path: Path) -> None       # 第1頁掃描影像、第2頁原生文字
def build_encrypted(path: Path) -> None   # scanned + AES-256 user_pw="test"
def build_corrupt(path: Path) -> None     # path.write_bytes(b"%PDF-1.4 not really a pdf")
def build_all(folder: Path) -> dict[str, Path]  # 檔名鍵：native/scanned/rotated/mixed/encrypted/corrupt
```
```python
# tests/conftest.py
@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path   # build_all 進 session tmp dir
@pytest.fixture()
def work_folder(fixtures_dir, tmp_path) -> Path  # 複製一組 fixtures 到函式級 tmp（測試可自由改動）
```

**Steps:**
- [ ] 寫 `tests/test_fixtures_gen.py`（本任務自己的測試）：
```python
def test_scanned_has_no_text_layer(fixtures_dir):
    doc = pymupdf.open(fixtures_dir / "scanned.pdf")
    assert doc[0].get_text().strip() == ""

def test_native_has_text(fixtures_dir):
    assert "診斷證明書" in pymupdf.open(fixtures_dir / "native.pdf")[0].get_text()

def test_rotated_is_90(fixtures_dir):
    assert pymupdf.open(fixtures_dir / "rotated.pdf")[0].rotation == 90

def test_encrypted_needs_pass(fixtures_dir):
    assert pymupdf.open(fixtures_dir / "encrypted.pdf").needs_pass
```
- [ ] 實作 → 測試綠 → ruff 乾淨

---

### Task C3: 通用 LLM 連接層 + 命名器

**Files:**
- Create: `src/pdf_ocrer/llm_providers.py`, `src/pdf_ocrer/llm_namer.py`,
  `tests/test_llm_providers.py`, `tests/test_llm_namer.py`

**Interfaces:** SPEC §4.4 與 §4.5 全部簽名照抄（`LLMClient` Protocol、`LLMError`、`register_provider`、`create_client`、`OpenAICompatClient`、`build_prompt`、`sanitize_filename`、`resolve_collision`、`suggest_filename`）。

**行為重點:** sanitize 八步清洗順序（SPEC §4.5）逐步都要有對應測試；`suggest_filename` 對 `LLMError` 重試 1 次（共 2 次呼叫）後 fallback。`OpenAICompatClient` 用 `openai.OpenAI(base_url=..., api_key=resolve_api_key(cfg) or "not-needed", timeout=...)`。

**Steps:**
- [ ] 先寫兩個測試檔。核心案例（必含，可再加）：
```python
# test_llm_providers.py
def test_create_client_none_provider():
    assert create_client(replace(LlmConfig(), provider="none")) is None

def test_create_client_unknown_lists_available():
    with pytest.raises(ConfigError, match="openai_compatible"):
        create_client(replace(LlmConfig(), provider="wat"))

def test_openai_compat_calls_sdk(monkeypatch):
    captured = {}
    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            msg = SimpleNamespace(content="20260615_診斷證明書_王小明")
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    class FakeOpenAI:
        def __init__(self, **kw): captured["init"] = kw
        chat = SimpleNamespace(completions=FakeCompletions())
    monkeypatch.setattr("pdf_ocrer.llm_providers.OpenAI", FakeOpenAI)
    out = create_client(LlmConfig()).complete("hi")
    assert out == "20260615_診斷證明書_王小明"
    assert captured["init"]["api_key"] == "not-needed"      # 空 key 的本機情境
    assert captured["model"] == "qwen3:8b"

def test_openai_compat_empty_response_raises(monkeypatch):
    ...  # choices[0].message.content 為 None/"" → LLMError

# test_llm_namer.py — sanitize 表格驅動
@pytest.mark.parametrize("raw,expected", [
    ("<think>嗯…想想</think>\n20260615_轉診單_王小明", "20260615_轉診單_王小明"),
    ('「20260615_收據」', "20260615_收據"),
    ("檔名：20260615_報告.pdf", "檔名：20260615_報告"),   # 只去尾端 .pdf；冒號屬非法字元 → 實際斷言刪除後結果
    ("a/b\\c:d*e?f\"g<h>i|j", "abcdefghij"),
    ("name...   ", "name"),
    ("CON", "_CON"),
    ("", None),
    ("<think>only</think>", None),
])
def test_sanitize(raw, expected): ...

def test_sanitize_truncates_to_max():
    assert len(sanitize_filename("字" * 300, 80)) == 80

def test_collision_disk_and_batch(tmp_path):
    (tmp_path / "報告.pdf").touch()
    used: set[str] = set()
    assert resolve_collision(tmp_path, "報告", used) == "報告_2"
    assert resolve_collision(tmp_path, "報告", used) == "報告_3"   # 批次內
    assert resolve_collision(tmp_path, "RePort", {"report"}) == "RePort_2"  # casefold

def test_suggest_retries_once_then_fallback():
    calls = []
    class Boom:
        def complete(self, p): calls.append(1); raise LLMError("down")
    stem, src = suggest_filename("文字", "scan001", make_cfg(), Boom(), "$text")
    assert (stem, src) == ("scan001_OCR", "fallback") and len(calls) == 2

def test_build_prompt_user_braces_safe():
    out = build_prompt("模板{x} $text $unknown", "T", "o", "20260703")
    assert "T" in out and "{x}" in out and "$unknown" in out
```
- [ ] 確認測試失敗 → 實作兩模組 → 測試綠 → ruff 乾淨

---

### Task C4: ocr_engine 封裝

**Files:**
- Create: `src/pdf_ocrer/ocr_engine.py`, `tests/test_ocr_engine.py`
- Append: `tests/test_integration.py`（`@pytest.mark.integration` 部分）

**Interfaces:** SPEC §4.2（`OcrLine`、`OcrEngineProtocol`、`PaddleOcrEngine`），另加可單測的純函式：
```python
def lines_from_prediction(pred: Mapping[str, Any], min_confidence: float) -> list[OcrLine]
# pred 對應 predict() 回傳 result[0]，讀 rec_texts / rec_scores / rec_polys
# poly 依 FACTS 記載的點序正規化為 TL,TR,BR,BL；float 化
```

**前置:** 先讀 FACTS（通道順序、poly 點序、predict 參數）。若 FACTS 與 SPEC 衝突，以 FACTS 為準並回報。

**Steps:**
- [ ] 測試（不 import paddle）：
```python
def test_lines_from_prediction_filters_and_converts():
    pred = {"rec_texts": ["高", "", "低分"], "rec_scores": [0.9, 0.9, 0.3],
            "rec_polys": [np.array([[0,0],[10,0],[10,5],[0,5]])]*3}
    lines = lines_from_prediction(pred, 0.5)
    assert [l.text for l in lines] == ["高"] and lines[0].poly[0] == (0.0, 0.0)

def test_module_import_is_paddle_free():
    code = "import pdf_ocrer.ocr_engine, sys; assert 'paddleocr' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
```
- [ ] `test_integration.py` 加真實冒煙（marker integration）：以 `tests/fixtures_gen.build_scanned` 產圖 → `PaddleOcrEngine(OcrConfig()).recognize(render)` → 斷言含「診斷證明書」
- [ ] 實作（lazy import、init 參數照 SPEC §4.2、log 回呼）→ 單元綠 → ruff 乾淨（integration 由指揮者統一跑）

---

### Task C5: pdf_processor 座標換算 + 隱形文字層（技術核心）

**Files:**
- Create: `src/pdf_ocrer/pdf_processor.py`, `tests/test_pdf_processor.py`
- Append: `tests/test_integration.py`

**Interfaces:** SPEC §4.3 全部（`EncryptedPdfError`、`has_text_layer`、`render_page`、`add_text_layer`、`PageReport`、`PdfResult`、`process_pdf`）。`BatchCancelled` 定義於 pipeline（C6）——本任務先在模組內定義 `class BatchCancelled(Exception)` 佔位？**否**：取消檢查以 callback 例外拋出會造成循環相依 → 將 `BatchCancelled` 定義在 `pdf_processor.py`，pipeline 從此處 import（SPEC §4.6 相應調整，已由指揮者核准）。

**Steps:**
- [ ] **座標慣例探針（先做）**：寫 10 行臨時腳本確認「`search_for`/`get_text` 回傳座標」在 rotation=90 頁面上的空間慣例（未旋轉 vs 顯示空間），把結論寫進 `pdf_processor.py` 模組 docstring 與 FACTS 附錄，再據以寫測試斷言
- [ ] 單元測試（fake OcrLine，不用真 OCR）核心案例：
```python
def test_render_page_dims(fixtures_dir):
    page = pymupdf.open(fixtures_dir / "scanned.pdf")[0]
    img = render_page(page, 200)
    assert img.shape == (2339, 1653, 3) and img.dtype == np.uint8  # A4@200dpi

def test_add_text_layer_roundtrip_search(fixtures_dir, tmp_path):
    doc = pymupdf.open(fixtures_dir / "scanned.pdf"); page = doc[0]
    # 假 OCR 行：模擬「診斷證明書」位於渲染影像中的已知 px 區域（由 GT_LINES 換算）
    line = OcrLine(text="診斷證明書", poly=px_poly_from_gt(GT_LINES[0], dpi=200), score=0.99)
    add_text_layer(page, [line], dpi=200)
    out = tmp_path / "o.pdf"; doc.save(out)
    p2 = pymupdf.open(out)[0]
    assert "診斷證明書" in p2.get_text()
    rects = p2.search_for("診斷證明書"); assert rects
    assert abs(rects[0].x0 - 72) < 20 and abs(rects[0].y1 - 100) < 25  # 基線/左緣 ±容差

def test_add_text_layer_rotated_page(fixtures_dir, tmp_path):
    ...  # 同上但 rotated.pdf：poly 給「顯示空間」px，斷言依探針確認的慣例寫

def test_invisible_by_default(...):   # render 輸出頁 → 與原 scanned 渲染逐像素相同（render_mode=3 不落墨）
def test_visible_debug_mode(...):     # visible=True → 渲染影像與原圖不同（有紅字）
def test_process_pdf_mixed(fixtures_dir):  # mixed.pdf + FakeEngine → 頁1 action=="ocr"、頁2 "kept_existing"
def test_process_pdf_encrypted_raises(fixtures_dir): ...
def test_process_pdf_cancel(fixtures_dir): # cancel 預先 set → BatchCancelled
```
- [ ] 實作（公式照 SPEC §4.3；`natural_w<=0` 跳過該行；模組層快取 `Font("cjk")`）→ 綠 → ruff
- [ ] `test_integration.py` 加端到端：真引擎 process_pdf(scanned) → save → get_text 含全部 GT 文字、search_for 位置斷言、rotated.pdf 同樣通過

---

### Task C6: pipeline 批次協調 + CSV + CLI

**Files:**
- Create: `src/pdf_ocrer/pipeline.py`, `src/pdf_ocrer/cli.py`, `src/pdf_ocrer/__main__.py`,
  `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:** SPEC §4.6 / §4.7 全部；`BatchCancelled` 從 `pdf_processor` import。
CLI 可測性：`main(argv=None, *, engine_factory=None, client_factory=None) -> int`（None 用真工廠；測試注入 fake）。

**Steps:**
- [ ] `tests/test_pipeline.py`（FakeEngine 固定回 GT 行、FakeClient 固定回名或 raise）：
  - 混合資料夾（scanned/native/encrypted/corrupt + 輸出子夾裡塞一個 pdf）→ 狀態逐一斷言、輸出子夾內檔案不被掃到
  - CSV：存在、首 3 bytes == `\xef\xbb\xbf`（utf-8-sig）、列數 = 檔數、欄位順序照 SPEC
  - 原檔 sha256 前後不變
  - 兩檔 LLM 回同名 → 第二檔 `_2`
  - FakeClient 全 raise → 全 fallback、批次完成、naming_source=="fallback"
  - cancel_event 在第 1 檔後 set（用 progress_cb 觸發）→ cancelled=True、results 只含已完成
  - 空資料夾 → csv_path is None、results==[]
- [ ] `tests/test_cli.py`：`--version`；不存在資料夾 → 2；fixtures 資料夾 + fake factories + `--no-llm` → 0、CSV 產生；`--dpi 300` 傳達到 engine_factory 收到的 cfg
- [ ] 實作 → 綠 → ruff

---

### Task C7: Tkinter GUI

**Files:**
- Create: `src/pdf_ocrer/gui.py`, `tests/test_gui.py`

**Interfaces:** SPEC §4.8（`run_gui`；內部 `App` 類）。worker→UI 事件用 `queue.Queue`，事件型別：`("log", str) / ("progress", file_i, file_n, page_i, page_n, name) / ("done", BatchSummary) / ("error", str)`。

**Steps:**
- [ ] `tests/test_gui.py`：`tkinter` importorskip；`try: root=tk.Tk() except: skip`；建 `App` 不 mainloop：斷言標題含版本、佇列事件 `("log","x")` 經 `app._drain_queue()` 後出現在日誌 widget、`("done", summary)` 啟用開始鈕
- [ ] 實作（SPEC §4.8 全部元件；worker thread daemon；關窗確認）→ 綠 → ruff

---

### Task C8: 整合驗證（指揮者主導）

- [ ] `pytest -m integration -v`（真 PaddleOCR）全綠
- [ ] CLI 對 fixtures 資料夾全流程 + `--no-llm`；再配 Ollama/fake OpenAI-compat server 跑 LLM 態
- [ ] `debug.visible_text=true` 產出紅字 PDF，渲染比對對位
- [ ] 效能基線記錄到 README（頁/秒）
- [ ] GUI 手動冒煙（開視窗、跑一批、取消一次）

### Task C9: 開源文件（Codex 起草、指揮者潤飾）

- [ ] `README.md`（en）/`README.zh-TW.md`：功能、截圖佔位、安裝（含 paddle-cpu extras、VC++ 前置）、快速開始、config 全表、各 LLM provider 設定範例（Ollama/OpenAI/Gemini/Anthropic/LM Studio）、隱私說明（全本機模式）、FAQ（模型快取位置、離線部署、罕用字字型）
- [ ] `CONTRIBUTING.md`：dev setup、測試分層（unit/integration）、ruff、PR 流程
- [ ] `LICENSE`（MIT, 2026）

### Task C10: 最終審查 + commit（指揮者）

- [ ] `/code-review` 全 diff → 修 confirmed 問題（派回 Codex）
- [ ] 全測試綠 + ruff 乾淨 + 文件互鏈檢查
- [ ] 單一 commit（含 Co-Authored-By）

## 任務相依

C1 → {C2, C3, C4} 可並行 → C5（需 C2 fixtures、C4 OcrLine）→ C6 → C7 → C8 → C9 → C10
