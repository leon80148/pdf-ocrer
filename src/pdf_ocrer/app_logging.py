"""Application file logging setup.

Stage 4 multiprocess note: worker processes should use QueueHandler -> main-process
QueueListener feeding the same handler.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pdf_ocrer.config import LoggingConfig

_HANDLER_MARKER = "_pdf_ocrer_file_handler"
_LOGGER_NAME = "pdf_ocrer"
_LOG_FILENAME = "pdf_ocrer.log"


def setup_logging(cfg: LoggingConfig) -> Path | None:
    if not cfg.enabled:
        return None

    try:
        log_dir = _log_dir(cfg)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / _LOG_FILENAME
        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(cfg.level)

        existing = _find_existing_handler(logger, log_path)
        if existing is not None:
            return Path(existing.baseFilename)

        _remove_stale_file_handlers(logger)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    except OSError:
        return None

    return log_path


def _log_dir(cfg: LoggingConfig) -> Path:
    if cfg.dir:
        return Path(cfg.dir).expanduser()

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "pdf_ocrer" / "logs"

    return Path.home() / ".pdf_ocrer" / "logs"


def _find_existing_handler(
    logger: logging.Logger,
    log_path: Path,
) -> RotatingFileHandler | None:
    resolved = log_path.resolve()
    for handler in logger.handlers:
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        base_filename = getattr(handler, "baseFilename", None)
        if base_filename is not None and Path(base_filename).resolve() == resolved:
            return handler
    return None


def _remove_stale_file_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()
