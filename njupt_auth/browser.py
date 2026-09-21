"""Optional, temporary Edge login with narrowly scoped response capture."""

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


def matches_validation_response(response: Any, environment: NetworkEnvironment) -> bool:
    """Only accept the selected lab endpoint and service, never portal responses."""
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


def authenticate_in_browser(
    *,
    environment: NetworkEnvironment,
    redactor: Redactor | None = None,
) -> AuthenticationResult:
    """Let the user log in in temporary Edge; return a verified requests.Session."""
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
        # Playwright exceptions can include complete URLs and network headers.
        raise BrowserUnavailableError(
            "浏览器操作未完成，请确认已安装 Edge，或选择其他登录方式"
        ) from None
