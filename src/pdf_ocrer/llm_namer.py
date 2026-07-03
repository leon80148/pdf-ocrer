from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from string import Template

from pdf_ocrer.config import AppConfig
from pdf_ocrer.llm_providers import LLMClient, LLMError


_THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
_ILLEGAL_RE = re.compile(r'[\x00-\x1f\x7f\\/:*?"<>|]')
_WHITESPACE_RE = re.compile(r"\s+")
_EDGE_MARKERS = " \t\r\n'\"`*“”‘’「」『』"
_RESERVED_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def build_prompt(template: str, text: str, original_name: str, today: str) -> str:
    return Template(template).safe_substitute(
        text=text,
        original_name=original_name,
        today=today,
    )


def sanitize_filename(raw: str, max_length: int) -> str | None:
    without_think = _THINK_RE.sub("", raw)

    candidate = ""
    for line in without_think.splitlines():
        if line.strip():
            candidate = line.strip()
            break

    candidate = _strip_edge_markdown(candidate)
    candidate = re.sub(r"\.pdf\Z", "", candidate, flags=re.IGNORECASE)
    candidate = _ILLEGAL_RE.sub("", candidate)
    candidate = _WHITESPACE_RE.sub(" ", candidate)
    candidate = candidate.rstrip(" .")

    if _is_reserved_device_name(candidate):
        candidate = f"_{candidate}"

    if len(candidate) > max_length:
        candidate = candidate[:max_length].rstrip(" .")

    return candidate or None


def resolve_collision(out_dir: Path, stem: str, used: set[str]) -> str:
    candidate = stem
    suffix = 2
    while _stem_unavailable(out_dir, candidate, used):
        candidate = f"{stem}_{suffix}"
        suffix += 1

    used.add(candidate.casefold())
    return candidate


def suggest_filename(
    text: str,
    original_stem: str,
    cfg: AppConfig,
    client: LLMClient | None,
    prompt_template: str,
    log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    if client is None or not text.strip():
        return _fallback(original_stem, cfg)

    prompt = build_prompt(
        prompt_template,
        text,
        original_stem,
        date.today().strftime("%Y%m%d"),
    )

    for attempt in range(2):
        try:
            raw = client.complete(prompt)
        except LLMError as exc:
            if attempt == 0:
                continue
            if log is not None:
                log(f"LLM 命名失敗，改用備用檔名：{exc}")
            return _fallback(original_stem, cfg)

        stem = sanitize_filename(raw, cfg.naming.max_filename_length)
        if stem is None:
            if log is not None:
                log("LLM 回傳的檔名無法使用，改用備用檔名")
            return _fallback(original_stem, cfg)
        return stem, "llm"

    return _fallback(original_stem, cfg)


def _strip_edge_markdown(value: str) -> str:
    value = re.sub(r"^```(?:[A-Za-z0-9_-]+)?\s*", "", value)
    value = re.sub(r"\s*```\Z", "", value)
    value = re.sub(r"^#+\s*", "", value)
    value = re.sub(r"^>\s*", "", value)
    value = re.sub(r"^(?:[-+*]|\d+[.)])\s+", "", value)
    return value.strip(_EDGE_MARKERS)


def _is_reserved_device_name(value: str) -> bool:
    stem = value.split(".", 1)[0].casefold()
    return stem in _RESERVED_DEVICE_NAMES


def _stem_unavailable(out_dir: Path, stem: str, used: set[str]) -> bool:
    return (out_dir / f"{stem}.pdf").exists() or stem.casefold() in used


def _fallback(original_stem: str, cfg: AppConfig) -> tuple[str, str]:
    return f"{original_stem}{cfg.naming.fallback_suffix}", "fallback"
