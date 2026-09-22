"""Run-scoped secret redaction shared by authentication and diagnostics."""

import json
import re
import threading
from urllib.parse import quote, quote_plus

_KEYS = (
    r"password|passwd|x-access-token|access[-_]token|authorization|token|entoken|ticket|"
    r"cookie|set-cookie|tgc|jsessionid|enssessionid|guestsessionid|username|sessionid"
)
_FIELD = re.compile(
    rf"(?i)(\b(?:{_KEYS})\b[\"']?\s*[:=]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}}]+)"
)
_COOKIE_LINE = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_STUDENT = re.compile(r"(?<![\w])(?:[A-Za-z]?\d{8,12})(?![\w])")


class Redactor:
    """Keep known secret values only for the lifetime of one application run."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.RLock()

    def remember(self, *values: object) -> None:
        """Register secrets and common encodings without exposing them in repr."""
        with self._lock:
            for value in values:
                if not isinstance(value, str) or not value:
                    continue
                self._values.update(
                    (value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1])
                )

    def redact(self, value: object) -> str:
        """Redact known values before applying field and identifier rules."""
        text = str(value)
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for secret in values:
            text = text.replace(secret, "***")
        text = _COOKIE_LINE.sub(r"\1***", text)
        text = _FIELD.sub(r"\1***", text)
        text = _BEARER.sub("Bearer ***", text)
        return _STUDENT.sub("***", text)

    def excerpt(self, value: object, limit: int = 500) -> str:
        """Return a bounded, single-line safe diagnostic."""
        text = self.redact(value).replace("\r", " ").replace("\n", " ").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def clear(self) -> None:
        """Release secret references when all diagnostic handlers are closed."""
        with self._lock:
            self._values.clear()


def safe_excerpt(value: object, limit: int = 500) -> str:
    """Redact structured secret fields when no run-scoped context is available."""
    return Redactor().excerpt(value, limit)
