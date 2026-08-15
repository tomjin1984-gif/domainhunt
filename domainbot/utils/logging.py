from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from domainbot.config import Settings


SENSITIVE_KEYS = ("key=", "api_key=", "DYNADOT_API_KEY=")


def redact_secret(value: str) -> str:
    redacted = value
    for marker in SENSITIVE_KEYS:
        if marker.lower() in redacted.lower():
            redacted = redacted.replace(marker, f"{marker}[redacted]")
    return redacted


def configure_logging(settings: Settings) -> None:
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(settings.log_file, maxBytes=10 * 1024 * 1024, backupCount=10),
            logging.StreamHandler(),
        ],
        force=True,
    )

