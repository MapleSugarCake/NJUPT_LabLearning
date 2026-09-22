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
    VPN_AUTHORIZATION,
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


def _identity_redirect_path(path: str) -> bool:
    return path in {
        "/cas/login",
        "/cas/granting",
        "/ssoLogin/index",
        "/user-login",
    } or path.startswith("/user-login/")


def _normalize_redirect(url: str) -> str:
    """Apply the browser's HTTPS upgrade only to approved identity routes."""
    parts = urlsplit(url)
    if (
        parts.scheme == "http"
        and parts.netloc == urlsplit(SSO_BASE).netloc
        and _identity_redirect_path(parts.path)
    ):
        return parts._replace(scheme="https").geturl()
    return url


def _allowed_redirect(url: str, *, via_vpn: bool) -> bool:
    """Validate each destination before sending any bootstrap cookies."""
    parts = urlsplit(url)
    path = unquote(parts.path)
    if (
        parts.username
        or parts.password
        or "\\" in path
        or "%" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin == SSO_BASE:
        return _identity_redirect_path(parts.path)
    if not via_vpn:
        return origin == SERVICE_URL.rstrip("/") and (parts.path or "/") in {"/", "/index.html"}
    if origin != VPN_ORIGIN:
        return False
    if parts.path in {"/", urlsplit(VPN_AUTHORIZATION).path} or parts.path.startswith(
        ("/enlink/", "/webvpn/")
    ):
        return True
    # Mapping identifiers are opaque. Credentials are posted only to fixed identity URLs;
    # resource tokens are never attached to this bootstrap Session.
    return bool(re.fullmatch(r"/http/webvpn[0-9a-f]+(?:/.*)?", parts.path))


def _vpn_authorization_key(url: str) -> str | None:
    """Identify a gateway credential independently of URL ordering and return parameters."""
    parts = urlsplit(url)
    if parts._replace(query="", fragment="").geturl() != VPN_AUTHORIZATION:
        return None
    params = parse_qs(parts.query, keep_blank_values=True)
    for name in ("entoken", "redirect_url"):
        values = params.get(name, [])
        if len(values) != 1 or not values[0].strip():
            raise AuthProtocolError("VPN 网关授权地址缺少有效 entoken 或 redirect_url 参数")
    return VPN_AUTHORIZATION + "?" + urlencode({"entoken": params["entoken"][0]})


def _request(
    session: HttpSession, method: str, url: str, stage: str, **kwargs: Any
) -> requests.Response:
    logger.debug("认证阶段开始：%s", stage)
    for key, values in _query(url).items():
        if key.casefold() in {"ticket", "sessionid", "entoken"}:
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
        response.close()
        raise AuthExpiredError(f"{stage}认证已失效（HTTP {response.status_code}）")
    if not 200 <= response.status_code < 400:
        response.close()
        raise AuthUnavailableError(f"{stage}失败（HTTP {response.status_code}）")
    if method.upper() == "POST" and response.is_redirect:
        response.close()
        raise AuthOutcomeUncertainError("认证 POST 返回重定向；未重发，请先到网页核对")
    logger.debug("认证阶段完成：%s（HTTP %d）", stage, response.status_code)
    return response


def _follow(
    session: HttpSession,
    url: str,
    stage: str,
    *,
    via_vpn: bool,
    consumed: set[str] | None = None,
) -> requests.Response:
    visited: set[tuple[str, str, bool]] = set()
    gateway_authorized = False
    consumed = consumed if consumed is not None else set()
    for hop in range(11):
        url = _normalize_redirect(url)
        if not _allowed_redirect(url, via_vpn=via_vpn):
            raise AuthProtocolError(f"{stage}返回了未批准的跳转目标")
        prepared = session.prepare_request(requests.Request("GET", url))
        # Gateway authorization can change server-side state without changing cookies.
        # Ordinary requests are compared within that phase, using destination cookies.
        target = urlsplit(prepared.url)._replace(fragment="").geturl()
        state = (target, prepared.headers.get("Cookie", ""), gateway_authorized)
        authorization_key = _vpn_authorization_key(target)
        single_use = (
            authorization_key is not None
            or "ticket" in _query(url)
            or urlsplit(url).path.endswith(("/cas/granting", VALIDATE_PATH))
        )
        consumption_key = authorization_key or target
        if state in visited or (single_use and consumption_key in consumed):
            raise AuthProtocolError(f"{stage}出现重定向循环；未重复访问票据地址")
        visited.add(state)
        if single_use:
            consumed.add(consumption_key)
        response = _request(session, "GET", url, stage, retry_get=not single_use)
        if not 300 <= response.status_code < 400:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise AuthProtocolError(f"{stage}重定向缺少 Location")
        if hop == 10:
            raise AuthProtocolError(f"{stage}重定向超过 10 次")
        logger.debug("认证重定向：%s（第 %d/10 跳）", stage, hop + 1)
        url = urljoin(response.url or url, location)
        if authorization_key and response.status_code == 302 and url == VPN_PRELOGIN_URL:
            # Only this verified transition permits re-entry with unchanged cookies.
            # It neither resets the hop budget nor releases consumed credentials.
            gateway_authorized = True
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
    with _request(
        session,
        "GET",
        base + "/AppLoginAllocation/queryLoginAllocationByService" + suffix,
        "读取应用登录配置",
        params={"service": service_id},
    ) as allocation:
        config = validated_result(allocation, "读取应用登录配置", session.redactor)
    app_id = config.get("loginAppId") or config.get("appId")
    if not isinstance(app_id, str) or not app_id.strip():
        raise AuthProtocolError("登录配置缺少应用标识")
    encrypted_username, encrypted_password = encrypt(username), encrypt(password)
    session.redactor.remember(encrypted_username, encrypted_password)
    with _request(
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
    ) as response:
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
    with response:
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
    session = None
    completed = False
    try:
        session = factory()
        check_access(session, base, redactor=redactor)
        completed = True
        return AuthenticationResult(session, base, factory)
    finally:
        if not completed:
            if session is not None:
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
    consumed: set[str] = set()
    with session_factory(redactor=redactor) as session:
        if via_vpn:
            _follow(
                session, VPN_PRELOGIN_URL, "建立 VPN 会话", via_vpn=True, consumed=consumed
            ).close()
            with _follow(
                session,
                SSO_BASE + "/cas/login?" + urlencode({"service": VPN_CALLBACK}),
                "打开统一认证",
                via_vpn=True,
                consumed=consumed,
            ) as prelogin:
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
                consumed=consumed,
            ).close()
        identity = VPN_IDENTITY_BASE if via_vpn else SSO_BASE
        with _follow(
            session,
            identity + "/cas/login?" + urlencode({"service": SERVICE_URL}),
            "初始化实验室登录",
            via_vpn=via_vpn,
            consumed=consumed,
        ) as prelogin:
            prelogin_url = prelogin.url
        if "ticket" not in _query(prelogin_url):
            service_id = _service_id(prelogin_url)
            _identity_login(session, identity, service_id, username, password)
            with _follow(
                session,
                identity + "/ssoLogin/index?" + urlencode({"sessionId": service_id}),
                "获取实验室服务票据",
                via_vpn=via_vpn,
                consumed=consumed,
            ) as prelogin:
                prelogin_url = prelogin.url
        ticket = _ticket(prelogin_url)
        redactor.remember(ticket)
        base = api_base(environment)
        params = {"ticket": ticket, "service": SERVICE_URL}
        if via_vpn:
            timestamp = cookie_value(session.cookies, "vpn_timestamp", base + VALIDATE_PATH)
            if timestamp:
                params["_t"] = timestamp
        with _request(
            session,
            "GET",
            base + VALIDATE_PATH,
            "换取实验室资源 Token",
            params=params,
            retry_get=False,
        ) as response:
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
