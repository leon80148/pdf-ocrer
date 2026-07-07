# pdf-ocrer

English developer README: [README.md](README.md)

pdf-ocrer 是給診所行政人員使用的批次 OCR 工具。你選一個資料夾，它會把裡面的掃描
PDF 與支援的圖片檔轉成可搜尋的雙層 PDF：原本的影像保留不動，另外加上一層看不見的文字。

它也可以讀取 OCR 文字，請本機或雲端的 OpenAI 相容 LLM 依照
`naming_prompt.txt` 自動命名輸出檔。原始檔案絕不修改，所有結果都放在
`OCR輸出` 子資料夾；本次有新處理檔案時，會產生 Excel 可直接開啟的 CSV 對照表。

## 你會得到什麼

- 掃描 PDF、JPG、PNG、TIFF 變成可以 Ctrl+F 搜尋的 PDF。
- 預設只處理所選資料夾第一層；也可啟用遞迴掃描子資料夾。
- 輸出檔依文件內容自動命名。
- 原始 PDF 留在原資料夾，不會被覆蓋。
- 本次有新處理檔案時，會產生 `對照表_YYYYMMDD_HHMMSS.csv`，方便回查原檔名、新檔名和處理狀態。
- 重複執行同一資料夾時，預設會跳過 manifest 已記錄且輸出仍存在的完成檔。
- 加密 PDF 不會讓整批中斷，會被略過並記錄在 CSV。

## 安裝前準備

- Python 3.11 以上，建議 Python 3.12。
- Windows 建議安裝 Microsoft Visual C++ Redistributable 2019 以上。
- 第一次執行 OCR 會下載 PP-OCRv6 模型，大約 100 MB，位置是
  `~/.paddlex/official_models`。

如果診所電腦不能上網，可以先在可上網的電腦跑過一次 OCR，再把整個
`official_models` 資料夾複製到離線電腦的相同位置。

## 安裝方式

目前請從原始碼資料夾安裝：

```powershell
git clone https://github.com/leon80148/pdf-ocrer.git
cd pdf-ocrer
py -3.12 -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e ".[rapidocr]"
```

然後在 `config.toml` 設定推薦的 OCR 引擎：

```toml
[ocr]
engine = "rapidocr"
```

未來若已發佈成套件，RapidOCR 推薦安裝指令會是：

```powershell
python -m pip install "pdf-ocrer[rapidocr]"
```

如果要使用 PaddleOCR 路徑，請安裝 Paddle CPU runtime：

```powershell
python -m pip install -e ".[paddle-cpu]"
```

`paddle-cpu` 會安裝 `paddlepaddle==3.2.*`，這是目前測試過且 oneDNN/MKLDNN
可正常加速的 PaddleOCR CPU 環境。未來若已發佈成套件，完整安裝指令會是：

```powershell
python -m pip install "pdf-ocrer[paddle-cpu]"
```

在那之前，請以上面的原始碼安裝方式為準。

## 最簡單的使用方式

1. 把要處理的 PDF 或圖片放在同一個資料夾。
2. 開啟命令列，啟動圖形介面：

   ```powershell
   pdf-ocrer
   ```

3. 選擇資料夾，按開始。
4. 完成後打開原資料夾裡的 `OCR輸出`。
5. 檢查新的 PDF 和 `對照表_YYYYMMDD_HHMMSS.csv`。

也可以直接用命令列批次處理：

```powershell
pdf-ocrer "C:\Scans"
```

圖形介面現在使用 CustomTkinter，外觀是比較現代的扁平桌面介面。處理時會顯示檔案狀態
表，欄位包含 `原檔名`、`狀態`、`新檔名`、`OCR頁數`，並依每個檔案即時更新。你可以按
按鈕選資料夾，也可以把資料夾拖放到視窗上；如果系統無法使用 `tkinterdnd2`，拖放功能
會自動關閉，仍可用一般選取資料夾方式操作。介面可切換系統、淺色、深色主題，預設值
來自 `config.toml` 的 `[gui] appearance`。設定視窗可調整 OCR 引擎、DPI、最低信心分數、
模型大小、同時處理檔案數、LLM 命名與常用 LLM 連線欄位。`完成後開啟對照表` 預設勾選，批次完成後會
自動開啟 CSV 對照表；`全部重新處理` 可在該次執行忽略增量記錄。

## 命令列選項

```text
pdf-ocrer                     # 不加資料夾，啟動 GUI
pdf-ocrer <folder>            # 批次處理資料夾裡的 PDF/圖片
  --config PATH               # 指定 config.toml
  --no-llm                    # 不使用 LLM 命名
  --dpi N                     # 指定 OCR 解析度
  --engine NAME               # 本次執行覆寫 OCR 引擎：paddle 或 rapidocr
  --workers N                 # 本次執行覆寫同時處理檔案數；0=自動，1=循序
  --recursive                 # 掃描子資料夾，並在輸出資料夾鏡像原結構
  --force                     # 忽略增量記錄，全部重新處理
  --version                   # 顯示版本
```

退出碼：

| 代碼 | 意義 |
|---:|---|
| 0 | 全部成功，或 GUI / 版本指令正常結束。 |
| 1 | 設定錯誤，或至少一個檔案失敗。 |
| 2 | 資料夾不存在、不是資料夾，或裡面沒有支援的 PDF/圖片。 |

處理中會顯示進度，例如：

```text
[3/12] scan.pdf 第 5/20 頁
```

## 輸出位置

假設原資料夾是：

```text
C:\Scans
```

輸出會在：

```text
C:\Scans\OCR輸出\
  對照表_YYYYMMDD_HHMMSS.csv
  <重新命名後的可搜尋 PDF>.pdf
```

預設只掃描 `C:\Scans` 第一層的支援輸入：PDF 與 JPG/JPEG/PNG/TIF/TIFF 圖片。圖片會先
透過 MuPDF 轉成 PDF，輸出一律是可搜尋的 `.pdf`；多頁 TIFF 會保留頁數。如果圖片沒有
DPI 標記，MuPDF 會假設 96dpi。

若要包含子資料夾，可以執行：

```powershell
pdf-ocrer "C:\Scans" --recursive
```

或在 `config.toml` 設定：

```toml
[input]
recursive = true
image_extensions = ["jpg", "jpeg", "png", "tif", "tiff"]  # 設為 [] 可只處理 PDF
```

啟用遞迴後，輸出會鏡像原本的子資料夾結構。例如
`C:\Scans\2026\scan.pdf` 會輸出到 `C:\Scans\OCR輸出\2026\...`，CSV 的原檔名
欄會記錄 `2026/scan.pdf`。任何名稱等於輸出資料夾名稱的資料夾，不論在第幾層，都會被略過。

CSV 使用 `utf-8-sig` 編碼，Excel 開啟比較不會亂碼。每處理完一個檔案就會寫入一列，
所以中途取消或當機時，已完成的紀錄仍會保留。

增量處理預設開啟。pdf-ocrer 會在輸出資料夾保存 `.pdf_ocrer_manifest.json`，來源檔大小
與修改時間符合先前成功輸出，或先前已判定為加密跳過時，重跑會顯示
`已處理-跳過`，不會把該檔寫進新的 CSV。若整批都沒有新處理檔案，本次不會產生 CSV。
要單次全部重做可用 `--force` 或 GUI 的 `全部重新處理`；要關閉增量可在設定檔加入：

```toml
[output]
incremental = false
```

## 自動命名

預設會使用 `naming_prompt.txt` 當命名規則。你可以直接打開這個檔案，修改想要的檔名
格式，例如日期、文件類型、病患姓名或發文機關。

如果 LLM 停用、斷線、逾時或回傳不可用文字，檔名會改用原檔名加上 `_OCR`。

停用 LLM 命名有兩種方式：

```powershell
pdf-ocrer "C:\Scans" --no-llm
```

或在 `config.toml` 設定：

```toml
[llm]
provider = "none"
```

## 隱私建議

如果使用本機 Ollama，OCR 文字不會離開電腦。這是診所、醫療行政、病歷相關文件比較
安全的做法。

如果使用雲端 LLM，pdf-ocrer 只會送出前面一小段 OCR 文字供命名使用，不會上傳整份
PDF。預設最多送出 `naming.max_chars_to_llm = 3000` 個字元，也會受到
`naming.max_pages_to_llm` 限制。

## LLM 設定

預設 provider 是 `openai_compatible`，可連接任何 OpenAI 相容 API。API key 可以寫在
`config.toml`：

```toml
[llm]
api_key = "..."
```

也可以使用環境變數：

```powershell
$env:PDF_OCRER_API_KEY = "..."
```

常見 `base_url` 範例：

| 服務 | `base_url` 範例 | 備註 |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | 預設值，本機執行，資料不離開電腦。 |
| OpenAI | `https://api.openai.com/v1` | 需要 API key。 |
| Gemini OpenAI 相容端點 | `https://generativelanguage.googleapis.com/v1beta/openai` | 使用相容端點。 |
| Anthropic 相容端點 | `https://api.anthropic.com/v1` | 使用設定範本中的相容端點。 |
| LM Studio | `http://localhost:1234/v1` | 指向本機 LM Studio server。 |
| vLLM | `http://localhost:8000/v1` | 指向你的 vLLM server。 |
| Groq | `https://api.groq.com/openai/v1` | 需要 API key。 |
| OpenRouter | `https://openrouter.ai/api/v1` | 需要 API key。 |

完整設定範本請看 [config.example.toml](config.example.toml)。

OCR 引擎可在設定檔指定。預設仍是 `paddle`，但 CPU 使用建議安裝 RapidOCR extra 並改用
`rapidocr`：

```toml
[ocr]
engine = "rapidocr"
```

圖形介面主題可在設定檔中調整：

```toml
[gui]
appearance = "system"  # 可用 "system"、"light"、"dark"
```

增量處理可在設定檔中調整：

```toml
[output]
incremental = true  # 預設 true；設 false 則每次都重新處理
```

子資料夾掃描可在設定檔中調整：

```toml
[input]
recursive = true
image_extensions = ["jpg", "jpeg", "png", "tif", "tiff"]  # 設為 [] 可只處理 PDF
```

平行處理可在設定檔中調整：

```toml
[performance]
workers = 1  # 預設循序；0=自動（最多 3）；2 到 8=指定 worker 數
```

每個 worker 會各載一份 OCR 模型，RapidOCR 約需 0.7 GB 記憶體，PaddleOCR 約需 1.3 到
2.5 GB。建議至少 6 個檔案以上再開平行，診所一般電腦可先從 `workers = 2` 試起。
若手動設定 `cpu_threads`，平行模式下每個 worker 都沿用該值（總執行緒約為
`workers × cpu_threads`），建議平行時保留 `cpu_threads = 0` 讓程式自動分配。

## 速度與模型

AMD Ryzen 5 8600G CPU 上的 dense 2 頁診所文件實測：

| 引擎 / 設定 | OCR 速度 | 備註 |
|---|---:|---|
| PaddleOCR medium、PaddlePaddle 3.3.x、MKLDNN off | 34.81 秒/頁 | 現行相容基準。 |
| RapidOCR 預設 | 0.90 秒/頁 | 推薦；約比基準快 38.7x。 |
| PaddleOCR medium、PaddlePaddle 3.2.2、MKLDNN on | 5.06 秒/頁 | 約快 6.9x；實測輸出與 MKLDNN off 逐字相同。 |

完整數字、精度、冷啟動與記憶體結果請看
[docs/specs/benchmark-results.md](docs/specs/benchmark-results.md)。

建議 CPU 安裝與設定：

```powershell
python -m pip install "pdf-ocrer[rapidocr]"
```

```toml
[ocr]
engine = "rapidocr"
```

PaddleOCR 仍可用於相容路徑。PaddlePaddle 3.3.x 的 oneDNN/MKLDNN 路徑有已知錯誤，
因此使用 3.3.x 時請維持 `enable_mkldnn=false`。`paddle-cpu` extra 目前釘到
PaddlePaddle 3.2.x，可開啟 MKLDNN 加速：

```toml
[ocr]
enable_mkldnn = true
```

如果使用 PaddleOCR 且速度比準確度更重要，可以在 `config.toml` 嘗試小模型：

```toml
[ocr]
det_model_name = "PP-OCRv6_small_det"
rec_model_name = "PP-OCRv6_small_rec"
```

若你的 PaddleOCR 安裝提供 tiny 模型，也可以用同樣欄位調整；速度可能較快，但辨識率
可能下降。

## 已知限制

- 如果掃描影像本身是橫躺的，且 PDF 沒有 `/Rotate` 標記可補償，仍可搜尋，但反白方向
  可能不準。
- 歪斜文字行仍可搜尋，但 v1 使用水平文字層，反白位置可能略有偏移。
- 需要密碼的 PDF 會被略過，並記錄在 CSV。
- 沒有 DPI 標記的圖片會使用 MuPDF 的 96dpi 頁面尺寸假設。
- `ocr.device` 設定欄位存在，但目前只宣稱 CPU 路徑已測試，不宣稱 GPU 支援。
- PaddlePaddle 3.3.x 不能可靠使用 MKLDNN/oneDNN；要開啟 `ocr.enable_mkldnn` 前，
  請使用 `paddle-cpu` extra 釘住的 3.2.x。

## 對位除錯

如果想檢查文字層是否對齊，可以在 `config.toml` 開啟：

```toml
[debug]
visible_text = true
```

這會把文字層顯示成紅字。確認完請關掉，正常輸出應使用隱形文字層。

## 開發與貢獻

開發環境、測試方式、程式風格、如何新增 LLM provider，請看
[CONTRIBUTING.md](CONTRIBUTING.md)。這份 README 只保留使用者需要的操作步驟。

## 授權

MIT。詳見 [LICENSE](LICENSE)。
