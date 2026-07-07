from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from pdf_ocrer.config import InputConfig

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanItem:
    src: Path
    rel: str


def scan_inputs(folder: Path, output_subdir_name: str, cfg: InputConfig) -> list[ScanItem]:
    folder = Path(folder)
    output_root = (folder / output_subdir_name).resolve()
    items = (
        _scan_recursive(folder, output_subdir_name, output_root, cfg)
        if cfg.recursive
        else _scan_top_level(folder, output_root, cfg)
    )
    return sorted(items, key=lambda item: item.rel.casefold())


def _allowed_extensions(cfg: InputConfig) -> frozenset[str]:
    return frozenset(
        {"pdf", *(extension.removeprefix(".").casefold() for extension in cfg.image_extensions)}
    )


def _scan_top_level(folder: Path, output_root: Path, cfg: InputConfig) -> list[ScanItem]:
    items: list[ScanItem] = []
    for path in folder.iterdir():
        if not _is_allowed_input(path, cfg) or _is_under_output_root(path, output_root):
            continue
        items.append(ScanItem(src=path, rel=path.name))
    return items


def _scan_recursive(
    folder: Path,
    output_subdir_name: str,
    output_root: Path,
    cfg: InputConfig,
) -> list[ScanItem]:
    items: list[ScanItem] = []
    visited: set[Path] = set()
    folder_resolved = _resolve_path(folder)
    for dirpath, dirnames, filenames in os.walk(folder, topdown=True):
        root = Path(dirpath)
        root_resolved = _resolve_path(root)
        if root_resolved in visited:
            dirnames[:] = []
            continue
        visited.add(root_resolved)
        dirnames[:] = _filter_dirnames(
            root,
            root_resolved,
            folder_resolved,
            dirnames,
            output_subdir_name,
            visited,
        )
        for filename in filenames:
            path = root / filename
            if not _is_allowed_input(path, cfg) or _is_under_output_root(path, output_root):
                continue
            items.append(ScanItem(src=path, rel=path.relative_to(folder).as_posix()))
    return items


def _filter_dirnames(
    root: Path,
    root_resolved: Path,
    folder_resolved: Path,
    dirnames: list[str],
    output_subdir_name: str,
    visited: set[Path],
) -> list[str]:
    kept: list[str] = []
    for name in dirnames:
        child = root / name
        if name == output_subdir_name:
            if root_resolved != folder_resolved:
                _logger.warning("剪枝輸出目錄: %s", child)
            continue
        if _resolve_path(child) in visited:
            continue
        kept.append(name)
    return kept


def _is_allowed_input(path: Path, cfg: InputConfig) -> bool:
    return path.is_file() and path.suffix.removeprefix(".").casefold() in _allowed_extensions(cfg)


def _is_under_output_root(path: Path, output_root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(output_root)
    except (OSError, RuntimeError):
        return False


def _resolve_path(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()
