"""离线验证重定向进展、一次性凭据和认证地址边界。

本文件定义：
    authorization：构造只使用虚构材料的 VPN 授权地址。
    redirect：构造可触发原生 Cookie 提取的跳转响应。
    test_cookies_not_sent_to_destination_do_not_count_as_progress：
        验证不适用于目标地址的 Cookie 不构成跳转进展。
    test_only_verified_gateway_response_advances_phase：验证非 302 网关返回不能推进授权阶段。
    test_unverified_return_does_not_advance_phase：
        验证未经指定授权跳转的入口返回仍受循环检测约束。
    test_gateway_progress_occurs_once_and_subsequent_loops_fail：
        验证网关授权阶段只推进一次，之后的相同状态循环被拒绝。
    test_cookie_and_gateway_progress_do_not_reset_hop_budget：
        验证 Cookie 变化或网关阶段进展不重置总跳转预算。
    test_gateway_parameters_must_be_unambiguous：验证网关凭据和返回地址必须各有一个非空值。
    test_gateway_credential_cannot_replay_across_stages_or_url_changes：
        验证同一网关凭据跨阶段或改写查询串后仍不能重放。
    test_single_use_authentication_requests_never_retry：
        验证票据、授权及 Token 交换类请求遇到故障也只有一次尝试。
    test_gateway_progress_never_releases_consumed_tickets：
        验证网关授权成功不会解除已经消费的票据限制。
    test_new_routes_preserve_host_path_and_network_restrictions：
        验证新增网关路由仍受协议、端口、主机和路径限制。
    test_resource_landing_retains_ticket_and_nested_service：
        验证实验室落地页尾斜杠差异不会丢失票据和嵌套服务参数。
    test_gateway_secret_is_registered_and_redacted_in_both_log_outputs：
        验证网关凭据及其 URL 编码在双日志出口的堆栈中均被脱敏。
"""

import io
import logging
from urllib.parse import quote, urlencode, urlsplit

import pytest
import requests
from conftest import cookie_response

from labpass_cli.logging_utils import configure_logging
from njupt_auth.auth import _follow
from njupt_auth.config import (
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
from njupt_auth.errors import AuthError, AuthProtocolError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession


# 作用：构造只使用虚构材料的 VPN 授权地址。
# 参数：
#     token：模拟的一次性网关凭据。 默认值为 'fake+authorization/key'。
#     target：网关授权后应跳转的目标地址。 默认值为 VPN_PRELOGIN_URL。
# 返回：包含 redirect_url 和 entoken 的编码地址字符串。
def authorization(token="fake+authorization/key", target=VPN_PRELOGIN_URL):
    return VPN_AUTHORIZATION + "?" + urlencode({"redirect_url": target, "entoken": token})


# 作用：构造可触发原生 Cookie 提取的跳转响应。
# 参数：
#     target：响应指向的跳转目标。
#     status：模拟的 HTTP 重定向状态。 默认值为 302。
#     *cookies：作为多个 Set-Cookie 头写入的虚构 Cookie 字符串。
# 返回：带 Location、可选 Cookie 和指定状态的模拟 Response。
def redirect(target, *cookies, status=302):
    return cookie_response(status=status, headers={"Location": target}, cookies=cookies)


# 作用：验证不适用于目标地址的 Cookie 不构成跳转进展。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     cookie：不存在或域、路径不匹配的参数化 Cookie 头。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：相同入口应在一次请求后被识别为循环，并关闭已取得响应。
@pytest.mark.parametrize(
    "cookie",
    [None, "unrelated=fake; Domain=other.test; Path=/", "unrelated=fake; Path=/unrelated; Secure"],
)
def test_cookies_not_sent_to_destination_do_not_count_as_progress(install_cookie_transport, cookie):
    response = redirect(VPN_PRELOGIN_URL, *((cookie,) if cookie else ()))
    calls = install_cookie_transport(lambda *args: response)
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == 1 and response.close.called


# 作用：验证非 302 网关返回不能推进授权阶段。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     status：参数化的 301、303、307 或 308 状态。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：返回相同入口时仍判定为循环，总共只发送两个请求。
@pytest.mark.parametrize("status", [301, 303, 307, 308])
def test_only_verified_gateway_response_advances_phase(install_cookie_transport, status):
    responses = iter([redirect(authorization()), redirect(VPN_PRELOGIN_URL, status=status)])
    calls = install_cookie_transport(lambda *args: next(responses))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == 2


# 作用：验证未经指定授权跳转的入口返回仍受循环检测约束。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     use_authorization：是否在其他跳转之前先访问一次网关授权接口。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：即使曾经过授权端点，未经它直接以预期方式返回入口仍不能放宽循环检查。
@pytest.mark.parametrize("use_authorization", [False, True])
def test_unverified_return_does_not_advance_phase(install_cookie_transport, use_authorization):
    other = VPN_ORIGIN + "/enlink/other"
    responses = [redirect(other), redirect(VPN_PRELOGIN_URL)]
    if use_authorization:
        responses.insert(0, redirect(authorization()))
    remaining = iter(responses)
    calls = install_cookie_transport(lambda *args: next(remaining))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == len(responses)


# 作用：验证网关授权阶段只推进一次，之后的相同状态循环被拒绝。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     second_authorization：是否在已授权阶段再次经过使用不同凭据的授权地址。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：分别核对直接返回与再授权两种情况下的请求次数。
@pytest.mark.parametrize("second_authorization", [False, True])
def test_gateway_progress_occurs_once_and_subsequent_loops_fail(
    install_cookie_transport, second_authorization
):
    repeated = authorization("fake-second-key") if second_authorization else VPN_PRELOGIN_URL
    responses = iter(
        [
            redirect(authorization()),
            redirect(VPN_PRELOGIN_URL),
            redirect(repeated),
            redirect(VPN_PRELOGIN_URL),
        ]
    )
    calls = install_cookie_transport(lambda *args: next(responses))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == (4 if second_authorization else 3)


# 作用：验证 Cookie 变化或网关阶段进展不重置总跳转预算。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     gateway_progress：是否让第一次请求跳转到网关授权地址。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：连续改变 Cookie 的跳转最终仍在 11 次请求后报超过十次跳转。
@pytest.mark.parametrize("gateway_progress", [False, True])
def test_cookie_and_gateway_progress_do_not_reset_hop_budget(
    install_cookie_transport, gateway_progress
):
    # 作用：模拟持续推进 Cookie 的入口跳转及可选初次网关授权。
    # 参数：
    #     *args：模拟适配器传入的请求参数，本闭包只读取外层调用计数。
    # 返回：带递增虚构 Cookie 或授权目标的模拟重定向响应。
    def handler(*args):
        if gateway_progress and len(calls) == 1:
            return redirect(authorization())
        return redirect(VPN_PRELOGIN_URL, f"gateway=fake-{len(calls)}; Path=/; Secure")

    calls = install_cookie_transport(handler)
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="10"):
        _follow(session, VPN_PRELOGIN_URL, "synthetic", via_vpn=True)
    assert len(calls) == 11


# 作用：验证网关凭据和返回地址必须各有一个非空值。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     params：缺失、空白或重复关键字段的参数化查询串。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：所有非法输入都应在发送前抛出 AuthProtocolError，请求记录保持为空。
@pytest.mark.parametrize(
    "params",
    [
        "redirect_url=fake",
        "entoken=&redirect_url=fake",
        "entoken=+&redirect_url=fake",
        "entoken=fake&entoken=&redirect_url=fake",
        "entoken=fake&entoken=other&redirect_url=fake",
        "entoken=fake",
        "entoken=fake&redirect_url=",
        "entoken=fake&redirect_url=one&redirect_url=two",
    ],
)
def test_gateway_parameters_must_be_unambiguous(install_cookie_transport, params):
    calls = install_cookie_transport(lambda *args: pytest.fail("Invalid authorization requested"))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="授权地址"):
        _follow(session, VPN_AUTHORIZATION + "?" + params, "synthetic", via_vpn=True)
    assert not calls


# 作用：验证同一网关凭据跨阶段或改写查询串后仍不能重放。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     replayed：重复凭据构成的变体地址，可改变返回地址、编码或参数顺序。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：首次消费后即使 Cookie 变化，共享 consumed 集合仍阻止第二次请求。
@pytest.mark.parametrize(
    "replayed",
    [
        authorization(),
        authorization(target=VPN_ORIGIN + "/enlink/changed"),
        VPN_AUTHORIZATION + "?entoken=fake%2bauthorization%2fkey&redirect_url=fake&extra=fake",
    ],
)
def test_gateway_credential_cannot_replay_across_stages_or_url_changes(
    install_cookie_transport, replayed
):
    calls = install_cookie_transport(lambda *args: cookie_response(text="done"))
    consumed = set()
    with HttpSession() as session:
        _follow(session, authorization(), "first", via_vpn=True, consumed=consumed).close()
        session.cookies.set("gateway", "fake-new", domain="vpn.njupt.edu.cn", path="/")
        with pytest.raises(AuthProtocolError, match="循环"):
            _follow(session, replayed, "second", via_vpn=True, consumed=consumed)
    assert len(calls) == 1


# 作用：验证票据、授权及 Token 交换类请求遇到故障也只有一次尝试。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     url：参数化的认证地址或资源落地地址。
#     fault：读取超时、连接失败或可重试 HTTP 状态之一。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：即使服务端返回 Retry-After，一次性认证地址也不能再次请求。
@pytest.mark.parametrize(
    "url",
    [
        SSO_BASE + "/cas/granting",
        VPN_IDENTITY_BASE + "/cas/granting",
        VPN_CALLBACK + "?ticket=fake-ticket",
        VPN_API_BASE + VALIDATE_PATH + "?ticket=fake-ticket",
        authorization(),
    ],
)
@pytest.mark.parametrize("fault", ["timeout", "connection", 429, 500, 502, 503, 504])
def test_single_use_authentication_requests_never_retry(install_cookie_transport, url, fault):
    # 作用：按选定故障模拟一次性认证请求失败。
    # 参数：
    #     *args：模拟传输传入的位置参数，不影响当前故障选择。
    # 返回：指定状态的模拟响应，或抛出对应 requests 网络异常。
    def handler(*args):
        if fault == "timeout":
            raise requests.ReadTimeout("synthetic")
        if fault == "connection":
            raise requests.ConnectionError("synthetic")
        return cookie_response(status=fault, headers={"Retry-After": "0"})

    calls = install_cookie_transport(handler)
    with HttpSession() as session, pytest.raises(AuthError):
        _follow(session, url, "synthetic", via_vpn=True)
    assert len(calls) == 1


# 作用：验证网关授权成功不会解除已经消费的票据限制。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     url：参数化的认证地址或资源落地地址。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：流程回到原票据地址时必须在发送前拒绝，总请求数为三次。
@pytest.mark.parametrize(
    "url",
    [
        SSO_BASE + "/cas/granting",
        VPN_IDENTITY_BASE + "/cas/granting",
        VPN_ORIGIN + "/http/webvpnabcdef?ticket=fake-ticket",
        VPN_API_BASE + VALIDATE_PATH + "?ticket=fake-ticket",
    ],
)
def test_gateway_progress_never_releases_consumed_tickets(install_cookie_transport, url):
    responses = iter([redirect(authorization()), redirect(VPN_PRELOGIN_URL), redirect(url)])
    calls = install_cookie_transport(lambda *args: next(responses))
    with HttpSession() as session, pytest.raises(AuthProtocolError, match="循环"):
        _follow(session, url, "synthetic", via_vpn=True)
    assert len(calls) == 3


# 作用：验证新增网关路由仍受协议、端口、主机和路径限制。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     url：参数化的认证地址或资源落地地址。
#     via_vpn：本场景是否允许 VPN 路由。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：覆盖相似端点、恶意主机、地址用户信息及路径编码绕过，所有输入均不发出请求。
@pytest.mark.parametrize(
    "url,via_vpn",
    [
        (VPN_AUTHORIZATION, False),
        (VPN_AUTHORIZATION + "/", True),
        (VPN_AUTHORIZATION + "-other", True),
        (VPN_ORIGIN + "/engateway/api/other", True),
        (VPN_AUTHORIZATION.replace("https:", "http:"), True),
        (VPN_AUTHORIZATION.replace(":8443", ":8444"), True),
        (VPN_AUTHORIZATION.replace("vpn.njupt.edu.cn", "vpn.njupt.edu.cn.evil.test"), True),
        (VPN_AUTHORIZATION.replace("https://", "https://evil@"), True),
        (VPN_ORIGIN + "/http/webvpnabcdef-evil", True),
        (VPN_ORIGIN + "/http/webvpnabcdef/%2e%2e/other", True),
        (VPN_ORIGIN + "/http/webvpnabcdef/%252e%252e/other", True),
        ("http://i.njupt.edu.cn.evil.test/user-login", False),
        ("http://evil@i.njupt.edu.cn/user-login", False),
        ("http://i.njupt.edu.cn:8080/user-login", False),
        ("http://i.njupt.edu.cn/other", False),
        ("https://i.njupt.edu.cn/user-login/%2e%2e/other", False),
    ],
)
def test_new_routes_preserve_host_path_and_network_restrictions(
    install_cookie_transport, url, via_vpn
):
    calls = install_cookie_transport(lambda *args: pytest.fail("Unapproved request"))
    with HttpSession() as session, pytest.raises(AuthProtocolError):
        _follow(session, url, "synthetic", via_vpn=via_vpn)
    assert not calls


# 作用：验证实验室落地页尾斜杠差异不会丢失票据和嵌套服务参数。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     via_vpn：本场景是否允许 VPN 路由。
#     slash：参数化选择空字符串或尾斜杠。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：比较最终准备请求及查询串，确认允许落地地址时参数语义完整保留。
@pytest.mark.parametrize("via_vpn", [False, True])
@pytest.mark.parametrize("slash", ["", "/"])
def test_resource_landing_retains_ticket_and_nested_service(
    install_cookie_transport, via_vpn, slash
):
    root = VPN_ORIGIN + "/http/webvpnabcdef" if via_vpn else SERVICE_URL.rstrip("/")
    url = root + slash + "?ticket=fake-ticket&service=http%3A%2F%2Fexample.test%2F%3Fa%3Db%26c%3Dd"
    calls = install_cookie_transport(lambda *args: cookie_response(text="done"))
    with HttpSession() as session:
        _follow(session, url, "synthetic", via_vpn=via_vpn).close()
    assert len(calls) == 1 and calls[0][1].url == requests.Request("GET", url).prepare().url
    assert urlsplit(calls[0][1].url).query == urlsplit(url).query


# 作用：验证网关凭据及其 URL 编码在双日志出口的堆栈中均被脱敏。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：日志只写入测试临时目录；断言控制台和文件完全一致且不含虚构秘密值。
def test_gateway_secret_is_registered_and_redacted_in_both_log_outputs(
    install_cookie_transport, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    secret = "synthetic+gateway/key"
    stream = io.StringIO()
    redactor = Redactor()
    calls = install_cookie_transport(lambda *args: cookie_response(text="done"))
    with configure_logging(True, redactor=redactor, stream=stream):
        with HttpSession(redactor=redactor) as session:
            _follow(session, authorization(secret), "synthetic", via_vpn=True).close()
        try:
            raise ValueError(secret + " " + quote(secret, safe=""))
        except ValueError:
            logging.getLogger("njupt_auth.test").debug("synthetic error", exc_info=True)
    output = stream.getvalue()
    assert len(calls) == 1 and "Traceback" in output
    assert secret not in output and quote(secret, safe="") not in output
    assert (tmp_path / "labpass_log.txt").read_text(encoding="utf-8") == output
