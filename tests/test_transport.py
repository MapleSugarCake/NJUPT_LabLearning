"""通过模拟发送验证精确重试边界和资源会话隔离。

本文件定义：
    test_get_retries_exact_statuses_at_most_three：验证指定暂时失败状态的 GET 最多尝试三次。
    test_other_statuses_never_retry_even_with_retry_after：
        验证未获准的 HTTP 状态不因 Retry-After 而增加重试。
    test_network_retry_boundaries：验证 GET 网络故障可重试，而证书错误和 POST 不能重试。
    test_post_never_retries_or_follows_redirects：
        验证 POST 即使显式请求跳转或遇到暂时失败也只发送一次。
    test_single_use_get_does_not_retry：验证显式关闭重试的一次性 GET 只尝试一次。
    test_session_factory_independence_cookie_attributes_and_scope：
        验证工厂副本资源独立、Cookie 属性完整和 API 地址范围受限。
    test_cookie_resolution_obeys_destination_and_path：
        验证同名 Cookie 按目标主机和最长匹配路径选择。
    test_direct_resource_does_not_add_vpn_params：
        验证校园网资源会话的 GET 与 POST 均不添加 VPN 查询参数。
"""

import pytest
import requests
from conftest import make_response

from njupt_auth.config import INTRANET_API_BASE, REQUEST_TIMEOUT, VPN_API_BASE
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession, ResourceSessionFactory, cookie_value


# 作用：验证指定暂时失败状态的 GET 最多尝试三次。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     status：参数化的 429、500、502、503 或 504 状态。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：精确检查退避序列、默认超时、禁止跳转及底层适配器不自动重试。
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_get_retries_exact_statuses_at_most_three(install_transport, status):
    calls = install_transport(lambda *args: make_response(status=status))
    sleeps = []
    with HttpSession(sleep=sleeps.append) as session:
        assert session.get("https://example.test/").status_code == status
        assert all(adapter.max_retries.total == 0 for adapter in session.adapters.values())
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    assert all(
        call[2]["timeout"] == REQUEST_TIMEOUT and call[2]["allow_redirects"] is False
        for call in calls
    )


# 作用：验证未获准的 HTTP 状态不因 Retry-After 而增加重试。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     status：成功、跳转、认证失败或其他非重试状态之一。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：模拟响应统一携带 Retry-After，仍断言只有一次请求。
@pytest.mark.parametrize("status", [200, 301, 401, 403, 404, 413, 501])
def test_other_statuses_never_retry_even_with_retry_after(install_transport, status):
    calls = install_transport(
        lambda *args: make_response(status=status, headers={"Retry-After": "0"})
    )
    with HttpSession(sleep=lambda _: None) as session:
        session.get("https://example.test/")
    assert len(calls) == 1


# 作用：验证 GET 网络故障可重试，而证书错误和 POST 不能重试。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     method：本用例发送的 HTTP 方法。
#     error：模拟传输抛出的请求异常类型。
#     count：该方法和异常组合预期的总尝试次数。
# 返回：无返回值（None）；测试函数通过断言验证预期。
@pytest.mark.parametrize(
    "method,error,count",
    [
        ("GET", requests.ConnectTimeout, 3),
        ("GET", requests.ReadTimeout, 3),
        ("GET", requests.ConnectionError, 3),
        ("GET", requests.exceptions.SSLError, 1),
        ("POST", requests.ConnectTimeout, 1),
        ("POST", requests.ReadTimeout, 1),
        ("POST", requests.ConnectionError, 1),
    ],
)
def test_network_retry_boundaries(install_transport, method, error, count):
    # 作用：在每次模拟发送时抛出指定网络异常。
    # 参数：
    #     *args：模拟发送传入的位置参数，仅用于兼容处理器调用。
    # 返回：不返回；抛出外层 error 参数指定的异常。
    def fail(*args):
        raise error("synthetic failure")

    calls = install_transport(fail)
    with HttpSession(sleep=lambda _: None) as session, pytest.raises(error):
        session.request(method, "https://example.test/")
    assert len(calls) == count


# 作用：验证 POST 即使显式请求跳转或遇到暂时失败也只发送一次。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     status：参数化的保持方法跳转、限流或服务器失败状态。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：传入 allow_redirects=True 仍由会话强制禁止自动跟随。
@pytest.mark.parametrize("status", [307, 308, 429, 500, 503])
def test_post_never_retries_or_follows_redirects(install_transport, status):
    calls = install_transport(
        lambda *args: make_response(status=status, headers={"Location": "https://other.test/"})
    )
    with HttpSession(sleep=lambda _: None) as session:
        session.post("https://example.test/", allow_redirects=True, json={"fake": "payload"})
    assert len(calls) == 1


# 作用：验证显式关闭重试的一次性 GET 只尝试一次。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：用 503 响应验证 retry_get=False 对票据交换类请求的保护。
def test_single_use_get_does_not_retry(install_transport):
    calls = install_transport(lambda *args: make_response(status=503))
    with HttpSession() as session:
        session.get("https://example.test/validateLogin", retry_get=False)
    assert len(calls) == 1


# 作用：验证工厂副本资源独立、Cookie 属性完整和 API 地址范围受限。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：检查会话、CookieJar、Cookie 对象及适配器不同，修改一份 Cookie 不影响另一份。
# 说明：同时验证 VPN 时间戳和写入参数、资源 Token 头、门户 Cookie 隔离，以及恶意地址拒绝和关闭后不
#     能创建。
def test_session_factory_independence_cookie_attributes_and_scope(install_transport):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("vpn_timestamp", "fake-timestamp", domain="vpn.njupt.edu.cn", path="/", secure=True)
    jar.set(
        "gateway",
        "fake-cookie",
        domain="vpn.njupt.edu.cn",
        path="/",
        secure=True,
        rest={"HttpOnly": True},
    )
    jar.set("tgc", "fake-identity-token", domain="i.njupt.edu.cn", path="/")
    redactor = Redactor()
    factory = ResourceSessionFactory(
        VPN_API_BASE, "fake-resource-token", jar, via_vpn=True, redactor=redactor
    )
    calls = install_transport(lambda *args: make_response({}))
    with factory() as first, factory() as second:
        assert isinstance(first, requests.Session)
        assert first is not second and first.cookies is not second.cookies
        assert first.get_adapter("https://") is not second.get_adapter("https://")
        first_cookie, second_cookie = list(first.cookies)[0], list(second.cookies)[0]
        assert first_cookie is not second_cookie
        assert first_cookie.secure and first_cookie.domain == "vpn.njupt.edu.cn"
        first_cookie.value = "changed"
        assert second_cookie.value == "fake-timestamp"
        second.get(VPN_API_BASE + "/resource", params={"id": "course-x"})
        second.post(VPN_API_BASE + "/write", json={"id": "course-x"})
        assert "_t=fake-timestamp" in calls[0][1].url
        assert calls[1][1].url.endswith("?enlink-vpn")
        assert calls[0][1].headers["X-Access-Token"] == "fake-resource-token"
        assert "tgc" not in calls[0][1].headers.get("Cookie", "")
        assert "X-Access-Token" not in second.headers
        for url in (
            "https://evil.test/",
            VPN_API_BASE + "/../portal",
            VPN_API_BASE + "/%2e%2e/portal",
        ):
            with pytest.raises(requests.exceptions.InvalidURL):
                second.get(url)
    assert first.closed and second.closed
    factory.close()
    with pytest.raises(RuntimeError):
        factory()


# 作用：验证同名 Cookie 按目标主机和最长匹配路径选择。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：同时设置其他主机、根路径及特定路径的虚构值，应选中特定路径的匹配项。
def test_cookie_resolution_obeys_destination_and_path():
    jar = requests.cookies.RequestsCookieJar()
    jar.set("JSESSIONID", "fake-other", domain="other.test", path="/")
    jar.set("JSESSIONID", "fake-root", domain="example.test", path="/")
    jar.set("JSESSIONID", "fake-specific", domain="example.test", path="/ssoLogin")
    assert cookie_value(jar, "JSESSIONID", "https://example.test/ssoLogin/index") == "fake-specific"


# 作用：验证校园网资源会话的 GET 与 POST 均不添加 VPN 查询参数。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：两次模拟请求的地址不含查询串，完成后关闭会话及认证快照工厂。
def test_direct_resource_does_not_add_vpn_params(install_transport):
    factory = ResourceSessionFactory(
        INTRANET_API_BASE,
        "fake-token",
        requests.cookies.RequestsCookieJar(),
        via_vpn=False,
        redactor=Redactor(),
    )
    calls = install_transport(lambda *args: make_response({}))
    with factory() as session:
        session.get(INTRANET_API_BASE + "/x")
        session.post(INTRANET_API_BASE + "/x")
    assert all("?" not in call[1].url for call in calls)
    factory.close()
