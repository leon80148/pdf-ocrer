# pdf-ocrer

Primary user guide: [繁體中文 README](README.zh-TW.md)

pdf-ocrer batch-converts scanned PDFs in a folder into searchable dual-layer PDFs.
It keeps the original page image, adds an invisible text layer with PP-OCRv6 via
PaddleOCR 3.7, and can rename each output file with any OpenAI-compatible LLM
using the editable `naming_prompt.txt` prompt.

Original PDFs are never modified. Outputs are written to an `OCR輸出` subfolder
with a timestamped CSV audit table encoded as `utf-8-sig`, so Excel opens it
cleanly.

## Status

This is a Windows-first Python desktop/CLI tool. It should work on other
platforms supported by the dependencies, but the examples and packaging are
currently optimized for Windows.

The project is source-install first until a package is published. Do not assume
`pip install pdf-ocrer` is available from PyPI yet.

## What It Does

- Converts every PDF directly inside a selected folder, non-recursively.
- Adds an invisible searchable text layer to scanned pages.
- Preserves pages that already have a text layer.
- Names output PDFs from OCR text using `naming_prompt.txt`.
- Supports local and cloud OpenAI-compatible LLM endpoints.
- Records every processed, skipped, or failed file in a CSV audit table.
- Skips password-protected PDFs and records them in the CSV instead of stopping
  the batch.

Non-goals for v1: layout reconstruction, Markdown/table extraction, watermark
removal, image cleanup, and verified GPU support. The config has an `ocr.device`
field, but GPU execution is not tested.

## Requirements

- Python 3.11 or newer. Python 3.12 is recommended.
- Windows: Microsoft Visual C++ Redistributable 2019 or newer.
- First OCR run downloads PP-OCRv6 models, about 100 MB, to
  `~/.paddlex/official_models`.

For offline machines, copy the whole `official_models` folder from a machine that
has already run OCR to the same path on the offline machine.

## Install From Source

```powershell
git clone https://github.com/leon80148/pdf-ocrer.git
cd pdf-ocrer
py -3.12 -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e ".[paddle-cpu]"
```

The `paddle-cpu` extra installs `paddlepaddle==3.3.*`, which is the tested CPU
runtime for PaddleOCR. After a published package exists, the equivalent package
install will be:

```powershell
python -m pip install "pdf-ocrer[paddle-cpu]"
```

For contributor setup, tests, style, and native provider guidance, read
[CONTRIBUTING.md](CONTRIBUTING.md).

## Usage

Start the GUI:

```powershell
pdf-ocrer
```

Run a CLI batch:

```powershell
pdf-ocrer "C:\path\to\pdf-folder"
```

Use the explicit GUI entry point if your launcher needs it:

```powershell
pdf-ocrer-gui
```

CLI flags:

```text
pdf-ocrer <folder>
  --config PATH   Use a config file. Default is config.toml in the current directory.
  --no-llm        Force fallback naming, regardless of config.
  --dpi N         Override OCR render DPI.
  --version       Print the version.
```

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | All files completed successfully, or GUI/version command completed. |
| 1 | Configuration error or at least one file failed. |
| 2 | Folder is missing, not a directory, or contains no PDFs. |

Progress is printed to stdout, for example:

```text
[3/12] scan.pdf 第 5/20 頁
```

The final summary table includes the CSV path.

## Output

Given a folder such as:

```text
C:\Scans
```

pdf-ocrer writes:

```text
C:\Scans\OCR輸出\
  對照表_YYYYMMDD_HHMMSS.csv
  <renamed searchable PDFs>.pdf
```

CSV columns:

```text
原檔名,新檔名,狀態,總頁數,OCR頁數,命名來源,備註
```

The CSV is appended and flushed after each file, so completed rows remain even if
a long batch is interrupted.

## Configuration

Copy the example file and edit it:

```powershell
copy config.example.toml config.toml
notepad config.toml
```

Important settings:

| Section | Setting | Purpose |
|---|---|---|
| `[ocr]` | `dpi = 200` | OCR render resolution. Valid range is 72 to 600. |
| `[ocr]` | `enable_mkldnn = false` | Required workaround for the PaddlePaddle 3.3.0 oneDNN bug. |
| `[ocr]` | `det_model_name`, `rec_model_name` | Use small or tiny PP-OCRv6 models for speed tuning. |
| `[output]` | `subdir_name = "OCR輸出"` | Output folder name. |
| `[naming]` | `prompt_file = "naming_prompt.txt"` | User-editable prompt for output names. |
| `[naming]` | `max_chars_to_llm = 3000` | Maximum OCR text characters sent to the naming LLM. |
| `[llm]` | `provider = "openai_compatible"` | Default generic provider. |
| `[llm]` | `provider = "none"` | Disable LLM naming. Files use original name plus `_OCR`. |
| `[debug]` | `visible_text = true` | Render the text layer in red for alignment checks. |

See [config.example.toml](config.example.toml) for the full documented template.

## LLM Naming

The default provider is `openai_compatible`. It uses the OpenAI Chat Completions
shape, so the same code path works with local servers and compatible cloud
providers.

The default endpoint is Ollama:

```toml
[llm]
provider = "openai_compatible"
base_url = "http://localhost:11434/v1"
model = "qwen3:8b"
```

Provider examples:

| Service | Example `base_url` | Notes |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | Default. Local and privacy-friendly. |
| OpenAI | `https://api.openai.com/v1` | Set `PDF_OCRER_API_KEY` or `llm.api_key`. |
| Gemini OpenAI-compatible endpoint | `https://generativelanguage.googleapis.com/v1beta/openai` | Uses the OpenAI-compatible endpoint. |
| Anthropic compatibility endpoint | `https://api.anthropic.com/v1` | Uses the compatibility endpoint shown in config. |
| LM Studio | `http://localhost:1234/v1` | Point to the local server's OpenAI-compatible API. |
| vLLM | `http://localhost:8000/v1` | Point to your vLLM server. |
| Groq | `https://api.groq.com/openai/v1` | Set an API key. |
| OpenRouter | `https://openrouter.ai/api/v1` | Set an API key. |

API keys can be set in config:

```toml
[llm]
api_key = "..."
```

or through the environment:

```powershell
$env:PDF_OCRER_API_KEY = "..."
```

To disable LLM naming:

```toml
[llm]
provider = "none"
```

or run:

```powershell
pdf-ocrer "C:\Scans" --no-llm
```

Native providers can be added through `register_provider`. See
[Adding an LLM provider](CONTRIBUTING.md#adding-an-llm-provider).

## Privacy

With local Ollama, OCR text stays on the machine.

With cloud providers, pdf-ocrer sends only the naming sample, limited by
`naming.max_chars_to_llm` and `naming.max_pages_to_llm`. The default character
limit is 3000. The original PDF file is not uploaded by pdf-ocrer for naming.

This matters for clinic and medical-administration workflows. Use local Ollama
when documents cannot leave the machine.

## Performance

Measured baseline on CPU:

- PP-OCRv6 medium, 200 DPI, `enable_mkldnn=false`: about 22 seconds per page.

`enable_mkldnn=false` is the default because PaddlePaddle 3.3.0 currently hits a
oneDNN runtime error when MKLDNN is enabled. See
[docs/specs/paddleocr-api-facts.md §2](docs/specs/paddleocr-api-facts.md) for
the measured failure and workaround.

For speed, try smaller PP-OCRv6 models in `config.toml`:

```toml
[ocr]
det_model_name = "PP-OCRv6_small_det"
rec_model_name = "PP-OCRv6_small_rec"
```

Tiny models may be faster if your PaddleOCR installation provides them, with the
usual accuracy tradeoff.

## Known Limitations

- Pages whose content is sideways while the PDF is also displayed sideways,
  without `/Rotate` compensation, are searchable but highlight orientation may be
  off.
- Skewed text lines are searchable, but highlights can be slightly offset because
  v1 inserts an axis-aligned text layer.
- Password-protected PDFs are skipped and recorded in the CSV.
- The batch is non-recursive. PDFs in subfolders are not scanned.
- GPU support is not claimed. The `ocr.device` config field exists, but CPU is
  the tested path.

## Debug Alignment

Set:

```toml
[debug]
visible_text = true
```

The generated text layer becomes visible red text. Use this only for alignment
checks, not normal output.

## Development

- Entry points: `pdf-ocrer` and `pdf-ocrer-gui`.
- Project metadata and extras live in [pyproject.toml](pyproject.toml).
- Design details live in [docs/specs](docs/specs).
- Contributor workflow lives in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
