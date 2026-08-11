"""Console logging with defense-in-depth redaction."""

import logging
import re
import sys
from typing import Final

_KEY_VALUE_SECRET: Final = re.compile(
    r"(?i)(password|passwd|x-access-token|authorization|token|ticket|cookie|"
    r"jsessionid|enssessionid|guestsessionid|username)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)([^\s,;&}\"']+)",
)
_BEARER_SECRET: Final = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_STUDENT_NUMBER: Final = re.compile(r"(?<!\d)(\d{8,12})(?!\d)")


def redact_text(value: object) -> str:
    """Return text safe enough for console diagnostics."""

    text = str(value)
    text = _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)
    text = _BEARER_SECRET.sub("Bearer ***", text)
    return _STUDENT_NUMBER.sub(lambda match: f"{match.group(1)[:2]}***{match.group(1)[-2:]}", text)


def safe_excerpt(value: object, limit: int = 500) -> str:
    text = redact_text(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(debug: bool = False) -> None:
    """Configure one thread-safe console handler for the application."""

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(
        RedactingFormatter(
            fmt="%(asctime)s | %(levelname)-7s | %(threadName)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
