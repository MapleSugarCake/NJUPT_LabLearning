"""Console/file logging configuration; reusable packages never configure handlers."""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from njupt_auth.redaction import Redactor

from .config import LOG_FILENAME


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor, *, debug: bool) -> None:
        super().__init__(
            "%(asctime)s | %(levelname)-7s | %(threadName)s | %(message)s"
            if debug
            else "%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))


def log_path() -> Path:
    """Frozen builds use the EXE directory, never the extraction directory."""
    directory = (
        Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    )
    return directory / LOG_FILENAME


@contextmanager
def configure_logging(
    debug: bool, *, redactor: Redactor, stream: TextIO | None = None
) -> Iterator[None]:
    """Install run-scoped handlers, restoring and closing them on every exit path."""
    handlers: list[logging.Handler] = []
    previous = []
    try:
        if debug:
            handlers.append(logging.FileHandler(log_path(), mode="x", encoding="utf-8"))
        handlers.append(logging.StreamHandler(stream if stream is not None else sys.stdout))
        for handler in handlers:
            handler.setLevel(logging.DEBUG if debug else logging.INFO)
            handler.setFormatter(RedactingFormatter(redactor, debug=debug))
        for name in ("njupt_auth", "njupt_safetylabpass", "labpass_cli"):
            logger = logging.getLogger(name)
            previous.append((logger, logger.handlers[:], logger.level, logger.propagate))
            logger.handlers = handlers[:]
            logger.setLevel(logging.DEBUG if debug else logging.INFO)
            logger.propagate = False
        yield
    finally:
        for logger, old_handlers, level, propagate in previous:
            logger.handlers = old_handlers
            logger.setLevel(level)
            logger.propagate = propagate
        for handler in handlers:
            handler.close()
