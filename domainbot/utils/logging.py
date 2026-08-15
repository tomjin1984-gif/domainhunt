from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from domainbot.config import Settings


SECRET_PATTERNS = (
    re.compile(r"(?i)(\bkey=)[^&\s\"']+"),
    re.compile(r"(?i)(\bapi_key=)[^&\s\"']+"),
    re.compile(r"(?i)(\bDYNADOT_API_KEY=)[^\s\"']+"),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)[^\s\"']+"),
)


def redact_secret(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[redacted]", redacted)
    return redacted


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_secret(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    redacting_filter = RedactingFilter()
    file_handler = RotatingFileHandler(settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=10)
    stream_handler = logging.StreamHandler()
    file_handler.addFilter(redacting_filter)
    stream_handler.addFilter(redacting_filter)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[file_handler, stream_handler],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
