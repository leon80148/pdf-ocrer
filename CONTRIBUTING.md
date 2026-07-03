# Contributing to pdf-ocrer

Thanks for your interest! This document explains how to set up a development
environment and the conventions this project follows.

## Development setup

```bash
git clone https://github.com/leon80148/pdf-ocrer.git
cd pdf-ocrer
python -m venv .venv               # Python 3.11+ (3.12 recommended)
.venv\Scripts\activate             # Windows
pip install -e .[paddle-cpu,dev]   # paddle-cpu pulls paddlepaddle (large download)
```

Contributors who only work on non-OCR modules (config, LLM providers, naming,
pipeline) can skip the heavy OCR stack:

```bash
pip install -e .[dev] pymupdf openai
```

Unit tests never import PaddlePaddle, so they run fine without it.

## Test layers

| Layer | Command | Requirements |
|---|---|---|
| Unit (default) | `pytest` | no network, no Paddle, no GUI |
| Integration | `pytest -m integration` | PaddleOCR models (downloaded on first run) |

Rules for unit tests:

- Never touch the network.
- Never import `paddleocr` / `paddlepaddle` (module-level imports of these in
  `src/` are lazy for this reason — keep it that way).
- Never open a Tk window (GUI tests only construct widgets when a display is
  available and must skip otherwise).

## Code style

- `ruff check .` must pass (line length 100, target py311).
- Type hints on all public functions.
- Docstrings and code comments in English; user-facing strings in
  Traditional Chinese (this tool ships with a zh-TW UI).
- Follow the interfaces documented in `docs/specs/` — they are the design
  contract. Propose spec changes in the PR description when needed.

## Adding an LLM provider

The naming step talks to LLMs through a tiny protocol
(`llm_providers.LLMClient`: one `complete(prompt) -> str` method). The default
`openai_compatible` provider covers any OpenAI-compatible endpoint (OpenAI,
Ollama, LM Studio, vLLM, Groq, OpenRouter, Gemini/Anthropic compatibility
endpoints).

To add a native provider:

1. Create a class with a `complete(self, prompt: str) -> str` method that
   raises `LLMError` on failure.
2. Register it: `@register_provider("your_name")`.
3. Keep its SDK an optional dependency (guard the import inside the factory).
4. Add unit tests with a fake transport — no real network calls.

## Pull requests

- Write tests first (this repo is developed TDD-style); every behavior change
  needs a test.
- Keep PRs focused on one concern.
- Run `pytest` and `ruff check .` before pushing.
