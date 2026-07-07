from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from pdf_ocrer.manifest import FileIdentity, MANIFEST_NAME, Manifest
from pdf_ocrer.pipeline import FileStatus


def test_manifest_round_trips_entries_and_preserves_completed_timestamp(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / MANIFEST_NAME
    output_root = tmp_path / "out"
    output_file = output_root / "sub" / "doc_OCR.pdf"
    output_file.parent.mkdir(parents=True)
    output_file.write_bytes(b"pdf")
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"source")
    identity = FileIdentity.from_stat(source)

    manifest = Manifest()
    manifest.record(
        "sub/doc.pdf",
        identity,
        FileStatus.SUCCESS_OCR.value,
        "sub/doc_OCR.pdf",
    )
    manifest.save(manifest_path)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["entries"]) == {"sub/doc.pdf"}

    loaded = Manifest.load(manifest_path)
    entry = loaded.should_skip("sub/doc.pdf", identity, output_root)

    assert entry is not None
    assert entry.size == identity.size
    assert entry.mtime_ns == identity.mtime_ns
    assert entry.output == "sub/doc_OCR.pdf"
    assert entry.status == FileStatus.SUCCESS_OCR.value
    completed_at = datetime.fromisoformat(entry.completed_at)
    assert completed_at.tzinfo is not None
    assert completed_at.microsecond == 0


@pytest.mark.parametrize("payload", ["not json", json.dumps({"version": 2, "entries": {}})])
def test_manifest_load_bad_json_or_wrong_version_returns_empty_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    payload: str,
) -> None:
    manifest_path = tmp_path / MANIFEST_NAME
    manifest_path.write_text(payload, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pdf_ocrer.manifest"):
        manifest = Manifest.load(manifest_path)

    assert manifest.entries == {}
    assert "manifest" in caplog.text


def test_manifest_load_missing_file_returns_empty_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="pdf_ocrer.manifest"):
        manifest = Manifest.load(tmp_path / MANIFEST_NAME)

    assert manifest.entries == {}
    assert "manifest" in caplog.text


@pytest.mark.parametrize(
    "status",
    [
        FileStatus.SUCCESS_OCR.value,
        FileStatus.SUCCESS_EXISTING_TEXT.value,
        FileStatus.NO_TEXT_FOUND.value,
    ],
)
def test_should_skip_completed_statuses_require_matching_identity_and_existing_output(
    tmp_path: Path,
    status: str,
) -> None:
    output_root = tmp_path / "out"
    output_file = output_root / "doc_OCR.pdf"
    output_file.parent.mkdir(parents=True)
    output_file.write_bytes(b"pdf")
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"source")
    identity = FileIdentity.from_stat(source)
    manifest = Manifest()
    manifest.record("doc.pdf", identity, status, "doc_OCR.pdf")

    assert manifest.should_skip("doc.pdf", identity, output_root) is not None
    assert (
        manifest.should_skip(
            "doc.pdf",
            FileIdentity(size=identity.size + 1, mtime_ns=identity.mtime_ns),
            output_root,
        )
        is None
    )

    output_file.unlink()

    assert manifest.should_skip("doc.pdf", identity, output_root) is None


def test_should_skip_encrypted_status_does_not_require_output(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.pdf"
    source.write_bytes(b"encrypted")
    identity = FileIdentity.from_stat(source)
    manifest = Manifest()
    manifest.record("encrypted.pdf", identity, FileStatus.SKIPPED_ENCRYPTED.value, None)

    entry = manifest.should_skip("encrypted.pdf", identity, tmp_path / "out")

    assert entry is not None
    assert entry.output is None
    assert entry.status == FileStatus.SKIPPED_ENCRYPTED.value


def test_get_returns_entry_without_skip_requirements(tmp_path: Path) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"source")
    identity = FileIdentity.from_stat(source)
    manifest = Manifest()
    manifest.record("doc.pdf", identity, FileStatus.SUCCESS_OCR.value, "missing_OCR.pdf")

    entry = manifest.get("doc.pdf")

    assert entry is not None
    assert entry.output == "missing_OCR.pdf"


@pytest.mark.parametrize("status", [FileStatus.FAILED.value, "已取消-使用者中止", "未知狀態"])
def test_record_ignores_failed_cancelled_and_unknown_statuses(
    tmp_path: Path,
    status: str,
) -> None:
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"source")
    identity = FileIdentity.from_stat(source)
    manifest = Manifest()

    manifest.record("doc.pdf", identity, status, "doc_OCR.pdf")

    assert manifest.entries == {}
    assert manifest.should_skip("doc.pdf", identity, tmp_path / "out") is None


def test_should_skip_defensively_ignores_unsupported_loaded_status(tmp_path: Path) -> None:
    manifest_path = tmp_path / MANIFEST_NAME
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"source")
    identity = FileIdentity.from_stat(source)
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "doc.pdf": {
                        "size": identity.size,
                        "mtime_ns": identity.mtime_ns,
                        "output": "doc_OCR.pdf",
                        "status": FileStatus.FAILED.value,
                        "completed_at": "2026-07-08T12:00:00+08:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = Manifest.load(manifest_path)

    assert manifest.should_skip("doc.pdf", identity, tmp_path / "out") is None


def test_save_writes_atomically_without_leaving_temp_files(tmp_path: Path) -> None:
    manifest_path = tmp_path / MANIFEST_NAME
    source = tmp_path / "doc.pdf"
    source.write_bytes(b"source")
    identity = FileIdentity.from_stat(source)
    manifest = Manifest()
    manifest.record("doc.pdf", identity, FileStatus.SKIPPED_ENCRYPTED.value, None)

    manifest.save(manifest_path)

    assert manifest_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [MANIFEST_NAME, "doc.pdf"]


def test_save_failure_logs_warning_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pdf_ocrer.manifest as manifest_module

    manifest_path = tmp_path / MANIFEST_NAME
    manifest = Manifest()

    def fail_replace(src: Path, dst: Path) -> None:
        raise OSError(f"cannot replace {src} -> {dst}")

    monkeypatch.setattr(manifest_module.os, "replace", fail_replace)

    with caplog.at_level(logging.WARNING, logger="pdf_ocrer.manifest"):
        manifest.save(manifest_path)

    assert not manifest_path.exists()
    assert "manifest" in caplog.text
