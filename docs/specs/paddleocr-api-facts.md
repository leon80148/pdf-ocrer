# PaddleOCR 3.7.0 API 實測事實（本機冒煙查證）

實測日期：2026-07-03｜環境：Windows 11、Python 3.12.10、paddlepaddle 3.3.0 (CPU)、paddleocr 3.7.0、pymupdf 1.28.0、numpy 2.3.5

> 本文件是 `ocr_engine.py` / `pdf_processor.py` 實作的**唯一事實來源**，內容全部來自實際執行輸出，非文件推測。

## 1. 建構參數（實測可用）

```python
PaddleOCR(
    lang="chinese_cht",
    ocr_version="PP-OCRv6",
    use_doc_orientation_classify=False,   # 關：保持座標在原始渲染影像空間
    use_doc_unwarping=False,              # 關：同上，且 CPU 慢
    use_textline_orientation=True,
    device="cpu",
    enable_mkldnn=False,                  # ★必要★ 見 §2
)
```

載入模型：`PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec` + `PP-LCNet_x1_0_textline_ori`。
warm init（模型已下載）約 0.7s。

## 2. paddlepaddle 3.3.0 oneDNN bug（★關鍵）

`enable_mkldnn` 保持預設（True）時，`predict()` 直接拋：

```
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute not support
[pir::ArrayAttribute<pir::DoubleAttribute>] (onednn_instruction.cc:118)
```

`enable_mkldnn=False` 可完全繞過。**代價：CPU 推論顯著變慢**（本機 A4@200dpi 約 22 秒/頁）。
調校方向（C8）：換 `PP-OCRv6_small/tiny` 模型、追蹤 paddle 修復版本、或評估 onnxruntime 路線。

**2026-07-08 閘門實測更新**（詳見 `benchmark-results.md`）：

- paddlepaddle **3.3.1 未修**此 bug（同錯誤重現）。
- paddlepaddle **3.2.2 + mkldnn 正常**（官方 issue #77340 建議的 workaround）：與 paddleocr 3.7.0
  / paddlex 3.7.2 相容（pip check 乾淨、全測試綠），medium 模型 dense 件 34.8 → 5.06 秒/頁
  （**6.9x**）且輸出逐字相同。`paddle-cpu` extra 已改釘 `paddlepaddle==3.2.*`。
- onnxruntime 路線（rapidocr）實測更快（0.90 秒/頁），已列為推薦引擎；見 `rapidocr-api-facts.md`。

## 3. predict() 簽名（逐次呼叫可覆寫）

```
predict(input, *, use_doc_orientation_classify=None, use_doc_unwarping=None,
        use_textline_orientation=None, text_det_limit_side_len=None,
        text_det_limit_type=None, text_det_thresh=None, text_det_box_thresh=None,
        text_det_unclip_ratio=None, text_rec_score_thresh=None, return_word_box=None)
```

- `input` 接受：檔案路徑 str、`np.ndarray`
- **通道順序：RGB 與 BGR 實測結果完全相同**（分數到小數三位一致）。約定：**傳 RGB**（pixmap 原生順序，與路徑輸入結果一致）

## 4. 回傳結構

`predict()` 回傳 `list`（每頁/每圖一個 `OCRResult`，dict 式存取）：

```python
res = ocr.predict(img)[0]
sorted(res.keys()) == ['doc_preprocessor_res', 'dt_polys', 'input_path', 'model_settings',
 'page_index', 'rec_boxes', 'rec_polys', 'rec_scores', 'rec_texts', 'return_word_box',
 'text_det_params', 'text_rec_score_thresh', 'text_type', 'textline_orientation_angles',
 'vis_fonts']
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `rec_texts` | `list[str]` | 每行文字 |
| `rec_scores` | `list[float]` | 信心度（實測 1.000 / 0.982） |
| `rec_polys` | `list[np.ndarray(4,2) int16]` | **點序 TL,TR,BR,BL**（實測驗證）；像素座標，原點左上 |
| `rec_boxes` | `list[np.ndarray(4,) int16]` | `[x1,y1,x2,y2]` |

注意：poly 是 **int16**，換算前先轉 float。

## 5. 座標對位驗證（渲染 dpi=200，scale=200/72≈2.778）

Ground truth：`insert_text` 基線 pt(72,100)、fontsize 20、「診斷證明書」（Font("cjk")：asc=1.04296875、desc=-0.265625）

| 量 | 預期 px | 實測 poly | 誤差 |
|---|---|---|---|
| 左緣 x | 200 | 196 | -4px |
| 上緣 y（基線-asc×fs） | 220 | 225 | +5px |
| 下緣 y（基線-desc×fs） | 293 | 288 | -5px |
| 寬（5字×20pt） | 278 | 283 | +5px |

→ **`px × 72/dpi` 線性換算成立**，偵測框緊貼字面（±5px @200dpi ≈ ±1.8pt）。

## 6. 辨識品質（合成影像）

3 行 GT 全數正確辨識（含 fs12 小字與全形冒號差異：`病患:王小明` 辨識為半形冒號 — 命名 prompt 與斷言不要依賴全半形冒號一致）。

## 7. 模型快取（離線部署）

```
C:\Users\User\.paddlex\official_models\
├── PP-LCNet_x1_0_textline_ori
├── PP-OCRv6_medium_det
└── PP-OCRv6_medium_rec
```

離線機器部署：拷貝整個 `official_models` 資料夾到目標機器同路徑即可（首次執行不再需要網路）。

## 8. PyMuPDF 字型事實（見 SPEC §9）

- `Font("cjk")` = Droid Sans Fallback Regular，buffer 3.4MB；asc=1.04296875、desc=-0.265625
- `insert_text(fontname="cjk")` 不可用 → 每頁先 `page.insert_font(fontname=..., fontbuffer=Font("cjk").buffer)`
- `text_length("診斷證明書",12)==60.0`（CJK=1em）；罕用字 堃峯犇喆玥 全部有字形
- `get_pixmap(dpi=200, alpha=False)` → Colorspace DeviceRGB、`samples` 為 RGB、A4=(2339,1653,3) uint8

## 9. 對規格的增補（已回饋 SPEC §4.1/§4.2）

`OcrConfig` 新增三個可選欄位（皆 pass-through，None=函式庫預設）：

```python
enable_mkldnn: bool = False              # 見 §2，預設關
det_limit_side_len: int | None = None    # 小字漏偵時調大（predict 時傳 text_det_limit_side_len）
rec_model_name: str | None = None        # 速度調校用（如 "PP-OCRv6_small_rec"；det 同理 det_model_name）
det_model_name: str | None = None
```

2026-07-08 增補（Stage 1 引擎選擇）：`PaddleOCR(...)` 建構子額外接受 `cpu_threads: int`
kwarg（實測 3.7.0 + paddle 3.2.2 通過：`PaddleOCR(cpu_threads=4, ...)` 建構與 predict 正常；
內部進 PaddleX `PaddlePredictorOption.cpu_threads`，要求 ≥1，故 `OcrConfig.cpu_threads == 0`
時不傳、沿用函式庫預設）。`use_textline_orientation` 同為建構子 kwarg，已由
`OcrConfig.textline_orientation` 透傳（預設 True）。
