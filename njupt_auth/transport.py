"""Explicit GET retries and independently owned, resource-scoped Sessions."""

import copy
import logging
import posixpath
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import RequestsCookieJar, get_cookie_header

from .config import (
    DEFAULT_HEADERS,
    GET_RETRY_ATTEMPTS,
    GET_RETRY_BACKOFF,
    REQUEST_TIMEOUT,
    RETRY_STATUS_CODES,
)
from .redaction import Redactor

logger = logging.getLogger(__name__)


def cookies_for_url(cookies: RequestsCookieJar, url: str) -> RequestsCookieJar:
    """Copy matching cookies without dropping domain/path/security attributes."""
    selected = RequestsCookieJar()
    prepared = requests.Request("GET", url).prepare()
    for cookie in cookies:
        candidate = RequestsCookieJar()
        candidate.set_cookie(copy.deepcopy(cookie))
        if get_cookie_header(candidate, prepared):
            selected.set_cookie(copy.deepcopy(cookie))
    return selected


def cookie_value(cookies: RequestsCookieJar, name: str, url: str) -> str | None:
    """Resolve a cookie against its destination, preferring the longest path."""
    matches = sorted(
        (cookie for cookie in cookies_for_url(cookies, url) if cookie.name == name),
        key=lambda cookie: len(cookie.path),
        reverse=True,
    )
    return matches[0].value if matches else None


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    delay = GET_RETRY_BACKOFF * 2**attempt
    if response is not None:
        value = response.headers.get("Retry-After", "")
        try:
            delay = float(value)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                delay = (deadline - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, min(delay, 30.0))


class HttpSession(requests.Session):
    """No implicit redirects or adapter retries; only selected GETs are retried."""

    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__()
        self.redactor = redactor if redactor is not None else Redactor()
        self._sleep = sleep
        self.closed = False
        self.headers.update(DEFAULT_HEADERS)
        for adapter in self.adapters.values():
            adapter.close()
        self.mount("http://", HTTPAdapter(max_retries=0))
        self.mount("https://", HTTPAdapter(max_retries=0))

    def request(
        self, method: str, url: str, *, retry_get: bool = True, **kwargs: Any
    ) -> requests.Response:
        if self.closed:
            raise requests.RequestException("会话已关闭")
        method = method.upper()
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = kwargs.get("timeout") or REQUEST_TIMEOUT
        attempts = GET_RETRY_ATTEMPTS if method == "GET" and retry_get else 1
        for attempt in range(attempts):
            response = None
            try:
                response = super().request(method, url, **kwargs)
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                if isinstance(exc, requests.exceptions.SSLError) or attempt + 1 == attempts:
                    raise
            else:
                self.redactor.remember(*(cookie.value for cookie in self.cookies))
                if response.status_code in RETRY_STATUS_CODES:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict) and str(payload.get("code")) in {"401", "403"}:
                        return response
                if response.status_code not in RETRY_STATUS_CODES or attempt + 1 == attempts:
                    return response
            delay = _retry_delay(response, attempt)
            if response is not None:
                response.close()
            logger.debug("GET 暂时失败，将进行第 %d/%d 次尝试", attempt + 2, attempts)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def close(self) -> None:
        if not self.closed:
            super().close()
            self.cookies.clear()
            self.closed = True


class ResourceSession(HttpSession):
    """A requests.Session restricted to the authenticated resource API prefix."""

    def __init__(self, api_base_url: str, token: str, *, via_vpn: bool, redactor: Redactor) -> None:
        super().__init__(redactor=redactor)
        self.api_base_url = api_base_url.rstrip("/")
        self._token = token
        self._via_vpn = via_vpn
        self.redactor.remember(token)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        base, target = urlsplit(self.api_base_url), urlsplit(url)
        path = unquote(target.path)
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or target.fragment
            or "\\" in path
            or not posixpath.normpath(path).startswith(base.path + "/")
            or "%" in path
        ):
            raise requests.exceptions.InvalidURL("拒绝向认证资源范围之外发送请求")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Access-Token"] = self._token
        kwargs["headers"] = headers
        if self._via_vpn:
            if method.upper() == "GET":
                params = dict(kwargs.pop("params", {}) or {})
                timestamp = cookie_value(self.cookies, "vpn_timestamp", url)
                if timestamp:
                    params.setdefault("_t", timestamp)
                kwargs["params"] = params
            elif method.upper() == "POST":
                query = target.query
                if "enlink-vpn" not in query.split("&"):
                    query = f"{query}&enlink-vpn" if query else "enlink-vpn"
                url = urlunsplit(target._replace(query=query))
        return super().request(method, url, **kwargs)

    def close(self) -> None:
        super().close()
        self._token = ""


class ResourceSessionFactory:
    """Thread-safe immutable authentication snapshot, never a shared Session."""

    def __init__(
        self,
        api_base_url: str,
        token: str,
        cookies: RequestsCookieJar,
        *,
        via_vpn: bool,
        redactor: Redactor,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.api_base_url = api_base_url
        self._token = token
        # Include cookies scoped to API subpaths as well as the API root.
        host = urlsplit(api_base_url).hostname or ""
        self._cookies = RequestsCookieJar()
        for cookie in cookies:
            domain = cookie.domain.lstrip(".")
            if host == domain or (cookie.domain_initial_dot and host.endswith("." + domain)):
                self._cookies.set_cookie(copy.deepcopy(cookie))
        self._headers = dict(headers or {})
        self._via_vpn = via_vpn
        self._redactor = redactor
        self._lock = threading.Lock()
        self._closed = False
        redactor.remember(token, *(cookie.value for cookie in self._cookies))

    def __call__(self) -> ResourceSession:
        with self._lock:
            if self._closed:
                raise RuntimeError("认证会话工厂已关闭")
            session = ResourceSession(
                self.api_base_url, self._token, via_vpn=self._via_vpn, redactor=self._redactor
            )
            session.headers.update(self._headers)
            session.cookies = copy.deepcopy(self._cookies)
            return session

    def close(self) -> None:
        """Release the snapshot after the owner has joined all course threads."""
        with self._lock:
            self._closed = True
            self._token = ""
            self._cookies.clear()
            self._headers.clear()
