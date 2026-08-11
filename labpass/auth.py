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
    REQUEST_TIMEOUT,
    SERVICE_URL,
    SSO_AFTER_LOGIN_URL,
    SSO_LOGIN_URL,
    SSO_PRELOGIN_URL,
    VPN_API_ENDPOINTS,
    VPN_CAS_AFTER_LOGIN_URL,
    VPN_CAS_LOGIN_URL,
    VPN_CAS_PRELOGIN_URL,
    VPN_PRELOGIN_URL,
    VPN_VALIDATE_LOGIN_URL,
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


def _extract_service_id(url: str) -> str:
    match = re.search(r"[&?]service=([A-Za-z0-9]+)", url)
    if not match:
        raise AuthenticationError("VPN 登录响应缺少 service 标识，学校认证流程可能已变更")
    return match.group(1)


def _extract_ticket(url: str) -> str:
    decoded_url = unquote(url)
    query_ticket = parse_qs(urlparse(decoded_url).query).get("ticket")
    if query_ticket and query_ticket[0]:
        return query_ticket[0]

    match = re.search(r"(?:[?&])ticket=(.*?)(?:&|$)", decoded_url)
    if not match or not match.group(1):
        raise AuthenticationError("统一认证响应缺少 ticket，学校认证流程可能已变更")
    return match.group(1)


def authenticate_automatically(
    username: str,
    password: str,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
) -> AuthenticationResult:
    """Complete the four-stage NJUPT SSO flow and return an authenticated Session."""

    if not username or not password:
        raise AuthenticationError("学号和密码不能为空")

    session = configure_session(session_factory())
    encrypted_username = encrypt(username)
    encrypted_password = encrypt(password)

    try:
        logger.info("正在建立 VPN 会话…")
        _request(session, "GET", VPN_PRELOGIN_URL, "建立 VPN 会话")
        _request(session, "GET", SSO_PRELOGIN_URL, "打开统一认证")

        logger.info("正在验证统一身份认证…")
        _request(
            session,
            "POST",
            SSO_LOGIN_URL,
            "提交统一认证",
            headers=DEFAULT_HEADERS,
            json=_login_payload(encrypted_username, encrypted_password),
        )
        jsession_id = session.cookies.get("JSESSIONID")
        if not jsession_id:
            raise AuthenticationError("统一认证未返回 JSESSIONID，请检查账号、密码或验证码要求")
        _request(
            session,
            "GET",
            SSO_AFTER_LOGIN_URL,
            "确认统一认证",
            params={"sessionId": jsession_id},
        )

        logger.info("正在获取实验室系统访问权限…")
        prelogin_response = _request(
            session,
            "GET",
            VPN_CAS_PRELOGIN_URL,
            "初始化实验室系统登录",
        )
        service_id = _extract_service_id(prelogin_response.url)
        _request(
            session,
            "POST",
            VPN_CAS_LOGIN_URL,
            "授权实验室系统",
            json=_login_payload(encrypted_username, encrypted_password),
        )
        ticket_response = _request(
            session,
            "GET",
            VPN_CAS_AFTER_LOGIN_URL,
            "获取服务票据",
            params={"sessionId": service_id},
        )
        ticket = _extract_ticket(ticket_response.url)

        token_response = _request(
            session,
            "GET",
            VPN_VALIDATE_LOGIN_URL,
            "换取业务 Token",
            params={
                "_t": session.cookies.get("vpn_timestamp"),
                "ticket": ticket,
                "service": SERVICE_URL,
                "enlink-vpn": None,
            },
        )
        try:
            payload = token_response.json()
            token = payload["result"]["token"]
        except (KeyError, TypeError, requests.JSONDecodeError, json.JSONDecodeError, ValueError):
            raise AuthenticationError("业务系统未返回有效 Token") from None
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError("业务系统返回了空 Token")

        session.headers.update({"x-access-token": token})
        logger.info("自动登录成功")
        return AuthenticationResult(session=session, endpoints=VPN_API_ENDPOINTS, mode="auto")
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
