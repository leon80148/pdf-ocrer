# RapidOCR API 實測事實（rapidocr 3.9.1 + onnxruntime 1.27.0）

> Spike 日期：2026-07-08，Windows 11 / Ryzen 5 8600G / Python 3.12。
> 本文件是 `rapidocr_engine.py` adapter 的單一事實來源；所有敘述皆經拋棄式 venv 實測。

## 1. 安裝與模型

- `pip install rapidocr onnxruntime`（版號需求：`rapidocr>=3.9`、`onnxruntime>=1.19`）。
- **預設模型內建在 wheel 內**：`site-packages/rapidocr/models/`（det=`PP-OCRv6_det_small.onnx`、rec=`PP-OCRv6_rec_small.onnx`、cls=`ch_ppocr_mobile_v2.0_cls_mobile.onnx`）。預設配置**不需下載**、天然支援離線部署（隨套件安裝即得）。
- 非預設變體（server/medium 等）首次使用才下載至同一 models 目錄；`Global.model_root_dir` 可改。
- `rapidocr` 模組**沒有 `__version__`**；用 `importlib.metadata.version("rapidocr")`。

## 2. 建構 API

```python
from rapidocr import RapidOCR, EngineType, LangDet, LangRec, ModelType, OCRVersion
ocr = RapidOCR()                      # 全預設（v6 small onnx，推薦）
ocr = RapidOCR(params={...})          # dotted-key 覆寫
```

- `RapidOCR.__init__(self, config_path=None, params=None)`；`params` 是 dotted-key dict。
- 枚舉值：`EngineType`：onnxruntime/openvino/paddle/torch/tensorrt/mnn；`ModelType`：mobile/server/tiny/small/medium；`OCRVersion`：PP-OCRv4/v5/v6；`LangRec` 含 `ch`（v6 統一模型）與 `chinese_cht`（繁中專用）。
- 常用 params 鍵（實測有效）：
  - `Det.engine_type` / `Det.ocr_version` / `Det.model_type` / `Det.lang_type` / `Det.limit_side_len` / `Det.limit_type`
  - `Rec.engine_type` / `Rec.ocr_version` / `Rec.model_type` / `Rec.lang_type`
  - `EngineConfig.onnxruntime.intra_op_num_threads`（-1=全核，預設）、`inter_op_num_threads`
  - `Global.text_score`（引擎層信心過濾；**adapter 不用它**，維持與 paddle 路徑一致改在轉換器過濾 `min_confidence`）、`Global.use_cls`、`Global.max_side_len`
- **繁中**：預設 `LangRec.CH`（v6 統一模型）對繁中輸出正確（實測 GT 相似度 0.9987、無簡體回歸）；`Rec.lang_type=LangRec.CHINESE_CHT` 也可用（0.989s/頁，品質相同）。**adapter 建議：`lang="chinese_cht"` 與 `"ch"` 都映射到預設統一模型即可**，不必切專用模型。

## 3. 呼叫與輸出

```python
result = ocr(img)     # __call__；img 接受 RGB numpy ndarray (H, W, 3) uint8（實測）
```

- 回傳 `RapidOCROutput`，屬性：`boxes`、`txts`、`scores`、`elapse`、`elapse_list`、`word_results`、`to_json()` 等。
- `boxes`：`np.ndarray (N, 4, 2) float32`，點序 **TL→TR→BR→BL**（與 paddle `rec_polys` 相同，可直接映射 `OcrLine.poly`）。
- `txts`：`tuple[str]`；`scores`：`tuple[float]`（0~1）。
- **空結果（無文字/空白圖）：`boxes`、`txts`、`scores` 全部是 `None`**，且 log 出 `WARNING ... The text detection result is empty`——adapter 轉換器必須先判 None 回 `[]`。
- console log 為 INFO 等級（loader 訊息）；可用 `Global.log_level` 調。

## 4. 效能實測（dense 2 頁診所文件、200dpi、s/頁 median，詳見 benchmark-results.md）

| 配置 | s/頁 | 冷 init | RSS | GT 相似度 |
|---|---|---|---|---|
| 預設（v6 small onnx，全核） | **0.90** | 1.2s | 320MB | 0.9987 |
| intra_op_num_threads=2 | 1.32 | 1.6s | 261MB | — |
| dpi 150 | 0.77 | 1.0s | 236MB | 0.9987 |

對照：paddle medium mkldnn-off（v0.2.0 現行預設）34.8s/頁 → **rapidocr 預設快約 38x**，且比 paddle-medium 更準（0.9987 vs 0.9973）、init 快 30x、RSS 一半。threads=2 只慢 47% → 多 worker 平行時每 worker 限 2 threads 可行性高。

## 5. adapter 參數映射（WO-1.3 規格）

| OcrConfig 欄位 | RapidOCR params | 說明 |
|---|---|---|
| `lang` | （不映射） | v6 統一模型即可；保留欄位相容 |
| `cpu_threads`（>0 時） | `EngineConfig.onnxruntime.intra_op_num_threads` | 0=不設（庫預設 -1 全核） |
| `det_limit_side_len`（非 None） | `Det.limit_side_len` | |
| `textline_orientation` | `Global.use_cls` | cls 模型開關 |
| `min_confidence` | （轉換器內過濾） | 與 paddle 路徑語意一致 |
| `dpi` | （不映射） | 由 render 端決定 |
| `det_model_name`/`rec_model_name` | （不映射，paddle 專用） | config 註解註明 |
| `device`/`enable_mkldnn` | （不映射，paddle 專用） | |
