"""通过临时 Edge 上下文获取并验证实验室认证材料。

本文件定义：
    matches_validation_response：判断响应是否准确匹配当前网络的实验室票据校验接口。
    _captured_token：从候选响应的成功 JSON 中取得资源 Token。
    _cookie_jar：将浏览器 Cookie 数据转换为独立的 requests CookieJar。
    authenticate_in_browser：由用户在临时 Edge 中登录，再交接验证后的资源会话。
"""

import time
from contextlib import suppress
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .auth import api_base, create_result
from .config import (
    BROWSER_TIMEOUT_SECONDS,
    SERVICE_URL,
    SSO_BASE,
    VALIDATE_PATH,
    VPN_CALLBACK,
    VPN_IDENTITY_BASE,
    VPN_ORIGIN,
)
from .errors import AuthError, BrowserUnavailableError
from .models import AuthenticationResult, NetworkEnvironment
from .redaction import Redactor


# 作用：判断响应是否准确匹配当前网络的实验室票据校验接口。
# 参数：
#     response：本次浏览器上下文收到的响应对象，提供地址、请求方法和状态。
#     environment：用户明确选择的校园网或校外 VPN 环境。
# 返回：GET、HTTP 200、目标地址、service 和非空唯一票据均匹配时为 True。
# 说明：门户响应、错误端口、错误路径或其他 service 均不作为资源认证来源。
def matches_validation_response(response: Any, environment: NetworkEnvironment) -> bool:
    """判断响应是否准确匹配当前网络的实验室票据校验接口。"""
    target = urlsplit(response.url)
    expected = urlsplit(api_base(environment) + VALIDATE_PATH)
    query = parse_qs(target.query)
    return (
        response.request.method == "GET"
        and response.status == 200
        and (target.scheme, target.netloc, target.path)
        == (expected.scheme, expected.netloc, expected.path)
        and query.get("service") == [SERVICE_URL]
        and len(query.get("ticket", [])) == 1
        and bool(query["ticket"][0].strip())
    )


# 作用：从候选响应的成功 JSON 中取得资源 Token。
# 参数：
#     response：本次浏览器上下文收到的响应对象，提供地址、请求方法和状态。
#     redactor：用于登记认证材料和过滤诊断的脱敏上下文。
# 返回：非空 Token 字符串；JSON、业务状态或 Token 无效时返回 None。
# 说明：有效 Token 与地址中的票据一并登记脱敏；仅解析材料，权限验证由交接流程完成。
def _captured_token(response: Any, redactor: Redactor) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if payload.get("success") is not True or str(payload.get("code")) not in {"0", "200"}:
        return None
    token = result.get("token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token.strip():
        return None
    redactor.remember(token, *parse_qs(urlsplit(response.url).query).get("ticket", []))
    return token


# 作用：将浏览器 Cookie 数据转换为独立的 requests CookieJar。
# 参数：
#     items：浏览器提供的 Cookie 字典列表，包含名称、值、作用域和过期信息。
# 返回：保留域、路径、安全标记及扩展属性的新 CookieJar。
# 说明：正数过期时间转为整数；会话 Cookie 保留为无固定过期时间。
def _cookie_jar(items: list[dict[str, Any]]) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for item in items:
        expires = item.get("expires", -1)
        jar.set_cookie(
            requests.cookies.create_cookie(
                name=item["name"],
                value=item["value"],
                domain=item["domain"],
                path=item["path"],
                secure=item.get("secure", False),
                expires=int(expires) if expires and expires > 0 else None,
                rest={"HttpOnly": item.get("httpOnly", False), "SameSite": item.get("sameSite")},
            )
        )
    return jar


# 作用：由用户在临时 Edge 中登录，再交接验证后的资源会话。
# 参数：
#     environment：用户明确选择的校园网或校外 VPN 环境。
#     redactor：可复用的脱敏上下文；未传入时新建。 默认值为 None。
# 返回：通过 requests 只读权限探测的 AuthenticationResult。
# 说明：按需导入 Playwright，使用本机 Edge 和非持久化上下文，最长等待 5 分钟。
# 说明：只观察当前上下文响应，复制资源 Cookie 与用户代理；不读取日常配置或保存认证材料。
# 说明：成功、取消或失败时均尝试关闭浏览器；依赖、驱动、关闭窗口及超时错误转为安全认证异常。
def authenticate_in_browser(
    *,
    environment: NetworkEnvironment,
    redactor: Redactor | None = None,
) -> AuthenticationResult:
    """由用户在临时 Edge 中登录，再交接验证后的资源会话。"""
    environment = NetworkEnvironment(environment)
    redactor = redactor if redactor is not None else Redactor()
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserUnavailableError("未安装浏览器组件；源码运行请安装 browser 可选依赖") from None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=False)
            try:
                context = browser.new_context(accept_downloads=False)
                page = context.new_page()
                captured: list[Any] = []
                vpn_ready = False

                # 作用：收集实验室校验响应并记录 VPN 回调是否完成。
                # 参数：
                #     response：本次浏览器上下文收到的响应对象，提供地址、请求方法和状态。
                # 返回：无返回值（None）；更新外层候选响应队列和 VPN 就绪标记。
                # 说明：回调只在当前临时上下文内注册；VPN 回调成功后允许重新访问实验室入口。
                def observe(response: Any) -> None:
                    nonlocal vpn_ready
                    if matches_validation_response(response, environment):
                        captured.append(response)
                    parts = urlsplit(response.url)
                    if (
                        f"{parts.scheme}://{parts.netloc}" == VPN_ORIGIN
                        and parts.path == urlsplit(VPN_CALLBACK).path
                        and 200 <= response.status < 400
                    ):
                        vpn_ready = True

                context.on("response", observe)
                identity = (
                    VPN_IDENTITY_BASE if environment is NetworkEnvironment.EXTRANET else SSO_BASE
                )
                lab_entry = identity + "/cas/login?" + urlencode({"service": SERVICE_URL})
                page.goto(lab_entry, wait_until="domcontentloaded", timeout=30_000)
                deadline = time.monotonic() + BROWSER_TIMEOUT_SECONDS
                resumed_lab = False
                while time.monotonic() < deadline:
                    if not browser.is_connected() or page.is_closed():
                        raise AuthError("浏览器登录已取消")
                    if captured:
                        response = captured.pop(0)
                        token = _captured_token(response, redactor)
                        if token:
                            base = api_base(environment)
                            cookies = _cookie_jar(context.cookies([base + VALIDATE_PATH]))
                            user_agent = page.evaluate("navigator.userAgent")
                            return create_result(
                                token,
                                cookies,
                                environment=environment,
                                redactor=redactor,
                                headers={"User-Agent": user_agent},
                            )
                    if vpn_ready and not resumed_lab:
                        resumed_lab = True
                        page.goto(lab_entry, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(200)
                raise AuthError("浏览器登录等待超过 5 分钟，请重新选择登录方式")
            finally:
                with suppress(PlaywrightError):
                    browser.close()
    except PlaywrightError:
        # Playwright 异常可能含有完整地址和请求头，因此只向外提供安全的固定说明。
        raise BrowserUnavailableError(
            "浏览器操作未完成，请确认已安装 Edge，或选择其他登录方式"
        ) from None
