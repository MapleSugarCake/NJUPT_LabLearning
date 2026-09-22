"""离线验证认证、取消、超时和包依赖的跨模块边界。

本文件定义：
    test_auth_post_redirect_is_not_followed_or_retried：
        验证认证 POST 收到 307/308 后不跟随也不重发。
    test_application_id_fallback_and_challenge_classification：
        验证应用标识回退和需要验证码时的错误分类。
    test_none_timeout_still_has_connection_and_read_limits：
        验证显式传入 None 时仍使用默认连接和读取超时。
    test_keyboard_interrupt_cancels_scheduler_and_closes_sessions：
        验证完成回调中的中断会取消调度并关闭已开始的会话。
    test_architecture_has_no_reverse_imports_or_legacy_parser：
        通过源码语法树验证包依赖方向及旧接口清理约束。
"""

import ast
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests
from conftest import make_response, success
from test_auth import auth_server

from labpass_cli.runner import CourseRunner
from njupt_auth import NetworkEnvironment, authenticate
from njupt_auth.config import SSO_BASE
from njupt_auth.errors import AuthOutcomeUncertainError, InteractionRequiredError
from njupt_auth.redaction import Redactor
from njupt_auth.transport import HttpSession, ResourceSessionFactory
from njupt_safetylabpass import Course, MutationCoordinator, SafetyLabClient
from njupt_safetylabpass.exceptions import RunCancelledError


# 作用：验证认证 POST 收到 307/308 后不跟随也不重发。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     status：参数化的 307 或 308 重定向状态。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：预期结果不确定异常，并精确断言凭据 POST 只有一次。
@pytest.mark.parametrize("status", [307, 308])
def test_auth_post_redirect_is_not_followed_or_retried(install_transport, status):
    # 作用：仅把认证 POST 替换为保持方法的跳转响应。
    # 参数：
    #     session：模拟认证会话。
    #     request：待判定 HTTP 方法的准备请求。
    #     kwargs：透传给正常模拟认证服务的请求选项。
    # 返回：POST 返回指定重定向，其余请求交给正常模拟认证服务。
    def handler(session, request, kwargs):
        if request.method == "POST":
            return make_response(status=status, headers={"Location": SSO_BASE + "/ssoLogin/login"})
        return auth_server(session, request, kwargs)

    calls = install_transport(handler)
    with pytest.raises(AuthOutcomeUncertainError):
        authenticate(
            "synthetic-user", "synthetic-password", environment=NetworkEnvironment.INTRANET
        )
    assert sum(request.method == "POST" for _, request, _ in calls) == 1


# 作用：验证应用标识回退和需要验证码时的错误分类。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：配置只返回 appId，随后模拟人工挑战；断言抛出 InteractionRequiredError 并关闭引导会话。
def test_application_id_fallback_and_challenge_classification(install_transport):
    # 作用：模拟只有 appId 的应用配置及需要验证码的认证响应。
    # 参数：
    #     session：供其他阶段继续模拟认证的会话。
    #     request：按路径和方法区分配置查询与凭据提交的准备请求。
    #     kwargs：其他阶段交给 auth_server 的请求选项。
    # 返回：对应阶段的模拟 Response。
    def handler(session, request, kwargs):
        if urlsplit(request.url).path.endswith("queryLoginAllocationByService"):
            return make_response(success({"appId": "fake-lab-service-app"}))
        if request.method == "POST":
            return make_response({"success": False, "code": 400, "message": "需要验证码"})
        return auth_server(session, request, kwargs)

    calls = install_transport(handler)
    with pytest.raises(InteractionRequiredError):
        authenticate(
            "synthetic-user", "synthetic-password", environment=NetworkEnvironment.INTRANET
        )
    assert calls[-1][1].method == "POST" and calls[0][0].closed


# 作用：验证显式传入 None 时仍使用默认连接和读取超时。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：检查最终传输选项的 timeout 为 10/30 秒。
def test_none_timeout_still_has_connection_and_read_limits(install_transport):
    calls = install_transport(lambda *args: make_response({}))
    with HttpSession() as session:
        session.get("https://example.test/", timeout=None)
    assert calls[0][2]["timeout"] == (10.0, 30.0)


# 作用：验证完成回调中的中断会取消调度并关闭已开始的会话。
# 参数：
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：单线程提交多门课程，在回调注入 KeyboardInterrupt，随后检查协调器取消及会话关闭。
def test_keyboard_interrupt_cancels_scheduler_and_closes_sessions(install_transport):
    calls = install_transport(
        lambda session, request, kwargs: make_response(
            success([] if request.method == "GET" else None)
        )
    )
    base = "https://example.test/api"
    factory = ResourceSessionFactory(
        base, "fake-token", requests.cookies.RequestsCookieJar(), via_vpn=False, redactor=Redactor()
    )
    coordinator = MutationCoordinator()
    with SafetyLabClient(
        factory(), api_base_url=base, session_factory=factory, mutation_coordinator=coordinator
    ) as client:
        completed = Mock(side_effect=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            CourseRunner(client, 1, completed=completed).run(
                [Course(str(i), "synthetic") for i in range(20)]
            )
        with pytest.raises(RunCancelledError):
            coordinator.check_cancelled()
    assert all(session.closed for session, _, _ in calls)
    factory.close()


# 作用：通过源码语法树验证包依赖方向及旧接口清理约束。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：检查旧包目录、旧命令行解析器和反向导入，不导入或执行被检查模块的业务。
def test_architecture_has_no_reverse_imports_or_legacy_parser():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "labpass").exists()
    for package in ("njupt_auth", "njupt_safetylabpass", "labpass_cli"):
        for path in (root / package).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports.append(node.module.split(".")[0])
            assert "argparse" not in imports and "labpass" not in imports
            if package != "labpass_cli":
                assert "labpass_cli" not in imports
            if package == "njupt_auth":
                assert "njupt_safetylabpass" not in imports
