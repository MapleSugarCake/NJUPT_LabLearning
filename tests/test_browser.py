"""使用模拟 Playwright 对象验证浏览器认证，不启动真实浏览器。

本文件定义：
    browser_response：构造来自实验室 validateLogin 的模拟浏览器响应。
    fake_browser：提供可模拟成功、取消、超时及驱动失败的浏览器安装器。
    test_browser_response_to_verified_independent_session：
        验证浏览器材料经过权限探测后交接为独立资源会话。
    test_capture_rejects_other_sources：逐项改变响应来源条件，验证不符合条件的响应被拒绝。
    test_missing_optional_dependency_is_recoverable：
        验证未安装 Playwright 时返回可恢复的安全认证异常。
    test_missing_edge_is_recoverable_and_does_not_expose_driver_error：
        验证 Edge 启动失败被转换为安全错误且不泄漏驱动内容。
    test_cancelled_browser_is_closed：验证用户关闭页面后认证报告取消并关闭浏览器。
    test_browser_timeout_is_bounded_and_closes_context：通过模拟时钟验证浏览器等待到期并清理。
    test_browser_probe_failure_closes_browser_and_session：
        验证取得 Token 但权限探测失败时同时关闭浏览器和资源会话。
    test_cookie_conversion_preserves_security_attributes：
        验证浏览器 Cookie 转换保留域、路径、过期时间和安全扩展属性。
"""

import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import urlencode

import pytest
from conftest import make_response, success

from njupt_auth import NetworkEnvironment, authenticate_in_browser
from njupt_auth.auth import api_base
from njupt_auth.browser import _cookie_jar, matches_validation_response
from njupt_auth.config import SERVICE_URL, VALIDATE_PATH, VPN_CALLBACK
from njupt_auth.errors import AuthError, BrowserUnavailableError


# 作用：构造来自实验室 validateLogin 的模拟浏览器响应。
# 参数：
#     environment：参数化选择的校园网或校外 VPN 环境。
#     service：模拟响应查询串中的目标 service。 默认值为 SERVICE_URL。
#     token：成功响应中使用的虚构资源 Token。 默认值为 'fake-browser-token'。
# 返回：含 URL、状态、请求方法及 JSON 读取函数的 SimpleNamespace。
def browser_response(environment, *, service=SERVICE_URL, token="fake-browser-token"):
    return SimpleNamespace(
        url=api_base(environment)
        + VALIDATE_PATH
        + "?"
        + urlencode({"service": service, "ticket": "fake-browser-ticket"}),
        status=200,
        request=SimpleNamespace(method="GET"),
        json=lambda: success({"token": token}),
    )


# 作用：提供可模拟成功、取消、超时及驱动失败的浏览器安装器。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：可按网络环境和故障选项安装模拟 Playwright 的函数。
# 说明：所有浏览器动作只更新内存状态，requests 权限探测另由模拟传输接管。
@pytest.fixture
def fake_browser(monkeypatch):
    # 作用：安装本场景的浏览器、上下文、页面和驱动替身。
    # 参数：
    #     environment：参数化选择的校园网或校外 VPN 环境。
    #     capture：是否在页面访问时发送可捕获的模拟认证响应。 默认值为 True。
    #     cancelled：页面是否报告已经关闭，用于模拟用户取消。 默认值为 False。
    #     failure：是否在启动驱动时抛出含虚构敏感信息的异常。 默认值为 False。
    #     vpn_redirect：是否让首次访问只产生 VPN 回调，默认 False。
    #         设为 True 时，认证流程需在回调后重新打开实验室入口。
    # 返回：记录关闭状态、访问地址、Cookie 请求和上下文选项的状态对象。
    # 说明：通过 monkeypatch 替换当前进程中的 playwright.sync_api 模块，测试后自动恢复。
    def install(environment, *, capture=True, cancelled=False, failure=False, vpn_redirect=False):
        state = SimpleNamespace(
            closed=False,
            goto=[],
            cookies_urls=[],
            options={},
            response=browser_response(environment),
        )

        # 作用：模拟 Playwright 的基础操作异常。
        # 说明：继承 Exception，仅在当前测试安装器中使用，用于检查驱动异常的安全转换。
        class BrowserError(Exception):
            pass

        # 作用：模拟认证页面的导航、关闭状态及等待接口。
        # 说明：所有操作只访问外层 state 和选项，页面不会加载真实网址。
        class Page:
            # 作用：记录导航地址并按场景触发 VPN 或资源认证响应。
            # 参数：
            #     self：当前实例。
            #     url：认证流程要求页面访问的地址，仅保存供断言。
            #     **kwargs：导航超时和等待条件等兼容参数，本替身不实际等待。
            # 返回：无返回值（None）；调用登记的响应观察器。
            def goto(self, url, **kwargs):
                state.goto.append(url)
                if vpn_redirect and len(state.goto) == 1:
                    state.observe(
                        SimpleNamespace(
                            url=VPN_CALLBACK, status=302, request=SimpleNamespace(method="GET")
                        )
                    )
                elif capture:
                    state.observe(state.response)

            # 作用：返回预设的页面取消状态。
            # 参数：
            #     self：当前实例。
            # 返回：外层 cancelled 选项的布尔值。
            def is_closed(self):
                return cancelled

            # 作用：模拟读取页面用户代理表达式。
            # 参数：
            #     self：当前实例。
            #     expression：应为 navigator.userAgent 的表达式，其他表达式导致断言失败。
            # 返回：固定的虚构浏览器用户代理字符串。
            def evaluate(self, expression):
                assert expression == "navigator.userAgent"
                return "synthetic-browser-agent"

            # 作用：提供不实际休眠的页面等待接口。
            # 参数：
            #     self：当前实例。
            #     duration：原页面接口要求等待的毫秒数，本模拟实现不使用。
            # 返回：无返回值（None）。
            def wait_for_timeout(self, duration):
                pass

        # 作用：模拟临时浏览器上下文和响应监听。
        # 说明：提供新页面、监听注册及资源 Cookie 列表，保留调用状态供测试断言。
        class Context:
            # 作用：创建当前模拟上下文的页面对象。
            # 参数：
            #     self：当前实例。
            # 返回：新的 Page 替身实例。
            def new_page(self):
                return Page()

            # 作用：登记认证流程使用的响应监听函数。
            # 参数：
            #     self：当前实例。
            #     event：应为 response 的事件名称。
            #     callback：用于接收模拟浏览器响应的观察函数。
            # 返回：无返回值（None）；把回调存入外层状态。
            def on(self, event, callback):
                assert event == "response"
                state.observe = callback

            # 作用：返回符合当前网络主机的虚构资源 Cookie。
            # 参数：
            #     self：当前实例。
            #     urls：请求筛选 Cookie 的地址列表，记录到状态以检查资源范围。
            # 返回：含域、路径、安全标记和会话过期信息的 Cookie 字典列表。
            def cookies(self, urls):
                state.cookies_urls.extend(urls)
                domain = (
                    "vpn.njupt.edu.cn"
                    if environment is NetworkEnvironment.EXTRANET
                    else "10.22.192.38"
                )
                return [
                    {
                        "name": "gateway",
                        "value": "fake-gateway-cookie",
                        "domain": domain,
                        "path": "/",
                        "secure": environment is NetworkEnvironment.EXTRANET,
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": -1,
                    }
                ]

        # 作用：模拟浏览器连接、上下文创建及关闭状态。
        # 说明：始终报告连接正常；用户取消由页面关闭选项模拟，关闭动作记录在 state 中。
        class Browser:
            # 作用：保存上下文创建选项并返回临时上下文替身。
            # 参数：
            #     self：当前实例。
            #     **kwargs：认证流程传入的上下文配置，测试检查其中禁止下载选项。
            # 返回：新的 Context 实例。
            def new_context(self, **kwargs):
                state.options = kwargs
                return Context()

            # 作用：模拟仍保持连接的浏览器驱动。
            # 参数：
            #     self：当前实例。
            # 返回：固定返回 True。
            def is_connected(self):
                return True

            # 作用：记录认证流程已经关闭浏览器。
            # 参数：
            #     self：当前实例。
            # 返回：无返回值（None）；将外层 closed 标记设为 True。
            def close(self):
                state.closed = True

        # 作用：模拟 Playwright 的 Chromium 启动入口。
        # 说明：验证只调用本机可见 Edge；可按场景注入驱动启动错误。
        class Chromium:
            # 作用：检查 Edge 启动配置后创建浏览器替身或模拟失败。
            # 参数：
            #     self：当前实例。
            #     **kwargs：启动配置，必须选择 msedge 通道并关闭无头模式。
            # 返回：成功时返回 Browser；失败场景抛出 BrowserError。
            def launch(self, **kwargs):
                assert kwargs == {"channel": "msedge", "headless": False}
                if failure:
                    raise BrowserError("synthetic error with ticket=secret")
                return Browser()

        # 作用：模拟 sync_playwright 返回的上下文管理器。
        # 说明：进入时提供 Chromium 替身，退出时不额外操作资源，由浏览器关闭状态验证清理。
        class Manager:
            # 作用：提供模拟的 Playwright 驱动对象。
            # 参数：
            #     self：当前实例。
            # 返回：含 Chromium 实例的 SimpleNamespace。
            def __enter__(self):
                return SimpleNamespace(chromium=Chromium())

            # 作用：兼容 Playwright 上下文退出协议。
            # 参数：
            #     self：当前实例。
            #     *args：上下文退出时传入的异常类型、对象和堆栈，本替身不使用。
            # 返回：None，不抑制测试或认证流程抛出的异常。
            def __exit__(self, *args):
                pass

        module = ModuleType("playwright.sync_api")
        module.Error = BrowserError
        module.sync_playwright = Manager
        monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
        return state

    return install


# 作用：验证浏览器材料经过权限探测后交接为独立资源会话。
# 参数：
#     fake_browser：模拟浏览器安装夹具，替换 Playwright 模块并返回可断言的状态。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     environment：参数化选择的校园网或校外 VPN 环境。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：校内外分别断言 Token、Cookie、用户代理、资源地址筛选、会话副本独立及浏览器关闭。
@pytest.mark.parametrize("environment", list(NetworkEnvironment))
def test_browser_response_to_verified_independent_session(
    fake_browser, install_transport, environment
):
    state = fake_browser(environment, vpn_redirect=environment is NetworkEnvironment.EXTRANET)
    calls = install_transport(lambda *args: make_response(success({"menu": []}, code=0)))
    with authenticate_in_browser(environment=environment) as result:
        assert state.closed
        assert calls[0][1].headers["X-Access-Token"] == "fake-browser-token"
        assert "gateway=fake-gateway-cookie" in calls[0][1].headers["Cookie"]
        assert calls[0][1].headers["User-Agent"] == "synthetic-browser-agent"
        assert state.options == {"accept_downloads": False}
        assert state.cookies_urls == [api_base(environment) + VALIDATE_PATH]
        with result.session_factory() as worker:
            assert worker.cookies is not result.session.cookies
    assert result.session.closed
    assert len(state.goto) == (2 if environment is NetworkEnvironment.EXTRANET else 1)


# 作用：逐项改变响应来源条件，验证不符合条件的响应被拒绝。
# 参数：
#     change：要破坏的条件：来源主机、端口、路径、service、方法、状态或票据。
# 返回：无返回值（None）；测试函数通过断言验证预期。
@pytest.mark.parametrize(
    "change", ["origin", "port", "path", "service", "method", "status", "ticket"]
)
def test_capture_rejects_other_sources(change):
    environment = NetworkEnvironment.EXTRANET
    response = browser_response(environment)
    if change == "origin":
        response.url = response.url.replace("vpn.njupt.edu.cn", "evil.test")
    elif change == "port":
        response.url = response.url.replace(":8443", ":443")
    elif change == "path":
        response.url = response.url.replace("/jeecg-boot/sys/", "/portal/")
    elif change == "service":
        response = browser_response(environment, service="https://i.njupt.edu.cn/portal/")
    elif change == "method":
        response.request.method = "POST"
    elif change == "status":
        response.status = 403
    else:
        response.url = response.url.replace("ticket=fake-browser-ticket", "ticket=")
    assert not matches_validation_response(response, environment)


# 作用：验证未安装 Playwright 时返回可恢复的安全认证异常。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：替换可选模块为不可用状态，不安装依赖或访问真实浏览器。
def test_missing_optional_dependency_is_recoverable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(BrowserUnavailableError, match="browser"):
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)


# 作用：验证 Edge 启动失败被转换为安全错误且不泄漏驱动内容。
# 参数：
#     fake_browser：模拟浏览器安装夹具，替换 Playwright 模块并返回可断言的状态。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：模拟驱动异常含虚构票据，检查外层错误不包含该值。
def test_missing_edge_is_recoverable_and_does_not_expose_driver_error(fake_browser):
    fake_browser(NetworkEnvironment.INTRANET, failure=True)
    with pytest.raises(BrowserUnavailableError) as error:
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)
    assert "secret" not in str(error.value)


# 作用：验证用户关闭页面后认证报告取消并关闭浏览器。
# 参数：
#     fake_browser：模拟浏览器安装夹具，替换 Playwright 模块并返回可断言的状态。
# 返回：无返回值（None）；测试函数通过断言验证预期。
def test_cancelled_browser_is_closed(fake_browser):
    state = fake_browser(NetworkEnvironment.INTRANET, cancelled=True)
    with pytest.raises(AuthError, match="取消"):
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)
    assert state.closed


# 作用：通过模拟时钟验证浏览器等待到期并清理。
# 参数：
#     fake_browser：模拟浏览器安装夹具，替换 Playwright 模块并返回可断言的状态。
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：没有认证响应时把单调时钟推进到五分钟之后，不执行真实等待。
def test_browser_timeout_is_bounded_and_closes_context(fake_browser, monkeypatch):
    state = fake_browser(NetworkEnvironment.INTRANET, capture=False)
    clock = iter([0, 301])
    monkeypatch.setattr("njupt_auth.browser.time.monotonic", lambda: next(clock))
    with pytest.raises(AuthError, match="5 分钟"):
        authenticate_in_browser(environment=NetworkEnvironment.INTRANET)
    assert state.closed


# 作用：验证取得 Token 但权限探测失败时同时关闭浏览器和资源会话。
# 参数：
#     fake_browser：模拟浏览器安装夹具，替换 Playwright 模块并返回可断言的状态。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：探测模拟 HTTP 401，不能把捕获到非空 Token 当作认证成功。
def test_browser_probe_failure_closes_browser_and_session(fake_browser, install_transport):
    state = fake_browser(NetworkEnvironment.EXTRANET)
    calls = install_transport(lambda *args: make_response(status=401))
    with pytest.raises(AuthError):
        authenticate_in_browser(environment=NetworkEnvironment.EXTRANET)
    assert state.closed and calls[0][0].closed


# 作用：验证浏览器 Cookie 转换保留域、路径、过期时间和安全扩展属性。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：精确断言 secure、HttpOnly 和 SameSite，不依赖真实浏览器材料。
def test_cookie_conversion_preserves_security_attributes():
    jar = _cookie_jar(
        [
            {
                "name": "fake",
                "value": "fake-value",
                "domain": ".example.test",
                "path": "/resource",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Strict",
                "expires": 1999999999,
            }
        ]
    )
    cookie = next(iter(jar))
    assert cookie.domain == ".example.test" and cookie.path == "/resource"
    assert cookie.secure and cookie.expires == 1999999999
    assert cookie.get_nonstandard_attr("HttpOnly") is True
    assert cookie.get_nonstandard_attr("SameSite") == "Strict"
