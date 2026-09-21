"""One mutation lock and cancellation signal per business run."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .exceptions import AuthenticationExpiredError, RunCancelledError


class MutationCoordinator:
    """Shared by the main client and every course client, never by Session ownership."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._authentication_failed = threading.Event()

    def cancel(self, *, authentication_failed: bool = False) -> None:
        if authentication_failed:
            self._authentication_failed.set()
        self._cancelled.set()

    def check_cancelled(self) -> None:
        if self._authentication_failed.is_set():
            raise AuthenticationExpiredError("任务因登录状态失效而取消")
        if self._cancelled.is_set():
            raise RunCancelledError("任务已取消")

    @contextmanager
    def serialized(self) -> Iterator[None]:
        with self._lock:
            self.check_cancelled()
            yield
