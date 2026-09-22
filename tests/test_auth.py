"""使用虚构协议响应验证账号密码认证和 Token 导入。

本文件定义：
    auth_server：根据请求路径模拟统一认证、VPN 和实验室认证服务。
    test_password_auth_complete_chain_and_ownership：
        验证校内外完整认证顺序、动态应用选择和资源所有权。
    test_auth_failures_close_all_sessions：逐类注入认证失败并检查所有已创建会话关闭。
    test_import_token_probes_and_is_intranet_only：
        验证导入 Token 去除空白、仅使用内网基址并探测权限。
    test_token_probe_requires_complete_success_response：
        拒绝缺少成功标记或有效权限列表的 Token 探测响应。
    test_unapproved_redirect_is_rejected_without_request：
        验证恶意主机或非获准路径不会收到后续认证请求。
    test_redirect_limit：验证认证重定向最多接受十次跳转。
    test_redirect_cycle_never_revisits_ticket_url：验证相同票据地址的循环跳转不会再次消费票据。
    test_token_exchange_timeout_has_uncertain_result_and_no_retry：
        验证资源 Token 交换超时被标为结果不确定且不重试。
    test_service_and_ticket_parsing_do_not_accept_ambiguous_values：
        验证片段中的 service、票据编码和非法服务参数解析。
    test_auth_errors_redact_known_values：验证认证错误不会暴露登记过的秘密值。
"""

import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests
from conftest import make_response, success

from njupt_auth import NetworkEnvironment, authenticate, import_token
from njupt_auth.auth import _follow, _service_id, _ticket
from njupt_auth.config import (
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
from njupt_auth.errors import AuthError, AuthOutcomeUncertainError, AuthProtocolError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession


# 作用：根据请求路径模拟统一认证、VPN 和实验室认证服务。
# 参数：
#     session：模拟请求关联的会话，可用来设置虚构 Cookie。
#     request：准备好的请求对象，用于检查认证地址、请求头及载荷。
#     kwargs：传输入口收到的请求选项，由模拟处理器接收。
# 返回：对应认证阶段的内存 Response。
# 说明：只使用虚构的账号、Token、票据及应用标识；断言门户材料不会作为资源认证头使用。
def auth_server(session, request, kwargs):
    """根据请求路径模拟统一认证、VPN 和实验室认证服务。"""
    url, method = request.url, request.method
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    path = parsed.path
    if url == VPN_PRELOGIN_URL:
        session.cookies.set("vpn_timestamp", "fake-vpn-time", domain="vpn.njupt.edu.cn", path="/")
        return make_response(text="vpn bootstrap")
    if path.endswith(PERMISSION_PATH):
        assert request.headers["X-Access-Token"] == "fake-lab-token"
        assert "fake-identity-token" not in request.headers.get("Cookie", "")
        return make_response(success({"menu": []}, code=0))
    if path.endswith(VALIDATE_PATH):
        assert query["service"] == [SERVICE_URL]
        assert query["ticket"] == ["fake-lab-ticket"]
        assert "X-Access-Token" not in request.headers
        return make_response(success({"token": "fake-lab-token"}))
    if path.endswith("/cas/login"):
        prefix = VPN_IDENTITY_BASE if url.startswith(VPN_ORIGIN) else SSO_BASE
        service_id = (
            "fake-vpn-service" if query["service"] == [VPN_CALLBACK] else "fake-lab-service"
        )
        return make_response(
            status=302, headers={"Location": prefix + "/user-login/#/login?service=" + service_id}
        )
    if "/user-login/" in path:
        return make_response(text="synthetic login page")
    if path.endswith("queryLoginAllocationByService"):
        return make_response(
            success({"appId": "common", "loginAppId": query["service"][0] + "-app"})
        )
    if path.endswith("/ssoLogin/login") and method == "POST":
        payload = json.loads(request.body)
        assert payload["appId"] in {"fake-vpn-service-app", "fake-lab-service-app"}
        session.cookies.set(
            "JSESSIONID", "fake-bootstrap-session", domain="i.njupt.edu.cn", path="/"
        )
        return make_response(success({"token": "fake-identity-token"}))
    if path.endswith("/ssoLogin/index"):
        if query["sessionId"] == ["fake-bootstrap-session"]:
            target = VPN_CALLBACK + "?ticket=fake-vpn-ticket"
        else:
            assert query["sessionId"] == ["fake-lab-service"]
            target = (
                VPN_ORIGIN + "/http/webvpnabcdef/" if url.startswith(VPN_ORIGIN) else SERVICE_URL
            )
            target += "?ticket=fake-lab-ticket"
        return make_response(status=302, headers={"Location": target})
    return make_response(text="synthetic authenticated landing page")


# 作用：验证校内外完整认证顺序、动态应用选择和资源所有权。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     environment：参数化选择的校园网或校外 VPN 环境。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：断言账号密码提交次数、加密载荷、引导会话关闭以及主会话与副本独立；结果关闭后工厂不可继续使
#     用。
@pytest.mark.parametrize("environment", list(NetworkEnvironment))
def test_password_auth_complete_chain_and_ownership(install_transport, environment):
    calls = install_transport(auth_server)
    result = authenticate("synthetic-user", "synthetic-password", environment=environment)
    assert result.api_base_url == (
        VPN_API_BASE if environment is NetworkEnvironment.EXTRANET else INTRANET_API_BASE
    )
    bootstrap = calls[0][0]
    assert bootstrap.closed
    assert not result.session.closed
    assert isinstance(result.session, requests.Session)
    posts = [request for _, request, _ in calls if request.method == "POST"]
    assert len(posts) == (2 if environment is NetworkEnvironment.EXTRANET else 1)
    expected_apps = ["fake-lab-service-app"]
    if environment is NetworkEnvironment.EXTRANET:
        expected_apps.insert(0, "fake-vpn-service-app")
    assert [json.loads(request.body)["appId"] for request in posts] == expected_apps
    assert all(json.loads(request.body)["password"] != "synthetic-password" for request in posts)
    clone = result.session_factory()
    assert clone is not result.session
    clone.close()
    result.close()
    assert result.session.closed
    with pytest.raises(RuntimeError):
        result.session_factory()
    assert "fake-lab-token" not in repr(result)


# 作用：逐类注入认证失败并检查所有已创建会话关闭。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     fault：参数化故障类型，覆盖缺少 Token、非法 JSON、权限失败、标识缺失及 POST 超时。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：POST 超时场景另断言凭据仅发送一次。
@pytest.mark.parametrize(
    "fault",
    [
        "missing_token",
        "bad_json",
        "probe_failure",
        "missing_service",
        "missing_ticket",
        "post_timeout",
    ],
)
def test_auth_failures_close_all_sessions(install_transport, fault):
    # 作用：在选定认证阶段替换响应或抛出超时异常。
    # 参数：
    #     session：模拟请求关联的会话，可用来设置虚构 Cookie。
    #     request：准备好的请求对象，用于检查认证地址、请求头及载荷。
    #     kwargs：传输入口收到的请求选项，由模拟处理器接收。
    # 返回：对应故障的模拟响应，或正常 auth_server 响应。
    # 说明：闭包读取 fault；POST 超时直接抛出 requests.Timeout。
    def handler(session, request, kwargs):
        path = urlsplit(request.url).path
        if fault == "missing_service" and "/user-login/" in path:
            return make_response(text="invalid", url=SSO_BASE + "/user-login/")
        if fault == "missing_ticket" and path.endswith("/ssoLogin/index"):
            return make_response(text="no ticket")
        if fault == "post_timeout" and request.method == "POST":
            raise requests.Timeout("synthetic timeout")
        if path.endswith(VALIDATE_PATH):
            if fault == "missing_token":
                return make_response(success({}))
            if fault == "bad_json":
                return make_response(text="not json")
        if fault == "probe_failure" and path.endswith(PERMISSION_PATH):
            return make_response(status=401)
        return auth_server(session, request, kwargs)

    calls = install_transport(handler)
    with pytest.raises(AuthError):
        authenticate(
            "synthetic-user", "synthetic-password", environment=NetworkEnvironment.INTRANET
        )
    assert all(session.closed for session, _, _ in calls)
    if fault == "post_timeout":
        assert sum(request.method == "POST" for _, request, _ in calls) == 1


# 作用：验证导入 Token 去除空白、仅使用内网基址并探测权限。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：断言资源 Token、来源头和单次探测请求，退出上下文后主会话关闭。
def test_import_token_probes_and_is_intranet_only(install_transport):
    calls = install_transport(lambda *args: make_response(success({"menu": []}, code=0)))
    with import_token("  fake-import-token  ") as result:
        assert result.api_base_url == INTRANET_API_BASE
        assert calls[0][1].headers["X-Access-Token"] == "fake-import-token"
        assert calls[0][1].headers["Origin"] == SERVICE_URL.rstrip("/")
    assert len(calls) == 1


# 作用：拒绝缺少成功标记或有效权限列表的 Token 探测响应。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     body：参数化的缺字段或类型错误的模拟 JSON。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：预期 AuthError，且探测失败的资源会话已关闭。
@pytest.mark.parametrize(
    "body", [success({}), success({"menu": "bad"}), {"code": 200, "result": {"menu": []}}]
)
def test_token_probe_requires_complete_success_response(install_transport, body):
    calls = install_transport(lambda *args: make_response(body))
    with pytest.raises(AuthError):
        import_token("fake-import-token")
    assert calls[0][0].closed


# 作用：验证恶意主机或非获准路径不会收到后续认证请求。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     url：参数化的未获准跳转目标。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：仅发送初始请求，目标在发送前被 AuthProtocolError 拒绝。
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/",
        "https://i.njupt.edu.cn.evil.test/cas/login",
        "http://i.njupt.edu.cn/arbitrary",
    ],
)
def test_unapproved_redirect_is_rejected_without_request(install_transport, url):
    calls = install_transport(lambda *args: make_response(status=302, headers={"Location": url}))
    with HttpSession() as session, pytest.raises(AuthProtocolError):
        _follow(session, SSO_BASE + "/cas/login", "synthetic", via_vpn=False)
    assert len(calls) == 1


# 作用：验证认证重定向最多接受十次跳转。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：模拟持续变化的跳转地址，断言初始请求加十次跳转共发送 11 次。
def test_redirect_limit(install_transport):
    calls = install_transport(
        lambda *args: make_response(
            status=302, headers={"Location": f"/cas/login?step={len(calls)}"}
        )
    )
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="10"):
        _follow(session, SSO_BASE + "/cas/login", "synthetic", via_vpn=False)
    assert len(calls) == 11


# 作用：验证相同票据地址的循环跳转不会再次消费票据。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：预期循环协议异常，模拟发送总数为一次。
def test_redirect_cycle_never_revisits_ticket_url(install_transport):
    url = SERVICE_URL + "?ticket=fake-ticket"
    calls = install_transport(lambda *args: make_response(status=302, headers={"Location": url}))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, url, "synthetic", via_vpn=False)
    assert len(calls) == 1


# 作用：验证资源 Token 交换超时被标为结果不确定且不重试。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：断言只请求一次 validateLogin 地址。
def test_token_exchange_timeout_has_uncertain_result_and_no_retry(install_transport):
    # 作用：仅在资源票据交换阶段模拟读取超时。
    # 参数：
    #     session：模拟请求关联的会话，可用来设置虚构 Cookie。
    #     request：准备好的请求对象，用于检查认证地址、请求头及载荷。
    #     kwargs：传输入口收到的请求选项，由模拟处理器接收。
    # 返回：其他阶段返回正常模拟认证响应；交换阶段抛出 ReadTimeout。
    def handler(session, request, kwargs):
        if urlsplit(request.url).path.endswith(VALIDATE_PATH):
            raise requests.ReadTimeout("synthetic timeout")
        return auth_server(session, request, kwargs)

    calls = install_transport(handler)
    with pytest.raises(AuthOutcomeUncertainError, match="不确定"):
        authenticate(
            "synthetic-user", "synthetic-password", environment=NetworkEnvironment.INTRANET
        )
    assert sum(VALIDATE_PATH in request.url for _, request, _ in calls) == 1


# 作用：验证片段中的 service、票据编码和非法服务参数解析。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：有效值应正确提取；缺失、重复或把地址当作服务标识的输入应被拒绝。
def test_service_and_ticket_parsing_do_not_accept_ambiguous_values():
    assert _service_id(SSO_BASE + "/user-login/#/login?service=fake-service") == "fake-service"
    assert _ticket(SERVICE_URL + "?" + urlencode({"ticket": "fake-ticket"})) == "fake-ticket"
    for url in (
        SSO_BASE,
        SSO_BASE + "?service=https://other.test",
        SSO_BASE + "?service=a&service=b",
    ):
        with pytest.raises(AuthProtocolError):
            _service_id(url)


# 作用：验证认证错误不会暴露登记过的秘密值。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：把虚构 Token 放入错误消息，断言用户可见异常已脱敏且请求只发送一次。
def test_auth_errors_redact_known_values(install_transport):
    calls = install_transport(
        lambda *args: make_response(
            {"success": False, "code": 500, "message": "synthetic-password"}
        )
    )
    with pytest.raises(AuthError) as captured:
        import_token("synthetic-password", redactor=Redactor())
    assert "synthetic-password" not in str(captured.value)
    assert len(calls) == 1
