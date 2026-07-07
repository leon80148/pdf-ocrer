from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = ".pdf_ocrer_manifest.json"

_VERSION = 1
_logger = logging.getLogger(__name__)

_SUCCESS_WITH_OUTPUT_STATUSES = frozenset(
    {
        "OCR完成",
        "已有文字層-僅命名",
        "無文字-原樣輸出",
    }
)
_SKIPPED_ENCRYPTED_STATUS = "加密-跳過"
_RECORDABLE_STATUSES = _SUCCESS_WITH_OUTPUT_STATUSES | {_SKIPPED_ENCRYPTED_STATUS}


@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, path: Path) -> FileIdentity:
        stat = path.stat()
        return cls(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


@dataclass(frozen=True)
class ManifestEntry:
    size: int
    mtime_ns: int
    output: str | None
    status: str
    completed_at: str


@dataclass
class Manifest:
    entries: dict[str, ManifestEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        try:
            if not path.exists():
                _logger.warning("manifest load skipped: file does not exist path=%s", path)
                return cls()

            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _VERSION:
                _logger.warning("manifest load skipped: unsupported version path=%s", path)
                return cls()

            raw_entries = payload.get("entries")
            if not isinstance(raw_entries, dict):
                _logger.warning("manifest load skipped: invalid entries path=%s", path)
                return cls()

            entries: dict[str, ManifestEntry] = {}
            for rel, raw_entry in raw_entries.items():
                if not isinstance(rel, str) or not isinstance(raw_entry, dict):
                    _logger.warning("manifest entry ignored: invalid entry path=%s rel=%r", path, rel)
                    continue
                entry = _entry_from_payload(raw_entry)
                if entry is None:
                    _logger.warning("manifest entry ignored: invalid fields path=%s rel=%s", path, rel)
                    continue
                entries[rel] = entry
            return cls(entries)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            _logger.warning("manifest load failed path=%s error=%s", path, exc)
            return cls()

    def should_skip(
        self,
        rel: str,
        identity: FileIdentity,
        output_root: Path,
    ) -> ManifestEntry | None:
        entry = self.entries.get(rel)
        if entry is None:
            return None
        if entry.size != identity.size or entry.mtime_ns != identity.mtime_ns:
            return None

        if entry.status in _SUCCESS_WITH_OUTPUT_STATUSES:
            if entry.output is None:
                return None
            if not (output_root / entry.output).exists():
                return None
            return entry

        if entry.status == _SKIPPED_ENCRYPTED_STATUS:
            return entry

        return None

    def get(self, rel: str) -> ManifestEntry | None:
        return self.entries.get(rel)

    def record(
        self,
        rel: str,
        identity: FileIdentity,
        status: str,
        output: str | None,
    ) -> None:
        if status not in _RECORDABLE_STATUSES:
            return

        self.entries[rel] = ManifestEntry(
            size=identity.size,
            mtime_ns=identity.mtime_ns,
            output=output,
            status=status,
            completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def save(self, path: Path) -> None:
        tmp_path = path.with_name(f"{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(self._to_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except OSError as exc:
            _logger.warning("manifest save failed path=%s error=%s", path, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _to_payload(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "entries": {
                rel: {
                    "size": entry.size,
                    "mtime_ns": entry.mtime_ns,
                    "output": entry.output,
                    "status": entry.status,
                    "completed_at": entry.completed_at,
                }
                for rel, entry in self.entries.items()
            },
        }


def _entry_from_payload(payload: dict[str, Any]) -> ManifestEntry | None:
    size = payload.get("size")
    mtime_ns = payload.get("mtime_ns")
    output = payload.get("output")
    status = payload.get("status")
    completed_at = payload.get("completed_at")

    if not _is_int(size) or not _is_int(mtime_ns):
        return None
    if output is not None and not isinstance(output, str):
        return None
    if not isinstance(status, str) or not isinstance(completed_at, str):
        return None

    return ManifestEntry(
        size=size,
        mtime_ns=mtime_ns,
        output=output,
        status=status,
        completed_at=completed_at,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
