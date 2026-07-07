# pdf-ocrer

Primary user guide: [繁體中文 README](README.zh-TW.md)

pdf-ocrer batch-converts scanned PDFs and supported image files in a folder into
searchable dual-layer PDFs. It keeps the original page image, adds an invisible
text layer with PP-OCRv6 via the selected OCR engine, and can rename each output
file with any OpenAI-compatible LLM using the editable `naming_prompt.txt`
prompt.

Original PDFs are never modified. Outputs are written to an `OCR輸出` subfolder.
When a run processes new files, it also writes a timestamped CSV audit table
encoded as `utf-8-sig`, so Excel opens it cleanly.

## Status

This is a Windows-first Python desktop/CLI tool. It should work on other
platforms supported by the dependencies, but the examples and packaging are
currently optimized for Windows.

The project is source-install first until a package is published. Do not assume
`pip install pdf-ocrer` is available from PyPI yet.

## What It Does

- Converts every PDF, JPG, PNG, or TIFF directly inside a selected folder by
  default, with optional recursive subfolder scanning.
- Adds an invisible searchable text layer to scanned pages.
- Preserves pages that already have a text layer.
- Names output PDFs from OCR text using `naming_prompt.txt`.
- Supports local and cloud OpenAI-compatible LLM endpoints.
- Records every newly processed, encrypted-skipped, or failed file in a CSV
  audit table.
- Skips already completed files on repeat runs when the manifest still matches.
- Skips password-protected PDFs and records them in the CSV instead of stopping
  the batch.

Non-goals for v1: layout reconstruction, Markdown/table extraction, watermark
removal, image cleanup, and verified GPU support. The config has an `ocr.device`
field, but GPU execution is not tested.

## Requirements

- Python 3.11 or newer. Python 3.12 is recommended.
- Windows: Microsoft Visual C++ Redistributable 2019 or newer.
- PaddleOCR first run downloads PP-OCRv6 models, about 100 MB, to
  `~/.paddlex/official_models`.
- RapidOCR ships its default ONNX model in the wheel and is the recommended CPU
  engine for new installs.

For offline machines, copy the whole `official_models` folder from a machine that
has already run OCR to the same path on the offline machine.

## Install From Source

```powershell
git clone https://github.com/leon80148/pdf-ocrer.git
cd pdf-ocrer
py -3.12 -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
python -m pip install -e ".[rapidocr]"
```

Then set the OCR engine in `config.toml`:

```toml
[ocr]
engine = "rapidocr"
```

The future package install form is:

```powershell
python -m pip install "pdf-ocrer[rapidocr]"
```

To use PaddleOCR instead, install the Paddle CPU runtime:

```powershell
python -m pip install -e ".[paddle-cpu]"
```

The `paddle-cpu` extra installs `paddlepaddle==3.2.*`, the tested CPU runtime
for PaddleOCR with working oneDNN/MKLDNN. After a published package exists, the
equivalent package install will be:

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

Watch a scanner drop folder continuously:

```powershell
pdf-ocrer "C:\path\to\pdf-folder" --watch
```

Use the explicit GUI entry point if your launcher needs it:

```powershell
pdf-ocrer-gui
```

The GUI uses CustomTkinter for a modern flat desktop look. It shows a per-file
status table with `原檔名`, `狀態`, `新檔名`, and `OCR頁數` columns that update as
each file is processed. You can choose a folder with the button or drag a folder
onto the window; if `tkinterdnd2` is unavailable, drag-and-drop is disabled
gracefully and normal folder selection still works. The theme switcher supports
system, light, and dark appearances, defaulting from `[gui] appearance`. The
settings window exposes OCR engine, DPI, confidence threshold, model size,
parallel worker count, LLM naming, and common LLM connection fields. The
`完成後開啟對照表` checkbox is on by default and opens the CSV audit table when
the batch completes. `全部重新處理` ignores the incremental manifest for that run.
Enable `監看模式` to keep watching the selected folder after clicking Start; in
watch mode the force checkbox is ignored.

CLI flags:

```text
pdf-ocrer <folder>
  --config PATH   Use a config file. Default is config.toml in the current directory.
  --no-llm        Force fallback naming, regardless of config.
  --dpi N         Override OCR render DPI.
  --engine NAME   Override OCR engine for this run: paddle or rapidocr.
  --workers N     Override parallel file workers. 0=auto, 1=sequential.
  --recursive     Scan subfolders and mirror their structure under the output folder.
  --force         Ignore the incremental manifest and reprocess every input.
  --watch         Keep polling the folder and process stable new files.
  --version       Print the version.
```

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | All files completed successfully, or GUI/version command completed. |
| 1 | Configuration error or at least one file failed. |
| 2 | Folder is missing, not a directory, or contains no supported PDF/image inputs. |

Progress is printed to stdout, for example:

```text
[3/12] scan.pdf 第 5/20 頁
```

The final summary table includes the CSV path when a new CSV was written.

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

By default, only supported inputs directly inside `C:\Scans` are processed:
PDF plus JPG/JPEG/PNG/TIF/TIFF images. Image inputs are converted through MuPDF
and always written as searchable `.pdf` outputs; multi-page TIFF files keep
their page count. If an image has no DPI metadata, MuPDF assumes 96dpi.

Enable recursive scanning with:

```powershell
pdf-ocrer "C:\Scans" --recursive
```

or:

```toml
[input]
recursive = true
```

When recursive scanning is enabled, output folders mirror the input subfolder
structure. For example, `C:\Scans\2026\scan.pdf` writes under
`C:\Scans\OCR輸出\2026\...`, and the CSV records `2026/scan.pdf` as the source
path. Any folder named the configured output subfolder, at any depth, is skipped.

CSV columns:

```text
原檔名,新檔名,狀態,總頁數,OCR頁數,命名來源,備註
```

The CSV is appended and flushed after each file, so completed rows remain even if
a long batch is interrupted.

Incremental processing is enabled by default. pdf-ocrer stores
`.pdf_ocrer_manifest.json` in the output folder and skips inputs whose size and
mtime match a previous successful output, or a previous encrypted-skip record.
Skipped-done files show as `已處理-跳過` in the CLI/GUI summary and are not written
to a new CSV. If a whole repeat run has no newly processed files, no CSV is
created. Use `--force` for one run, or disable the behavior with:

```toml
[output]
incremental = false
```

## Watch Mode

Watch mode is meant for scanner drop folders: leave pdf-ocrer running on a
folder, and newly scanned PDFs or images are processed automatically after they
finish landing on disk.

CLI:

```powershell
pdf-ocrer "C:\Scans" --watch
```

GUI: choose the folder, enable `監看模式`, then press Start. The status line shows
the current polling cycle and cumulative processed count. Press Stop Watch to
end the loop; watch mode writes progress to the log instead of showing a dialog
after every cycle.

Behavior:

- The folder is polled every `[watch] poll_seconds` seconds. The default is 5.
- A file is considered ready only after two consecutive polls report the same
  `(size, mtime)` snapshot, which avoids processing half-written scanner output.
- Completed files are skipped through the incremental manifest after restart.
- Failed files are retried up to `[watch] max_retries` times for the same source
  snapshot, then frozen until the source file changes.
- Watch mode requires `[output] incremental = true` and cannot be combined with
  `--force`.
- Watch mode processes each polling cycle with a single worker; `workers > 1`
  or auto-selected parallel workers are ignored to avoid rebuilding worker
  processes every cycle.
- Do not run multiple watch processes on the same folder at the same time.

## Configuration

Copy the example file and edit it:

```powershell
copy config.example.toml config.toml
notepad config.toml
```

Important settings:

| Section | Setting | Purpose |
|---|---|---|
| `[ocr]` | `engine = "paddle"` | OCR engine. Install `pdf-ocrer[rapidocr]` and set `"rapidocr"` for the recommended CPU path. |
| `[ocr]` | `dpi = 200` | OCR render resolution. Valid range is 72 to 600. |
| `[ocr]` | `enable_mkldnn = false` | Keep false with PaddlePaddle 3.3.x; PaddlePaddle 3.2.x can enable it for speed. |
| `[ocr]` | `det_model_name`, `rec_model_name` | Use small or tiny PP-OCRv6 models for speed tuning. |
| `[output]` | `subdir_name = "OCR輸出"` | Output folder name. |
| `[output]` | `incremental = true` | Skip manifest-matched completed files on repeat runs. Use `--force` for one run. |
| `[input]` | `recursive = false` | Set true to scan subfolders and mirror them under the output folder. |
| `[input]` | `image_extensions = ["jpg", "jpeg", "png", "tif", "tiff"]` | Image extensions accepted as inputs. Set `[]` to process PDFs only. |
| `[performance]` | `workers = 1` | Parallel file workers. `1` is sequential and default; `0` auto-selects up to 3; `2` to `8` force a count. |
| `[watch]` | `poll_seconds = 5.0` | Poll interval for watch mode. |
| `[watch]` | `max_retries = 3` | Failed-file retries before freezing the same source snapshot. |
| `[naming]` | `prompt_file = "naming_prompt.txt"` | User-editable prompt for output names. |
| `[naming]` | `max_chars_to_llm = 3000` | Maximum OCR text characters sent to the naming LLM. |
| `[llm]` | `provider = "openai_compatible"` | Default generic provider. |
| `[llm]` | `provider = "none"` | Disable LLM naming. Files use original name plus `_OCR`. |
| `[gui]` | `appearance = "system"` | GUI theme: `"system"`, `"light"`, or `"dark"`. |
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

Dense two-page clinic-document benchmark on an AMD Ryzen 5 8600G CPU:

| Engine / config | OCR speed | Notes |
|---|---:|---|
| PaddleOCR medium, PaddlePaddle 3.3.x, MKLDNN off | 34.81 s/page | Current compatible baseline. |
| RapidOCR default | 0.90 s/page | Recommended; about 38.7x faster than the baseline. |
| PaddleOCR medium, PaddlePaddle 3.2.2, MKLDNN on | 5.06 s/page | About 6.9x faster; same text output as MKLDNN off in the benchmark. |

See [docs/specs/benchmark-results.md](docs/specs/benchmark-results.md) for the
full table, accuracy notes, cold-start time, and memory results.

Recommended CPU setup:

```powershell
python -m pip install "pdf-ocrer[rapidocr]"
```

```toml
[ocr]
engine = "rapidocr"
```

PaddleOCR remains available for compatibility. PaddlePaddle 3.3.x has a known
oneDNN/MKLDNN runtime bug, so keep `enable_mkldnn=false` with 3.3.x. The
`paddle-cpu` extra now pins PaddlePaddle 3.2.x, where MKLDNN can be enabled:

```toml
[ocr]
enable_mkldnn = true
```

For additional Paddle speed tuning, try smaller PP-OCRv6 models:

```toml
[ocr]
det_model_name = "PP-OCRv6_small_det"
rec_model_name = "PP-OCRv6_small_rec"
```

Tiny models may be faster if your PaddleOCR installation provides them, with the
usual accuracy tradeoff.

Parallel processing is configured with:

```toml
[performance]
workers = 1
```

The default is sequential because each worker loads its own OCR model. As a
rough memory budget, RapidOCR is about 0.7 GB per worker and PaddleOCR is about
1.3 to 2.5 GB per worker. Use parallel workers mainly for batches of six or more
files, and start with `workers = 2` on typical clinic PCs. `workers = 0`
auto-selects about one worker per four CPU cores, capped at 3.
If you manually set `cpu_threads`, every parallel worker uses that value
(total threads are roughly `workers × cpu_threads`), so keep `cpu_threads = 0`
in parallel mode to let the app distribute threads automatically.

## Known Limitations

- Sideways scans without a `/Rotate` page marker are searchable, but highlight
  orientation may be off.
- Skewed text lines are searchable, but highlights can be slightly offset because
  v1 inserts an axis-aligned text layer.
- Password-protected PDFs are skipped and recorded in the CSV.
- Images without DPI metadata use MuPDF's 96dpi page-size assumption.
- GPU support is not claimed. The `ocr.device` config field exists, but CPU is
  the tested path.
- PaddlePaddle 3.3.x cannot use the MKLDNN/oneDNN path reliably; use the
  `paddle-cpu` extra's 3.2.x pin before enabling `ocr.enable_mkldnn`.

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
