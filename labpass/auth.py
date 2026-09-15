"""Automatic SSO authentication and manual token fallback."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .config import (
    APP_ID,
    CHECK_KEY,
    DEFAULT_HEADERS,
    INTRANET_API_ENDPOINTS,
    INTRANET_VALIDATE_LOGIN_URL,
    REQUEST_TIMEOUT,
    SERVICE_URL,
    SSO_AFTER_LOGIN_URL,
    SSO_LOGIN_URL,
    SSO_PRELOGIN_URL,
    ApiEndpoints,
)
from .crypto import encrypt
from .exceptions import AuthenticationError
from .http import configure_session
from .logging_utils import safe_excerpt

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthenticationResult:
    session: requests.Session
    endpoints: ApiEndpoints
    mode: str


def _request(
    session: requests.Session,
    method: str,
    url: str,
    stage: str,
    **kwargs: Any,
) -> requests.Response:
    logger.debug("认证请求开始：%s", stage)
    try:
        response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise AuthenticationError(f"{stage}网络请求失败：{safe_excerpt(exc)}") from None

    if response.status_code >= 400:
        raise AuthenticationError(f"{stage}失败（HTTP {response.status_code}）")
    _validate_optional_business_response(response, stage)
    logger.debug("认证请求完成：%s（HTTP %s）", stage, response.status_code)
    return response


def _validate_optional_business_response(response: requests.Response, stage: str) -> None:
    content_type = response.headers.get("Content-Type", "").lower()
    body = response.text.lstrip()
    if "json" not in content_type and not body.startswith("{"):
        return

    try:
        payload = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return

    success = payload.get("success")
    code = payload.get("code")
    failed_code = code is not None and str(code) not in {"0", "200"}
    if success is False or failed_code:
        message = safe_excerpt(payload.get("message") or "服务器拒绝了认证请求")
        raise AuthenticationError(f"{stage}失败：{message}")


def _login_payload(encrypted_username: str, encrypted_password: str) -> dict[str, object]:
    return {
        "checkKey": CHECK_KEY,
        "password": encrypted_password,
        "username": encrypted_username,
        "captchaVerification": None,
        "appId": APP_ID,
        "mode": "none",
    }


def _extract_service(url: str) -> str:
    decoded_url = unquote(url)
    query_service = parse_qs(urlparse(decoded_url).query).get("service")
    if query_service and query_service[0]:
        return query_service[0]
    raise AuthenticationError("统一认证响应缺少 service 标识，学校认证流程可能已变更")


def _extract_ticket(url: str) -> str:
    decoded_url = unquote(url)
    query_ticket = parse_qs(urlparse(decoded_url).query).get("ticket")
    if query_ticket and query_ticket[0]:
        return query_ticket[0]

    match = re.search(r"(?:[?&])ticket=(.*?)(?:&|$)", decoded_url)
    if not match or not match.group(1):
        raise AuthenticationError("统一认证响应缺少 ticket，学校认证流程可能已变更")
    return match.group(1)


def _extract_result_token(response: requests.Response, stage: str) -> str:
    try:
        payload = response.json()
        token = payload["result"]["token"]
    except (KeyError, TypeError, requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        raise AuthenticationError(f"{stage}未返回有效 Token") from None
    if not isinstance(token, str) or not token.strip():
        raise AuthenticationError(f"{stage}返回了空 Token")
    return token


def authenticate_automatically(
    username: str,
    password: str,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
) -> AuthenticationResult:
    """Complete the NJUPT SSO flow and return an authenticated Session (校园网直连)."""

    if not username or not password:
        raise AuthenticationError("学号和密码不能为空")

    session = configure_session(session_factory())
    encrypted_username = encrypt(username)
    encrypted_password = encrypt(password)

    try:
        logger.info("正在打开统一身份认证…")
        prelogin_response = _request(
            session,
            "GET",
            SSO_PRELOGIN_URL,
            "打开统一认证",
            allow_redirects=True,
        )
        service = _extract_service(prelogin_response.url)

        logger.info("正在验证统一身份认证…")
        login_response = _request(
            session,
            "POST",
            SSO_LOGIN_URL,
            "提交统一认证",
            headers=DEFAULT_HEADERS,
            json=_login_payload(encrypted_username, encrypted_password),
        )
        tgc = _extract_result_token(login_response, "统一认证")
        session.cookies.set("tgc", tgc, domain="i.njupt.edu.cn", path="/")

        logger.info("正在获取实验室系统访问权限…")
        ticket_response = _request(
            session,
            "GET",
            SSO_AFTER_LOGIN_URL,
            "获取服务票据",
            params={"sessionId": service},
            allow_redirects=True,
        )
        ticket = _extract_ticket(ticket_response.url)

        token_response = _request(
            session,
            "GET",
            INTRANET_VALIDATE_LOGIN_URL,
            "换取业务 Token",
            params={"ticket": ticket, "service": SERVICE_URL},
            headers={"Origin": SERVICE_URL.rstrip("/"), "Referer": SERVICE_URL},
        )
        token = _extract_result_token(token_response, "业务系统")

        session.headers.update({"x-access-token": token})
        logger.info("自动登录成功")
        return AuthenticationResult(session=session, endpoints=INTRANET_API_ENDPOINTS, mode="auto")
    except Exception:
        session.close()
        raise


def authenticate_with_token(
    token: str,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
) -> AuthenticationResult:
    """Create a campus-network Session from an interactively supplied business token."""

    if not token.strip():
        raise AuthenticationError("Token 不能为空")
    session = configure_session(session_factory())
    session.headers.update(
        {
            "x-access-token": token.strip(),
            "Origin": "http://10.22.192.38:9092",
            "Referer": "http://10.22.192.38:9092/",
        }
    )
    return AuthenticationResult(session=session, endpoints=INTRANET_API_ENDPOINTS, mode="token")
