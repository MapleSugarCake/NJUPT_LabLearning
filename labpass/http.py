"""Shared HTTP session setup."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    DEFAULT_HEADERS,
    GET_RETRY_ATTEMPTS,
    GET_RETRY_BACKOFF,
    RETRY_STATUS_CODES,
)


def configure_session(session: requests.Session) -> requests.Session:
    """Apply headers and GET-only retry behavior to a Session."""

    retry = Retry(
        total=GET_RETRY_ATTEMPTS - 1,
        connect=GET_RETRY_ATTEMPTS - 1,
        read=GET_RETRY_ATTEMPTS - 1,
        status=GET_RETRY_ATTEMPTS - 1,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=RETRY_STATUS_CODES,
        backoff_factor=GET_RETRY_BACKOFF,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    return session
