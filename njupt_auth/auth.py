"""实现校内外账号密码认证、受限重定向及资源权限探测。

本文件定义：
    logger：认证阶段的模块日志记录器。
    api_base：根据显式网络选择实验室资源 API 基址。
    _query：合并地址查询串和页面片段中的查询参数。
    _service_id：提取并校验统一认证页面的唯一 service 标识。
    _ticket：提取唯一且非空的服务票据。
    _identity_redirect_path：判断路径是否属于获准的身份认证页面。
    _normalize_redirect：将指定身份站点的获准 HTTP 登录地址升级为 HTTPS。
    _allowed_redirect：在发送认证 Cookie 前检查跳转目标的主机与路径。
    _vpn_authorization_key：为 VPN 网关的一次性授权生成稳定的消费标识。
    _request：发送认证阶段请求并将传输失败转换为安全的认证异常。
    _follow：逐跳校验并跟随认证重定向，限制循环和凭据重放。
    validated_result：校验认证 JSON 响应并提取成功结果对象。
    _identity_login：按本次 service 动态选择应用并提交加密凭据。
    check_access：通过只读权限接口确认现有会话能访问实验室资源。
    create_result：建立资源会话及认证快照工厂，探测成功后交给调用方。
    authenticate：按选择的网络执行账号密码认证并验证实验室权限。
    import_token：在校园网直连条件下导入并验证实验室资源 Token。
"""

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

# 认证阶段的模块日志记录器。
# 只记录阶段和安全诊断，输出处理器由 CLI 统一配置。
logger = logging.getLogger(__name__)


# 作用：根据显式网络选择实验室资源 API 基址。
# 参数：
#     environment：调用方显式选择的网络环境，用于确定直连或 VPN 链路。
# 返回：校外 VPN 映射基址或校园网直连基址。
def api_base(environment: NetworkEnvironment) -> str:
    """根据显式网络选择实验室资源 API 基址。"""
    return VPN_API_BASE if environment is NetworkEnvironment.EXTRANET else INTRANET_API_BASE


# 作用：合并地址查询串和页面片段中的查询参数。
# 参数：
#     url：待解析或请求的完整地址。
# 返回：参数名到字符串列表的映射，保留重复值以便后续检查。
# 说明：片段含问号时解析其后内容，同名键覆盖普通查询串中的值。
def _query(url: str) -> dict[str, list[str]]:
    parts = urlsplit(url)
    values = parse_qs(parts.query)
    if "?" in parts.fragment:
        values.update(parse_qs(parts.fragment.split("?", 1)[1]))
    return values


# 作用：提取并校验统一认证页面的唯一 service 标识。
# 参数：
#     url：待解析或请求的完整地址。
# 返回：只包含字母、数字、下划线或连字符的标识字符串。
# 说明：缺失、重复或含不允许字符时抛出 AuthProtocolError。
def _service_id(url: str) -> str:
    values = _query(url).get("service", [])
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", values[0]):
        raise AuthProtocolError("统一认证登录页缺少有效 service 标识")
    return values[0]


# 作用：提取唯一且非空的服务票据。
# 参数：
#     url：待解析或请求的完整地址。
# 返回：票据字符串，供后续实验室 Token 交换使用。
# 说明：先解析原地址；缺少票据时再解析 URL 解码后的地址，仍无有效值则抛出 AuthProtocolError。
def _ticket(url: str) -> str:
    values = _query(url).get("ticket", [])
    if not values:
        values = _query(unquote(url)).get("ticket", [])
    if len(values) != 1 or not values[0].strip():
        raise AuthProtocolError("统一认证响应缺少有效 ticket")
    return values[0]


# 作用：判断路径是否属于获准的身份认证页面。
# 参数：
#     path：URL 路径部分，包含 CAS 登录、票据授予和用户登录页面路径。
# 返回：路径获准时为 True，否则为 False。
def _identity_redirect_path(path: str) -> bool:
    return path in {
        "/cas/login",
        "/cas/granting",
        "/ssoLogin/index",
        "/user-login",
    } or path.startswith("/user-login/")


# 作用：将指定身份站点的获准 HTTP 登录地址升级为 HTTPS。
# 参数：
#     url：待解析或请求的完整地址。
# 返回：符合升级规则的新地址，或未经更改的原地址。
# 说明：只替换协议，保留路径、查询串及片段中的 service、ticket 等信息。
def _normalize_redirect(url: str) -> str:
    """将指定身份站点的获准 HTTP 登录地址升级为 HTTPS。"""
    parts = urlsplit(url)
    if (
        parts.scheme == "http"
        and parts.netloc == urlsplit(SSO_BASE).netloc
        and _identity_redirect_path(parts.path)
    ):
        return parts._replace(scheme="https").geturl()
    return url


# 作用：在发送认证 Cookie 前检查跳转目标的主机与路径。
# 参数：
#     url：待解析或请求的完整地址。
#     via_vpn：是否允许 VPN 网关及映射地址。
# 返回：地址属于当前网络允许范围时为 True，否则为 False。
# 说明：拒绝含用户信息、路径穿越、反斜杠或残留编码的目标；VPN 映射标识按地址规则校验。
def _allowed_redirect(url: str, *, via_vpn: bool) -> bool:
    """在发送认证 Cookie 前检查跳转目标的主机与路径。"""
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
    # 映射标识只作为不透明地址片段处理；凭据仅提交到固定的身份认证地址。
    # 此引导会话不附带实验室资源 Token。
    return bool(re.fullmatch(r"/http/webvpn[0-9a-f]+(?:/.*)?", parts.path))


# 作用：为 VPN 网关的一次性授权生成稳定的消费标识。
# 参数：
#     url：待解析或请求的完整地址。
# 返回：由授权端点和 entoken 组成的标识；非该端点返回 None。
# 说明：授权端点必须恰有一个非空 entoken 和 redirect_url，否则抛出 AuthProtocolError。
# 说明：标识不受参数顺序和返回地址变化影响，避免同一网关凭据被变形重放。
def _vpn_authorization_key(url: str) -> str | None:
    """为 VPN 网关的一次性授权生成稳定的消费标识。"""
    parts = urlsplit(url)
    if parts._replace(query="", fragment="").geturl() != VPN_AUTHORIZATION:
        return None
    params = parse_qs(parts.query, keep_blank_values=True)
    for name in ("entoken", "redirect_url"):
        values = params.get(name, [])
        if len(values) != 1 or not values[0].strip():
            raise AuthProtocolError("VPN 网关授权地址缺少有效 entoken 或 redirect_url 参数")
    return VPN_AUTHORIZATION + "?" + urlencode({"entoken": params["entoken"][0]})


# 作用：发送认证阶段请求并将传输失败转换为安全的认证异常。
# 参数：
#     session：本轮使用的 HTTP 会话，携带 Cookie 和共享脱敏上下文。
#     method：HTTP 方法；POST 和关闭 GET 重试的请求按一次性操作处理。
#     url：待解析或请求的完整地址。
#     stage：用于日志及安全错误说明的当前认证阶段名称。
#     **kwargs：传给会话的请求选项，可包含请求体、查询参数及 retry_get。
# 返回：状态通过初步检查的响应，由调用方继续解析并关闭。
# 说明：登记地址中的票据和会话标识；认证失败、异常 HTTP 状态或 POST 重定向时，
#     关闭响应并抛出对应认证异常。
# 说明：POST 或一次性 GET 的网络异常报告结果不确定，其他网络故障报告服务不可用。
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


# 作用：逐跳校验并跟随认证重定向，限制循环和凭据重放。
# 参数：
#     session：本轮使用的 HTTP 会话，携带 Cookie 和共享脱敏上下文。
#     url：待解析或请求的完整地址。
#     stage：用于日志及安全错误说明的当前认证阶段名称。
#     via_vpn：是否允许 VPN 网关及映射地址。
#     consumed：跨认证阶段共享的已消费凭据集合；省略时在本次调用内新建。 默认值为 None。
# 返回：最后一个非重定向响应，由调用方关闭。
# 说明：最多接受 10 次跳转；同阶段按目标地址和实际发送 Cookie 判断是否取得进展。
# 说明：票据、CAS granting、资源校验及网关授权只发送一次；网关成功回到指定入口时可推进授权阶段。
# 说明：非法目标、循环、缺少 Location 或超过跳数时抛出 AuthProtocolError；中间响应及时关闭。
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
        # 网关授权可能只改变服务端状态，而不改变 Cookie。
        # 普通请求在同一授权阶段内按目标地址及实际发送的 Cookie 判断循环。
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
            # 仅此已校验的跳转允许在 Cookie 不变时重新进入 VPN 入口。
            # 授权阶段变化不重置跳转次数，也不解除已消费凭据的重放限制。
            gateway_authorized = True
    raise AssertionError("unreachable")


# 作用：校验认证 JSON 响应并提取成功结果对象。
# 参数：
#     response：待检查的 HTTP 响应，需为状态码 200 且包含有效 JSON。
#     stage：用于日志及安全错误说明的当前认证阶段名称。
#     redactor：本轮共享的脱敏上下文，用于登记秘密值和过滤诊断内容。
# 返回：成功响应中的 result 字典。
# 说明：验证 success、code 和 result，并登记可能出现的 Token 后再过滤 message。
# 说明：分别识别认证失效、凭据拒绝、人工验证及协议错误；响应由外层上下文负责关闭。
def validated_result(response: requests.Response, stage: str, redactor: Redactor) -> dict[str, Any]:
    """校验认证 JSON 响应并提取成功结果对象。"""
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


# 作用：按本次 service 动态选择应用并提交加密凭据。
# 参数：
#     session：本轮使用的 HTTP 会话，携带 Cookie 和共享脱敏上下文。
#     base：当前统一认证基址，可为直连身份站点或 VPN 映射地址。
#     service_id：从当前登录页面取得的服务标识，用于查询应用配置。
#     username：待加密的账号字符串。
#     password：待加密的原始密码，保留两侧空格。
# 返回：无返回值（None）；认证 Cookie 保留在传入会话中。
# 说明：优先使用 loginAppId，空值时使用 appId；应用缺失或服务拒绝时传播认证异常。
# 说明：直接身份站点返回的门户 Token 仅设置为身份站点 Cookie，不交给实验室业务。
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


# 作用：通过只读权限接口确认现有会话能访问实验室资源。
# 参数：
#     session：本轮使用的 HTTP 会话，携带 Cookie 和共享脱敏上下文。
#     api_base_url：与该会话匹配的实验室资源 API 基址。
#     redactor：本轮共享的脱敏上下文，用于登记秘密值和过滤诊断内容。
# 返回：验证成功时返回 None。
# 说明：HTTP 401/403 或跳转视为认证失效；成功 JSON 仍须包含列表类型的 menu。
# 说明：网络或响应异常转换为认证错误；所有响应均关闭，不刷新 Token 或重新登录。
def check_access(session: requests.Session, api_base_url: str, *, redactor: Redactor) -> None:
    """通过只读权限接口确认现有会话能访问实验室资源。"""
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


# 作用：建立资源会话及认证快照工厂，探测成功后交给调用方。
# 参数：
#     token：实验室资源 Token，必须是非空字符串；使用时去除两侧空白。
#     cookies：已有认证 Cookie 集合，由工厂复制适用于资源主机的条目。
#     environment：调用方显式选择的网络环境，用于确定直连或 VPN 链路。
#     redactor：本轮共享的脱敏上下文，用于登记秘密值和过滤诊断内容。
#     headers：附加资源请求头；省略时使用空集合，校园网另设置来源地址。 默认值为 None。
# 返回：包含主会话、资源基址和独立会话工厂的 AuthenticationResult。
# 说明：校验失败或中断时关闭已创建的会话和工厂；成功后资源所有权交给认证结果。
def create_result(
    token: str,
    cookies: RequestsCookieJar,
    *,
    environment: NetworkEnvironment,
    redactor: Redactor,
    headers: dict[str, str] | None = None,
) -> AuthenticationResult:
    """建立资源会话及认证快照工厂，探测成功后交给调用方。"""
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


# 作用：按选择的网络执行账号密码认证并验证实验室权限。
# 参数：
#     username：账号字符串；空白账号会被拒绝。
#     password：原始密码字符串；禁止空密码，保留两侧空白。
#     environment：调用方显式选择的网络环境，用于确定直连或 VPN 链路。
#     redactor：可复用的运行级脱敏上下文；未传入时新建。 默认值为 None。
#     session_factory：创建认证引导会话的可调用对象，支持注入离线测试会话。 默认值为 HttpSession。
# 返回：已验证的 AuthenticationResult；调用方在课程线程结束后关闭。
# 说明：校外依次建立 VPN、完成门户认证和映射 CAS 登录；校园网直接进行实验室 CAS 登录。
# 说明：动态查询应用配置，保留适用的网关 Cookie 和参数；票据交换仅尝试一次。
# 说明：无论成功、失败或中断，引导会话均关闭；只有通过权限探测才交接资源会话。
def authenticate(
    username: str,
    password: str,
    *,
    environment: NetworkEnvironment,
    redactor: Redactor | None = None,
    session_factory: Callable[..., HttpSession] = HttpSession,
) -> AuthenticationResult:
    """按选择的网络执行账号密码认证并验证实验室权限。"""
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
            # 保留校外链路第一阶段使用 JSESSIONID 作为会话标识的约定。
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


# 作用：在校园网直连条件下导入并验证实验室资源 Token。
# 参数：
#     token：用户提供的实验室资源 Token，不可使用身份门户 Token。
#     redactor：可复用的脱敏上下文；未传入时新建。 默认值为 None。
# 返回：只读权限探测通过的 AuthenticationResult。
# 说明：从空 Cookie 集合创建直连会话，不建立 VPN；无效 Token 或探测失败时抛出认证异常。
def import_token(token: str, *, redactor: Redactor | None = None) -> AuthenticationResult:
    """在校园网直连条件下导入并验证实验室资源 Token。"""
    redactor = redactor if redactor is not None else Redactor()
    redactor.remember(token)
    return create_result(
        token, RequestsCookieJar(), environment=NetworkEnvironment.INTRANET, redactor=redactor
    )
