"""SSO/CAS authentication and read-only verification of laboratory access."""

import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import requests
from requests.cookies import RequestsCookieJar

from .config import (
    CHECK_KEY,
    INTRANET_API_BASE,
    PERMISSION_PATH,
    SERVICE_URL,
    SSO_BASE,
    VALIDATE_PATH,
    VPN_API_BASE,
    VPN_CALLBACK,
    VPN_IDENTITY_BASE,
    VPN_ORIGIN,
    VPN_PRELOGIN_URL,
)
from .crypto import encrypt
from .errors import (
    AuthError,
    AuthExpiredError,
    AuthOutcomeUncertainError,
    AuthProtocolError,
    AuthUnavailableError,
    InteractionRequiredError,
    InvalidCredentialsError,
)
from .models import AuthenticationResult, NetworkEnvironment
from .redaction import Redactor
from .transport import HttpSession, ResourceSessionFactory, cookie_value

logger = logging.getLogger(__name__)


def api_base(environment: NetworkEnvironment) -> str:
    """Resolve a resource route only after the caller explicitly selects a network."""
    return VPN_API_BASE if environment is NetworkEnvironment.EXTRANET else INTRANET_API_BASE


def _query(url: str) -> dict[str, list[str]]:
    parts = urlsplit(url)
    values = parse_qs(parts.query)
    if "?" in parts.fragment:
        values.update(parse_qs(parts.fragment.split("?", 1)[1]))
    return values


def _service_id(url: str) -> str:
    values = _query(url).get("service", [])
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", values[0]):
        raise AuthProtocolError("统一认证登录页缺少有效 service 标识")
    return values[0]


def _ticket(url: str) -> str:
    values = _query(url).get("ticket", [])
    if not values:
        values = _query(unquote(url)).get("ticket", [])
    if len(values) != 1 or not values[0].strip():
        raise AuthProtocolError("统一认证响应缺少有效 ticket")
    return values[0]


def _allowed_redirect(url: str, *, via_vpn: bool) -> bool:
    """Allow the observed CAS HTTP upgrade and the selected resource routes only."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin == SSO_BASE:
        return parts.path.startswith(("/cas/", "/ssoLogin/", "/user-login/"))
    if origin == "http://i.njupt.edu.cn":
        return parts.path in {"/cas/login", "/cas/granting"}
    if not via_vpn:
        return origin == SERVICE_URL.rstrip("/") and parts.path in {"/", "/index.html"}
    if origin != VPN_ORIGIN:
        return False
    if parts.path == "/" or parts.path.startswith(("/enlink/", "/webvpn/")):
        return True
    # Mapping identifiers are opaque. Credentials are posted only to fixed identity URLs;
    # resource tokens are never attached to this bootstrap Session.
    return bool(re.fullmatch(r"/http/webvpn[0-9a-f]+/.*", parts.path))


def _request(
    session: HttpSession, method: str, url: str, stage: str, **kwargs: Any
) -> requests.Response:
    logger.debug("认证阶段开始：%s", stage)
    for key, values in _query(url).items():
        if key.casefold() in {"ticket", "sessionid"}:
            session.redactor.remember(*values)
    try:
        response = session.request(method, url, **kwargs)
    except requests.RequestException:
        if method.upper() == "POST" or kwargs.get("retry_get") is False:
            raise AuthOutcomeUncertainError(
                f"{stage}请求失败，结果不确定；未自动重试，请先到网页核对"
            ) from None
        raise AuthUnavailableError(f"{stage}网络请求失败，请检查网络后重新登录") from None
    if response.status_code in {401, 403}:
        raise AuthExpiredError(f"{stage}认证已失效（HTTP {response.status_code}）")
    if not 200 <= response.status_code < 400:
        raise AuthUnavailableError(f"{stage}失败（HTTP {response.status_code}）")
    if method.upper() == "POST" and response.is_redirect:
        raise AuthOutcomeUncertainError("认证 POST 返回重定向；未重发，请先到网页核对")
    logger.debug("认证阶段完成：%s（HTTP %d）", stage, response.status_code)
    return response


def _follow(session: HttpSession, url: str, stage: str, *, via_vpn: bool) -> requests.Response:
    visited: set[str] = set()
    for hop in range(11):
        if url in visited:
            raise AuthProtocolError(f"{stage}出现重定向循环；未重复访问票据地址")
        visited.add(url)
        if not _allowed_redirect(url, via_vpn=via_vpn):
            raise AuthProtocolError(f"{stage}返回了未批准的跳转目标")
        single_use = "ticket" in _query(url) or urlsplit(url).path.endswith("/cas/granting")
        response = _request(session, "GET", url, stage, retry_get=not single_use)
        if not 300 <= response.status_code < 400:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise AuthProtocolError(f"{stage}重定向缺少 Location")
        if hop == 10:
            raise AuthProtocolError(f"{stage}重定向超过 10 次")
        url = urljoin(response.url or url, location)
    raise AssertionError("unreachable")


def validated_result(response: requests.Response, stage: str, redactor: Redactor) -> dict[str, Any]:
    """Validate an authentication JSON envelope without exposing its raw body."""
    if response.status_code != 200:
        raise AuthProtocolError(f"{stage}未返回 HTTP 200")
    try:
        payload = response.json()
    except ValueError:
        raise AuthProtocolError(f"{stage}返回的不是有效 JSON") from None
    if not isinstance(payload, dict):
        raise AuthProtocolError(f"{stage}响应格式无效")
    result = payload.get("result")
    if isinstance(result, dict):
        redactor.remember(result.get("token"))
    code = str(payload.get("code"))
    message = redactor.excerpt(payload.get("message") or "认证服务拒绝请求")
    if code in {"401", "403"}:
        raise AuthExpiredError("登录状态已失效，请重新认证")
    if payload.get("success") is not True or code not in {"0", "200"}:
        if any(
            word in message.casefold()
            for word in ("captcha", "mfa", "验证码", "二次验证", "绑定手机")
        ):
            raise InteractionRequiredError("认证需要人工操作，请选择浏览器登录")
        if any(word in message for word in ("密码错误", "用户名或密码", "账号或密码")):
            raise InvalidCredentialsError("账号或密码被拒绝，请检查输入")
        raise AuthError(f"{stage}失败：{message}")
    if not isinstance(result, dict):
        raise AuthProtocolError(f"{stage}响应缺少有效 result 对象")
    return result


def _identity_login(
    session: HttpSession,
    base: str,
    service_id: str,
    username: str,
    password: str,
) -> None:
    session.redactor.remember(service_id)
    suffix = "?enlink-vpn" if base == VPN_IDENTITY_BASE else ""
    allocation = _request(
        session,
        "GET",
        base + "/AppLoginAllocation/queryLoginAllocationByService" + suffix,
        "读取应用登录配置",
        params={"service": service_id},
    )
    config = validated_result(allocation, "读取应用登录配置", session.redactor)
    app_id = config.get("loginAppId") or config.get("appId")
    if not isinstance(app_id, str) or not app_id.strip():
        raise AuthProtocolError("登录配置缺少应用标识")
    encrypted_username, encrypted_password = encrypt(username), encrypt(password)
    session.redactor.remember(encrypted_username, encrypted_password)
    response = _request(
        session,
        "POST",
        base + "/ssoLogin/login" + suffix,
        "提交统一认证",
        json={
            "checkKey": CHECK_KEY,
            "username": encrypted_username,
            "password": encrypted_password,
            "captchaVerification": None,
            "appId": app_id,
            "mode": "none",
        },
    )
    result = validated_result(response, "提交统一认证", session.redactor)
    identity_token = result.get("token")
    if isinstance(identity_token, str) and identity_token and base == SSO_BASE:
        session.cookies.set("tgc", identity_token, domain="i.njupt.edu.cn", path="/", secure=True)


def check_access(session: requests.Session, api_base_url: str, *, redactor: Redactor) -> None:
    """Require a successful read-only permissions response, never refresh or log in."""
    try:
        response = session.get(api_base_url + PERMISSION_PATH)
    except requests.RequestException:
        raise AuthUnavailableError("无法确认实验室访问权限，请检查网络后重新认证") from None
    if response.status_code in {401, 403} or 300 <= response.status_code < 400:
        raise AuthExpiredError("实验室或 VPN 登录状态已失效")
    result = validated_result(response, "验证实验室访问权限", redactor)
    if not isinstance(result.get("menu"), list):
        raise AuthProtocolError("实验室权限响应缺少有效 menu，无法确认认证结果")


def create_result(
    token: str,
    cookies: RequestsCookieJar,
    *,
    environment: NetworkEnvironment,
    redactor: Redactor,
    headers: dict[str, str] | None = None,
) -> AuthenticationResult:
    """Build, probe and hand over a resource Session with a frozen clone factory."""
    if not isinstance(token, str) or not token.strip():
        raise AuthProtocolError("实验室未返回有效资源 Token")
    base = api_base(environment)
    resource_headers = dict(headers or {})
    if environment is NetworkEnvironment.INTRANET:
        resource_headers.update({"Origin": SERVICE_URL.rstrip("/"), "Referer": SERVICE_URL})
    factory = ResourceSessionFactory(
        base,
        token.strip(),
        cookies,
        via_vpn=environment is NetworkEnvironment.EXTRANET,
        redactor=redactor,
        headers=resource_headers,
    )
    session = factory()
    completed = False
    try:
        check_access(session, base, redactor=redactor)
        completed = True
        return AuthenticationResult(session, base, factory)
    finally:
        if not completed:
            session.close()
            factory.close()


def authenticate(
    username: str,
    password: str,
    *,
    environment: NetworkEnvironment,
    redactor: Redactor | None = None,
    session_factory: Callable[..., HttpSession] = HttpSession,
) -> AuthenticationResult:
    """Authenticate over the selected network, then verify laboratory access."""
    environment = NetworkEnvironment(environment)
    redactor = redactor if redactor is not None else Redactor()
    redactor.remember(username, password)
    if not username.strip() or not password:
        raise InvalidCredentialsError("学号和密码不能为空")
    via_vpn = environment is NetworkEnvironment.EXTRANET
    with session_factory(redactor=redactor) as session:
        if via_vpn:
            _follow(session, VPN_PRELOGIN_URL, "建立 VPN 会话", via_vpn=True)
            prelogin = _follow(
                session,
                SSO_BASE + "/cas/login?" + urlencode({"service": VPN_CALLBACK}),
                "打开统一认证",
                via_vpn=True,
            )
            service_id = _service_id(prelogin.url)
            _identity_login(session, SSO_BASE, service_id, username, password)
            # Preserve the working external flow's first-stage JSESSIONID convention.
            session_id = cookie_value(session.cookies, "JSESSIONID", SSO_BASE + "/ssoLogin/index")
            session_id = session_id or service_id
            redactor.remember(session_id)
            _follow(
                session,
                SSO_BASE + "/ssoLogin/index?" + urlencode({"sessionId": session_id}),
                "确认 VPN 认证",
                via_vpn=True,
            )
        identity = VPN_IDENTITY_BASE if via_vpn else SSO_BASE
        prelogin = _follow(
            session,
            identity + "/cas/login?" + urlencode({"service": SERVICE_URL}),
            "初始化实验室登录",
            via_vpn=via_vpn,
        )
        if "ticket" not in _query(prelogin.url):
            service_id = _service_id(prelogin.url)
            _identity_login(session, identity, service_id, username, password)
            prelogin = _follow(
                session,
                identity + "/ssoLogin/index?" + urlencode({"sessionId": service_id}),
                "获取实验室服务票据",
                via_vpn=via_vpn,
            )
        ticket = _ticket(prelogin.url)
        redactor.remember(ticket)
        base = api_base(environment)
        params = {"ticket": ticket, "service": SERVICE_URL}
        if via_vpn:
            timestamp = cookie_value(session.cookies, "vpn_timestamp", base + VALIDATE_PATH)
            if timestamp:
                params["_t"] = timestamp
        response = _request(
            session,
            "GET",
            base + VALIDATE_PATH,
            "换取实验室资源 Token",
            params=params,
            retry_get=False,
        )
        result = validated_result(response, "换取实验室资源 Token", redactor)
        return create_result(
            result.get("token"), session.cookies, environment=environment, redactor=redactor
        )


def import_token(token: str, *, redactor: Redactor | None = None) -> AuthenticationResult:
    """Import and verify a resource Token on the campus network only."""
    redactor = redactor if redactor is not None else Redactor()
    redactor.remember(token)
    return create_result(
        token, RequestsCookieJar(), environment=NetworkEnvironment.INTRANET, redactor=redactor
    )
