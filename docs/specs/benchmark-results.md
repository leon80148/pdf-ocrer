# Benchmark Results

> 量測工具：`scripts/bench_ocr.py`（paddle 路徑）與 spike 腳本（rapidocr 路徑，API 同 render 流程）。
> 機器：AMD Ryzen 5 8600G（6C/12T）/ 32GB / Windows 11。測試件：
> - `synthetic`：fixtures_gen 稀疏頁（3 行繁中），4 頁
> - `dense`：合成診所文件 2 頁（診斷證明+檢驗報告，~33 行/頁，200dpi 掃描式），較接近真實件
> `ocr_s_med` 排除首頁 warmup。similarity 為正規化後 difflib ratio。

## GPU（DirectML）實機驗證（2026-07-09，dense 2 頁、rapidocr small）

以實際 `RapidOcrEngine`（device 映射走出貨程式碼路徑）在 AMD Radeon 760M 內顯上實測：

| device | 模型 | 冷 init | s/頁 med | 準確度 | 備註 |
|---|---|---|---|---|---|
| cpu | small | 3.49s | 0.962 | trad_ok | 基準 |
| dml | small | 2.91s | 1.005 | trad_ok | RapidOCR log 確認啟用 DirectML |

**結論**：`device="dml"` 的程式碼接線正確（RapidOCR 回報「try to use DirectML as
primary provider」，準確度不變）；但在**弱內顯 + small 模型**上，DirectML 開銷略大於
效益（0.96x，等於沒變快）。GPU 加速的實際效益出現在**獨立顯卡**或**較大的 `server`
模型**（server 首次需連網下載、CPU 上明顯慢，適合搭 GPU）。因此：small 模型維持 CPU
即可；GPU/server 屬進階選項，程式碼已就緒並經單元測試，實際增益依硬體而定。model_type
的 PP-OCRv6 世代已於 D1 確認為最新最準，無須更換。

## 決策 D1（2026-07-08）

**推薦引擎：`rapidocr`（PP-OCRv6 small ONNX，onnxruntime CPU）**：

- dense 實測 **0.90 s/頁**，比 v0.2.0 現行預設（paddle medium、mkldnn off）**快 ~38x**
- 精度**高於** paddle-medium（GT 相似度 0.9987 vs 0.9973）、繁體無簡化回歸、GT 全召回
- 冷啟動 1.2s（paddle 38s → 30x）、RSS 320MB（約一半）、模型內建 wheel 免下載（離線友善）
- 不受 paddlepaddle oneDNN bug / 版本鎖影響

**預設引擎維持 `paddle`**（既有安裝零意外；rapidocr 為 optional extra），但：

- `config.example.toml` 與 README 推薦 `engine = "rapidocr"`（`pip install pdf-ocrer[rapidocr]`）
- paddle 路徑同步改善：`paddle-cpu` extra 改釘 `paddlepaddle==3.2.*`（3.2.2 實測 oneDNN 正常且與 paddleocr 3.7 相容：pip check 乾淨、114 unit + 2 integration 全綠）；`enable_mkldnn` 程式預設仍 false（3.3.x 安全），文件註明 3.2.x 建議開 true（medium 快 6.9x、輸出逐字相同）
- 開發/生產 venv 已降至 paddlepaddle 3.2.2

閘門結論：G1 **失敗如預期**（paddlepaddle 3.3.1 + mkldnn 仍拋 `ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`）；G2 **通過**（3.2.2 + mkldnn 正常）；G3 rapidocr 勝出；G4：dpi150 精度不變快 14%（預設仍 200，保守），rapidocr threads=2 只慢 47%（Stage 4 多 worker 每 worker 限 2 threads 可行），頁面批次 predict 增益評估 <15% 不產品化。

## Results（dense 2 頁診所文件）

| label | engine | 模型 | mkldnn | paddle 版 | s/頁 med | 冷 init s | RSS MB | GT 相似度 | vs 現行 |
|---|---|---|---|---|---|---|---|---|---|
| G0-medium-dense | paddle | v6 medium | off | 3.3.0 | 34.81 | 38.0 | 652 | 0.9973 | 1x（現行） |
| G0-small-dense | paddle | v6 small | off | 3.3.0 | 10.51 | 13.1 | 542 | — | 3.3x |
| G2-322-medium-dense-mkldnn | paddle | v6 medium | on | 3.2.2 | 5.06 | 8.3 | 1268 | 0.9973（與 off 逐字同） | 6.9x |
| G2-322-small-dense-mkldnn | paddle | v6 small | on | 3.2.2 | 1.68 | 5.4 | 821 | 0.9836 vs medium | 20.7x |
| G3-rapidocr-default | rapidocr | v6 small onnx | — | — | **0.90** | **1.2** | **320** | **0.9987** | **38.7x** |
| G3-rapidocr-cht | rapidocr | v6 small + chinese_cht rec | — | — | 0.99 | 1.2 | 268 | — | 35.2x |
| G4-rapidocr-threads2 | rapidocr | v6 small onnx | — | — | 1.32 | 1.6 | 261 | — | 26.4x |
| G4-rapidocr-dpi150 | rapidocr | v6 small onnx | — | — | 0.77 | 1.0 | 236 | 0.9987 | 45.2x |

## Results（synthetic 稀疏頁）

| label | engine | 模型 | mkldnn | paddle 版 | s/頁 med | gt_recall | trad_ok |
|---|---|---|---|---|---|---|---|
| G0-medium-synth | paddle | v6 medium | off | 3.3.0 | 23.09 | 3/3 | True |
| G0-small-mkldnn_off | paddle | v6 small | off | 3.3.0 | 4.97 | 3/3 | True |
| G1-331-mkldnn_on | paddle | v6 small | on | 3.3.1 | **crash**（oneDNN bug 未修） | — | — |
| G2-322-mkldnn_on-smoke | paddle | v6 small | on | 3.2.2 | 0.81 | 3/3 | True |

原始 CSV：session scratchpad `bench\bench_results.csv`（含完整欄位與時間戳）。
