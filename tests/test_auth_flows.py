"""以严格请求序列和真实 Cookie 提取验证离线认证链路。

本文件定义：
    USER：离线认证测试使用的虚构账号。
    PASSWORD：带两侧空格的虚构测试密码。
    IDENTITY_TOKEN：模拟身份门户返回的 Token。
    LAB_TOKEN：模拟实验室认证返回的资源 Token。
    GUEST：模拟 VPN 访客会话 Cookie 的名称和值。
    ENS：模拟 VPN 网关会话 Cookie 的名称和值。
    GATEWAY：模拟 VPN 授权上下文 Cookie 的名称和值。
    VPN_COOKIES：完整模拟 VPN 上下文的 Cookie 字符串元组。
    query：为模拟地址追加正确编码的查询参数。
    redirect：构造带可选 Cookie 的模拟重定向响应。
    Step：描述严格离线认证序列中的一次预期请求。
    identity_entry：构造身份入口的协议升级和登录页面跳转步骤。
    identity_post：构造动态应用配置查询和加密凭据提交步骤。
    permission_step：构造资源会话只读权限探测的预期步骤。
    resource_steps：构造一次票据换 Token 和随后的权限探测步骤。
    campus_flow：组装校园网直连账号密码认证的完整模拟流程。
    vpn_flow：组装 VPN 建链、门户认证和映射实验室登录的完整流程。
    install_flow：安装按顺序消费 Step 并严格校验请求的模拟服务。
    test_strict_password_flows_probe_and_hand_over_independent_sessions：
        验证两种网络和应用字段选择下的完整认证及会话隔离。
    test_each_flow_failure_or_interrupt_releases_resources：
        在校内外流程每一步注入失败或中断并验证清理。
"""

import json
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import pytest
import requests
from conftest import cookie_response, success

from njupt_auth import NetworkEnvironment, authenticate, check_access
from njupt_auth.config import (
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
from njupt_auth.crypto import encrypt
from njupt_auth.errors import AuthError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import ResourceSessionFactory

# 离线认证测试使用的虚构账号。
# 仅用于构造和校验加密载荷，不来自真实用户凭据。
USER = "synthetic-user"
# 带两侧空格的虚构测试密码。
# 用来验证认证过程保留原始密码内容，不执行去空白处理。
PASSWORD = " synthetic password "
# 模拟身份门户返回的 Token。
# 用于断言门户 Cookie 与实验室资源 Token 的用途严格区分。
IDENTITY_TOKEN = "fake-identity-token"
# 模拟实验室认证返回的资源 Token。
# 只用于校验权限探测中的资源认证头。
LAB_TOKEN = "fake-lab-token"
# 模拟 VPN 访客会话 Cookie 的名称和值。
# 用于构造网关建链的第一阶段 Set-Cookie 及后续 Cookie 断言。
GUEST = "GUESTSESSIONID=fake-guest"
# 模拟 VPN 网关会话 Cookie 的名称和值。
# 用于验证网关阶段传递对应的会话材料。
ENS = "ENSSESSIONID=fake-ens"
# 模拟 VPN 授权上下文 Cookie 的名称和值。
# 在回调完成后加入请求，表示虚构网关上下文。
GATEWAY = "gateway_context=fake-context"
# 完整模拟 VPN 上下文的 Cookie 字符串元组。
# 由访客、网关会话和授权上下文三项组成，供严格请求序列复用。
VPN_COOKIES = (GUEST, ENS, GATEWAY)


# 作用：为模拟地址追加正确编码的查询参数。
# 参数：
#     url：待追加参数的地址。
#     **params：待编码的关键字查询参数；已有查询串时使用连接符追加。
# 返回：保留原查询串并追加参数后的地址字符串。
def query(url, **params):
    return url + ("&" if "?" in url else "?") + urlencode(params)


# 作用：构造带可选 Cookie 的模拟重定向响应。
# 参数：
#     location：Location 响应头中的跳转地址。
#     status：重定向 HTTP 状态码。 默认值为 302。
#     *cookies：作为多个 Set-Cookie 头输出的字符串。
# 返回：可执行原生 Cookie 提取的 Response。
def redirect(location, *cookies, status=302):
    return cookie_response(status=status, headers={"Location": location}, cookies=cookies)


# 作用：描述严格离线认证序列中的一次预期请求。
# 说明：带 slots 的数据类保存目标地址、响应或异常、请求方法及可选 Cookie 与应用标识断言。
# 说明：install_flow 按顺序消费这些步骤，用于发现多发、漏发或材料传递错误。
@dataclass(slots=True)
class Step:
    url: str
    response: requests.Response | BaseException
    method: str = "GET"
    cookies: tuple[str, ...] | None = None
    app_id: str | None = None


# 作用：构造身份入口的协议升级和登录页面跳转步骤。
# 参数：
#     base：当前模拟身份服务或资源服务的基址。
#     service：CAS 请求中的目标服务地址。
#     service_id：本阶段从登录入口取得的虚构服务标识。
#     cookie：入口响应应设置的虚构会话 Cookie。
# 返回：从 CAS 登录到 service 页面的一组 Step。
def identity_entry(base, service, service_id, cookie):
    login = query(base + "/user-login", service=service_id)
    location = login.replace(SSO_BASE, "http://i.njupt.edu.cn")
    return [
        Step(query(base + "/cas/login", service=service), redirect(location, cookie)),
        Step(login, redirect(query(base + "/user-login/", service=service_id), status=301)),
        Step(query(base + "/user-login/", service=service_id), cookie_response(text="login")),
    ]


# 作用：构造动态应用配置查询和加密凭据提交步骤。
# 参数：
#     base：当前模拟身份服务或资源服务的基址。
#     service_id：本阶段从登录入口取得的虚构服务标识。
#     app_id：该阶段最终应提交的应用标识。
#     fallback：是否模拟 loginAppId 为空并回退使用 appId。 默认值为 False。
# 返回：先读配置、再提交一次凭据的两个 Step。
# 说明：根据 fallback 选择应用字段，并按直连或映射身份站点设置不同 Cookie。
def identity_post(base, service_id, app_id, *, fallback=False):
    suffix = "?enlink-vpn" if base == VPN_IDENTITY_BASE else ""
    config = {"appId": app_id if fallback else "fake-unselected-app", "loginAppId": ""}
    if not fallback:
        config["loginAppId"] = app_id
    cookie = (
        "vpn_timestamp=fake-time-post; Path=/; Secure"
        if base == VPN_IDENTITY_BASE
        else f"tgc={IDENTITY_TOKEN}; Path=/; HttpOnly"
    )
    return [
        Step(
            query(
                base + "/AppLoginAllocation/queryLoginAllocationByService" + suffix,
                service=service_id,
            ),
            cookie_response(success(config)),
        ),
        Step(
            base + "/ssoLogin/login" + suffix,
            cookie_response(success({"token": IDENTITY_TOKEN}), cookies=(cookie,)),
            method="POST",
            app_id=app_id,
        ),
    ]


# 作用：构造资源会话只读权限探测的预期步骤。
# 参数：
#     base：当前模拟身份服务或资源服务的基址。
#     via_vpn：是否构造 VPN 链路的 Cookie 和查询参数。
# 返回：要求正确资源 Cookie 且返回空权限列表的 Step。
# 说明：VPN 场景同时要求最终时间戳查询参数及网关 Cookie。
def permission_step(base, *, via_vpn):
    url = base + PERMISSION_PATH
    cookies = ("lab_session=fake-lab-session",)
    if via_vpn:
        url = query(url, _t="fake-time-final")
        cookies = (*VPN_COOKIES, "vpn_timestamp=fake-time-final", *cookies)
    return Step(url, cookie_response(success({"menu": []}, code=0)), cookies=cookies)


# 作用：构造一次票据换 Token 和随后的权限探测步骤。
# 参数：
#     base：当前模拟身份服务或资源服务的基址。
#     via_vpn：是否构造 VPN 链路的 Cookie 和查询参数。
# 返回：资源校验及只读探测组成的 Step 列表。
# 说明：校验响应设置资源会话 Cookie，探测步骤验证该 Cookie 随请求传递。
def resource_steps(base, *, via_vpn):
    params = {"ticket": "fake-lab-ticket", "service": SERVICE_URL}
    cookies = ()
    if via_vpn:
        params["_t"] = "fake-time-final"
        cookies = (*VPN_COOKIES, "vpn_timestamp=fake-time-final")
    return [
        Step(
            query(base + VALIDATE_PATH, **params),
            cookie_response(
                success({"token": LAB_TOKEN}),
                cookies=("lab_session=fake-lab-session; Path=/; HttpOnly",),
            ),
            cookies=cookies,
        ),
        permission_step(base, via_vpn=via_vpn),
    ]


# 作用：组装校园网直连账号密码认证的完整模拟流程。
# 参数：
#     fallback：是否模拟 loginAppId 为空并回退使用 appId。 默认值为 False。
# 返回：包含身份登录、票据授予、资源交换及权限探测的有序 Step 列表。
# 说明：登录页包含 HTTP 到 HTTPS 的升级，实验室落地地址覆盖无尾斜杠形式。
def campus_flow(*, fallback=False):
    service_id = "fake-campus-service"
    landing = query(SERVICE_URL.rstrip("/"), ticket="fake-lab-ticket", service=SERVICE_URL)
    steps = identity_entry(
        SSO_BASE, SERVICE_URL, service_id, "JSESSIONID=fake-campus-session; Path=/"
    )
    steps += identity_post(SSO_BASE, service_id, "fake-campus-app", fallback=fallback)
    steps += [
        Step(
            query(SSO_BASE + "/ssoLogin/index", sessionId=service_id),
            redirect(query("http://i.njupt.edu.cn/cas/login", service=SERVICE_URL)),
            cookies=("JSESSIONID=fake-campus-session", "tgc=" + IDENTITY_TOKEN),
        ),
        Step(
            query(SSO_BASE + "/cas/login", service=SERVICE_URL),
            redirect("http://i.njupt.edu.cn/cas/granting"),
        ),
        Step(SSO_BASE + "/cas/granting", redirect(landing)),
        Step(landing, cookie_response(text="lab landing")),
    ]
    return steps + resource_steps(INTRANET_API_BASE, via_vpn=False)


# 作用：组装 VPN 建链、门户认证和映射实验室登录的完整流程。
# 参数：
#     fallback：是否模拟 loginAppId 为空并回退使用 appId。 默认值为 False。
# 返回：按实际接口形状构造、仅含虚构材料的有序 Step 列表。
# 说明：覆盖 Cookie 推进、网关授权阶段变化、两次动态应用选择及各阶段时间戳更新。
def vpn_flow(*, fallback=False):
    gateway = query(
        VPN_ORIGIN + "/enlink/sso/login", redirectUrl=VPN_PRELOGIN_URL, gate="fake-gate"
    )
    authorized = query(
        VPN_AUTHORIZATION, redirect_url=VPN_PRELOGIN_URL, entoken="fake+authorization/key"
    )
    callback = query(VPN_CALLBACK, ticket="fake-vpn-ticket", service=VPN_CALLBACK)
    steps = [
        Step(VPN_PRELOGIN_URL, redirect(VPN_PRELOGIN_URL, GUEST + "; Path=/; Secure"), cookies=()),
        Step(VPN_PRELOGIN_URL, redirect(gateway), cookies=(GUEST,)),
        Step(
            gateway,
            cookie_response(text="gateway", cookies=(ENS + "; Path=/; Secure; HttpOnly",)),
            cookies=(GUEST,),
        ),
    ]
    steps += identity_entry(
        SSO_BASE, VPN_CALLBACK, "fake-vpn-service", "JSESSIONID=fake-vpn-session; Path=/"
    )
    steps += identity_post(SSO_BASE, "fake-vpn-service", "fake-vpn-app", fallback=fallback)
    steps += [
        Step(
            query(SSO_BASE + "/ssoLogin/index", sessionId="fake-vpn-session"),
            redirect(query("http://i.njupt.edu.cn/cas/login", service=VPN_CALLBACK)),
            cookies=("JSESSIONID=fake-vpn-session", "tgc=" + IDENTITY_TOKEN),
        ),
        Step(
            query(SSO_BASE + "/cas/login", service=VPN_CALLBACK),
            redirect("http://i.njupt.edu.cn/cas/granting"),
        ),
        Step(SSO_BASE + "/cas/granting", redirect(callback)),
        Step(
            callback,
            redirect(VPN_PRELOGIN_URL, GATEWAY + "; Path=/; Secure; HttpOnly"),
            cookies=(GUEST, ENS),
        ),
        Step(VPN_PRELOGIN_URL, redirect(gateway), cookies=VPN_COOKIES),
        Step(gateway, redirect(authorized), cookies=VPN_COOKIES),
        # 模拟授权仅改变服务端状态，响应不设置新的 Cookie。
        Step(authorized, redirect(VPN_PRELOGIN_URL), cookies=VPN_COOKIES),
        Step(VPN_PRELOGIN_URL, cookie_response(text="authenticated gateway"), cookies=VPN_COOKIES),
    ]
    steps += identity_entry(
        VPN_IDENTITY_BASE,
        SERVICE_URL,
        "fake-mapped-service",
        "vpn_timestamp=fake-time-entry; Path=/; Secure",
    )
    steps += identity_post(
        VPN_IDENTITY_BASE, "fake-mapped-service", "fake-mapped-app", fallback=fallback
    )
    landing = query(
        VPN_ORIGIN + "/http/webvpnabcdef", ticket="fake-lab-ticket", service=SERVICE_URL
    )
    steps += [
        Step(
            query(VPN_IDENTITY_BASE + "/ssoLogin/index", sessionId="fake-mapped-service"),
            redirect(query(VPN_IDENTITY_BASE + "/cas/login", service=SERVICE_URL)),
            cookies=(*VPN_COOKIES, "vpn_timestamp=fake-time-post"),
        ),
        Step(
            query(VPN_IDENTITY_BASE + "/cas/login", service=SERVICE_URL),
            redirect(
                VPN_IDENTITY_BASE + "/cas/granting",
                "vpn_timestamp=fake-time-cas; Path=/; Secure",
            ),
        ),
        Step(
            VPN_IDENTITY_BASE + "/cas/granting",
            redirect(landing, "vpn_timestamp=fake-time-final; Path=/; Secure"),
            cookies=(*VPN_COOKIES, "vpn_timestamp=fake-time-cas"),
        ),
        Step(landing, cookie_response(text="mapped lab landing")),
    ]
    return steps + resource_steps(VPN_API_BASE, via_vpn=True)


# 作用：安装按顺序消费 Step 并严格校验请求的模拟服务。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     steps：预期请求步骤序列，可在指定位置注入响应错误或异常。
# 返回：请求记录列表和已返回响应列表组成的二元组。
def install_flow(install_cookie_transport, steps):
    remaining = iter(steps)
    responses = []

    # 作用：逐次检查地址、方法、超时、Cookie、Token 和加密载荷。
    # 参数：
    #     request：传输层准备完成的请求对象。
    #     kwargs：适配器接收的传输选项，用于断言固定连接和读取超时。
    # 返回：当前步骤的响应；步骤保存异常时直接抛出。
    # 说明：请求超出序列、凭据混用或载荷不一致时断言失败；响应另存以验证关闭。
    def handler(request, kwargs):
        step = next(remaining, None)
        assert step is not None, "Unexpected request after the authentication flow"
        assert request.url == requests.Request(step.method, step.url).prepare().url
        assert request.method == step.method
        assert kwargs["timeout"] == (10.0, 30.0)
        cookies = request.headers.get("Cookie", "")
        if step.cookies is not None:
            assert set(cookies.split("; ") if cookies else ()) == set(step.cookies)
        if urlsplit(request.url).path.endswith(PERMISSION_PATH):
            assert request.headers["X-Access-Token"] == LAB_TOKEN
        else:
            assert "X-Access-Token" not in request.headers
        if not request.url.startswith(SSO_BASE):
            assert IDENTITY_TOKEN not in cookies
        if step.app_id:
            assert json.loads(request.body) == {
                "checkKey": CHECK_KEY,
                "username": encrypt(USER),
                "password": encrypt(PASSWORD),
                "captchaVerification": None,
                "appId": step.app_id,
                "mode": "none",
            }
        if isinstance(step.response, BaseException):
            raise step.response
        responses.append(step.response)
        return step.response

    return install_cookie_transport(handler), responses


# 作用：验证两种网络和应用字段选择下的完整认证及会话隔离。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     environment：参数化选择的校园网直连或校外 VPN 环境。
#     fallback：是否模拟 loginAppId 为空并回退使用 appId。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：再从工厂创建会话执行权限探测，核对 Cookie 属性、适配器独立、全部响应关闭及工厂关闭后拒绝创
#     建。
@pytest.mark.parametrize("environment", list(NetworkEnvironment))
@pytest.mark.parametrize("fallback", [False, True])
def test_strict_password_flows_probe_and_hand_over_independent_sessions(
    install_cookie_transport, environment, fallback
):
    via_vpn = environment is NetworkEnvironment.EXTRANET
    base = VPN_API_BASE if via_vpn else INTRANET_API_BASE
    steps = (vpn_flow if via_vpn else campus_flow)(fallback=fallback)
    steps.append(permission_step(base, via_vpn=via_vpn))
    calls, responses = install_flow(install_cookie_transport, steps)
    redactor = Redactor()
    with authenticate(USER, PASSWORD, environment=environment, redactor=redactor) as result:
        assert result.api_base_url == base
        assert calls[0][0].closed
        assert isinstance(result.session, requests.Session) and not result.session.closed
        with result.session_factory() as clone:
            check_access(clone, base, redactor=redactor)
            assert clone is not result.session and clone.cookies is not result.session.cookies
            for main_cookie, clone_cookie in zip(
                result.session.cookies, clone.cookies, strict=True
            ):
                assert main_cookie is not clone_cookie
                assert vars(main_cookie) == vars(clone_cookie)
            for scheme in result.session.adapters:
                assert result.session.adapters[scheme] is not clone.adapters[scheme]
                assert clone.adapters[scheme].max_retries.total == 0
            clone.cookies.clear()
            assert result.session.cookies
    assert len(calls) == len(steps)
    assert all(session.closed for session, _, _ in calls)
    assert all(response.close.called for response in responses)
    assert all(not request.url.startswith("http://i.njupt.edu.cn") for _, request, _ in calls)
    with pytest.raises(RuntimeError):
        result.session_factory()


# 作用：在校内外流程每一步注入失败或中断并验证清理。
# 参数：
#     install_cookie_transport：离线适配器安装夹具，保留 requests 的真实 Cookie 提取过程。
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     environment：参数化选择的校园网直连或校外 VPN 环境。
#     index：参数化的故障步骤索引，从零开始。
#     interrupt：是否用 KeyboardInterrupt 替代 HTTP 403 故障。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：截断预期流程后断言请求没有继续，全部会话和返回响应已关闭，已创建的工厂不可继续使用。
@pytest.mark.parametrize(
    "environment,index",
    [
        (environment, index)
        for environment, flow in (
            (NetworkEnvironment.INTRANET, campus_flow),
            (NetworkEnvironment.EXTRANET, vpn_flow),
        )
        for index in range(len(flow()))
    ],
)
@pytest.mark.parametrize("interrupt", [False, True])
def test_each_flow_failure_or_interrupt_releases_resources(
    install_cookie_transport, monkeypatch, environment, index, interrupt
):
    steps = (vpn_flow if environment is NetworkEnvironment.EXTRANET else campus_flow)()[: index + 1]
    steps[-1].response = KeyboardInterrupt() if interrupt else cookie_response(status=403)
    factories = []

    # 作用：记录测试期间建立的真实资源会话工厂。
    # 参数：
    #     *args：原工厂构造函数的位置参数。
    #     **kwargs：原工厂构造函数的关键字参数。
    # 返回：按原参数构造的 ResourceSessionFactory。
    # 说明：闭包保存工厂实例，以便认证失败后检查关闭状态。
    def factory(*args, **kwargs):
        result = ResourceSessionFactory(*args, **kwargs)
        factories.append(result)
        return result

    monkeypatch.setattr("njupt_auth.auth.ResourceSessionFactory", factory)
    calls, responses = install_flow(install_cookie_transport, steps)
    with pytest.raises(KeyboardInterrupt if interrupt else AuthError):
        authenticate(USER, PASSWORD, environment=environment)
    assert len(calls) == len(steps)
    assert all(session.closed for session, _, _ in calls)
    assert all(response.close.called for response in responses)
    for created in factories:
        with pytest.raises(RuntimeError):
            created()
