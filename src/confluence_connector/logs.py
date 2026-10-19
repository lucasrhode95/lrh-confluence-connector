"""Process logging for long-running commands, with one daily-rotated file per process."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import TextIO

from confluence_connector import config

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
BACKUP_DAYS = 7
QUIET_LIBRARIES = ("httpx", "httpcore")
_MANAGED = "_confluence_connector_managed"
_VALID_COMPONENT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_HEADER_SECRET = re.compile(r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)(\s*:\s*)[^\r\n]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|skype[_-]?token|substrate[_-]?token|"
    r"authorization|cookie|client[_-]?secret)\b"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|token|code|client_secret|state)=)[^&#\s]+"
)


def configure_app_logging(
    component: str, *, stdout: bool = False, stream: TextIO | None = None, directory: Path | None = None
) -> Path:
    """Configure process logging and return its file path; callers must use a short component name."""
    if not _VALID_COMPONENT.fullmatch(component):
        raise ValueError("component must be a short lowercase identifier.")
    target = directory if directory is not None else log_directory()
    target.mkdir(parents=True, exist_ok=True)
    _prune_expired_logs(target, component)
    log_path = target / f"{component}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"

    level = getattr(logging, os.environ.get(config.LOG_LEVEL_VARIABLE, "INFO").upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if getattr(handler, _MANAGED, False):
            handler.close()
    root.setLevel(level)
    _add_handler(root, logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8", delay=True
    ))
    if stdout:
        _add_handler(root, logging.StreamHandler(stream if stream is not None else sys.stdout))
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    logging.captureWarnings(True)
    logging.getLogger(__name__).info("Logging configured component=%s", component)
    return log_path


def log_directory() -> Path:
    """Return the connector's per-user log directory."""
    return config.data_dir() / "logs"


class SecretRedactionFilter(logging.Filter):
    """Redact common credential forms from messages and tracebacks before output."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = _redact("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
        except Exception:
            record.msg = "Log record omitted because safe formatting failed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def _add_handler(root: logging.Logger, handler: logging.Handler) -> None:
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    handler.addFilter(SecretRedactionFilter())
    setattr(handler, _MANAGED, True)
    root.addHandler(handler)


def _prune_expired_logs(directory: Path, component: str) -> None:
    """Remove process logs only after the retention window has elapsed."""
    cutoff = time.time() - (BACKUP_DAYS + 1) * 24 * 60 * 60
    for path in directory.glob(f"{component}-*.log*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _redact(value: str) -> str:
    value = _HEADER_SECRET.sub(r"\1\2[REDACTED]", value)
    value = _BEARER_SECRET.sub("Bearer [REDACTED]", value)
    value = _ASSIGNED_SECRET.sub(r"\1\2[REDACTED]", value)
    return _QUERY_SECRET.sub(r"\1[REDACTED]", value)
