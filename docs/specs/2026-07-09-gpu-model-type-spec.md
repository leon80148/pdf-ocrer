# SDD 規格：GPU 加速（CPU/CUDA/DirectML）+ RapidOCR model_type（Track B）

> 建立日期：2026-07-09。狀態：approved（依總體計畫 Track B）。
> 單一事實來源：本規格 + `docs/specs/rapidocr-api-facts.md`。

## 1. 目標與範圍

讓 pdf_ocrer 的 **rapidocr 引擎**可選擇性使用 GPU 加速，並可選用更大的 OCR 模型（`server`）換取更高準確度。**僅適用於原始碼 / pip 安裝**——打包安裝檔（Track A）維持純 CPU + `small` 模型，本規格不影響其行為。

**非目標（刻意不做）**：
- paddle 引擎的 GPU 路徑（維持現狀，device kwarg 照舊透傳，不驗證/不支援 paddle GPU）
- GPU 硬體自動偵測與自動選擇後端（由使用者以設定明確指定）
- CUDA 與 DirectML 同時啟用（技術上互斥；提供兩個互斥 pip extras 讓使用者擇一）

## 2. 背景事實

- `OcrConfig.device` 欄位已存在（`config.py`，預設 `"cpu"`），但目前 **無驗證、rapidocr 路徑完全未讀取**。
- RapidOCR 的 `EngineConfig.onnxruntime` 設定內建 `use_cuda`/`use_dml` 布林開關（Stage 1 spike 於 config.yaml schema 觀察到）；預設皆 False，即使裝了 GPU 版 onnxruntime 也不會自動用 GPU，**必須顯式設 True**。
- `onnxruntime`、`onnxruntime-gpu`（CUDA）、`onnxruntime-directml`（DirectML）是三個獨立 PyPI 套件，**共用同一 `onnxruntime` 匯入名稱**，同時安裝會互相覆蓋 → 只能擇一。
- onnxruntime 若被要求的 execution provider 不存在，**靜默 fallback 回 CPU + 印警告**——使用者可能「設了 GPU 沒感覺變快」而不自知裝錯套件。
- RapidOCR `ModelType` 列舉：`mobile` / `tiny` / `small` / `medium` / `server`；預設 `small`（PP-OCRv6，內建 wheel、免下載）；`server` 等變體首次使用才下載（需網路一次）。

## 3. 設計

### 3.1 pyproject.toml — 兩個互斥 GPU extras

```toml
rapidocr-gpu-cuda = ["rapidocr>=3.9", "onnxruntime-gpu>=1.19"]      # NVIDIA 專用
rapidocr-gpu-dml  = ["rapidocr>=3.9", "onnxruntime-directml>=1.19"] # Windows 通用（NVIDIA/AMD/Intel）
```
兩者皆不含裸 `onnxruntime`，避免與 GPU 變體衝突。文件明確寫「擇一安裝，需先移除已裝的 CPU 版 onnxruntime」。

### 3.2 config.py — device / model_type 驗證

- `OcrConfig.model_type: str = "small"`（新欄位）
- `_validate_ocr` 新增：
  - `device` 必須是字串且 casefold ∈ `{"cpu", "cuda", "dml"}`，否則 `ConfigError`；正規化為小寫回存（比照既有 `engine` 欄位寫法 `replace(cfg, ...)`）
  - `model_type` 必須是字串且 casefold ∈ `{"mobile", "tiny", "small", "medium", "server"}`，否則 `ConfigError`；正規化小寫回存
- **device 語意**：`cuda`/`dml` 僅適用 rapidocr 引擎；paddle 引擎只支援 `cpu`（paddle GPU 非目標）。此為文件層約定，不做跨引擎交叉驗證（保持簡單）。
- PaddleOcrEngine **不改動**（既有 device 透傳行為與測試不變）。

### 3.3 rapidocr_engine.py — 參數映射 + 執行後端狀態 log

`_rapidocr_params(cfg)` 新增映射：
| OcrConfig | RapidOCR params | 條件 |
|---|---|---|
| `device="cuda"` | `EngineConfig.onnxruntime.use_cuda = True` | device==cuda |
| `device="dml"` | `EngineConfig.onnxruntime.use_dml = True` | device==dml |
| `model_type≠"small"` | `Det.model_type` + `Rec.model_type` = model_type | model_type!=small |

新增純函式 `_device_status_message(device, available_providers) -> str | None`：
- `device=="cpu"` → `None`（不 log）
- device 需要的 provider（cuda→`CUDAExecutionProvider`、dml→`DmlExecutionProvider`）**在** `available_providers` 中 → 回傳「OCR 使用 GPU 加速（<provider>）」
- **不在** → 回傳可操作警告：「已設定 device=X 但安裝的 onnxruntime 不支援 <provider>（可用：...），將以 CPU 執行。NVIDIA 請裝 pdf-ocrer[rapidocr-gpu-cuda]，其他 GPU 請裝 pdf-ocrer[rapidocr-gpu-dml]。」

`RapidOcrEngine._get_ocr()`：建構引擎時，`import onnxruntime` 並呼叫 `onnxruntime.get_available_providers()`，把 `_device_status_message(...)` 結果（若非 None）透過 `self._log` 印出。此舉把「靜默 fallback」風險轉為明確、可行動的提示。

### 3.4 config.example.toml — 註解

`[ocr]` 補充 `model_type` 說明與 GPU 使用指引（裝哪個 extra、`device` 設定、cuda/dml 僅 rapidocr、server 首次需網路下載）。

## 4. 測試策略（TDD，全部不需真 GPU）

`tests/test_config.py`：
- `device` 合法值（cpu/cuda/dml，含大小寫正規化）載入正確；非法值（如 "gpu"）拋 ConfigError
- `model_type` 合法值載入正確；非法值拋 ConfigError；預設為 "small"

`tests/test_rapidocr_engine.py`：
- `_rapidocr_params`：device=cuda→含 use_cuda=True；device=dml→含 use_dml=True；device=cpu→不含 GPU 鍵；model_type=server→含 Det/Rec.model_type；model_type=small→不含
- `_device_status_message`：cpu→None；cuda 且 provider 可用→GPU 訊息；cuda 但 provider 不可用→警告訊息含 extra 名稱；dml 同理
- `RapidOcrEngine` 建構時經 fake rapidocr + fake onnxruntime module（monkeypatch）驗證 device 狀態 log 有被呼叫（不需真 GPU/真套件）

## 5. 實機驗證（步驟，需 GPU 硬體，非單元測試）

裝 `[rapidocr-gpu-dml]`（任何 Windows 內顯可測）與（若有 NVIDIA）`[rapidocr-gpu-cuda]`，用 `scripts/bench_ocr.py` 對比 CPU/GPU × small/server 的秒/頁與準確度，結果記入 `docs/specs/benchmark-results.md`——這才是「server 模型是否值得」的實證答案。本規格的程式碼部分（§3、§4）不依賴此步驟即可完成與測試。
