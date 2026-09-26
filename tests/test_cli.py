"""使用可注入输入和模拟认证验证控制台流程及退出码。

本文件定义：
    Inputs：按预设序列提供控制台输入并记录提示。
    Inputs.__init__：保存输入迭代器及可选的共享提示记录。
    Inputs.__call__：记录当前提示并返回下一项预设输入。
    fake_auth_result：创建仅含虚构资源材料的认证结果用于交互测试。
    test_source_entrypoint_version_first_and_eof_before_network：
        验证源码入口应先输出版本并在首次输入结束时退出。
    test_version_and_first_prompt_then_default_settings：验证版本与首提示顺序以及默认运行设置。
    test_invalid_inputs_reprompt_and_custom_settings：
        验证非法选项重新询问并最终采用有效自定义设置。
    test_debug_log_conflict_fails_before_auth：验证已有日志文件导致认证前退出且文件字节保持不变。
    test_log_creation_error_fails_before_auth：验证日志目标不可创建时不会继续执行认证。
    test_password_uses_visible_input_without_stripping：验证密码走普通输入且保留两侧空白。
    test_password_failure_can_switch_to_hidden_token：
        验证账号密码失败后可切换为隐藏输入的校园网 Token。
    test_browser_failure_returns_to_fallback_choice：验证浏览器失败后重新显示回退菜单并允许退出。
    test_exit_codes_with_real_business_client：
        在模拟 HTTP 下用真实业务客户端验证各类执行结果的退出码。
    test_eof_and_interrupt_exit_codes：验证启动或网络选择阶段的 EOF 与中断退出码。
    test_login_failure_returns_two_without_credentials_in_output：
        验证登录失败返回 2 且诊断输出不包含已登记的密码。
    test_old_flags_are_not_parsed：验证旧命令行选项不会改变交互设置。
    test_entrypoint_pause_only_for_exe：验证只有冻结程序退出前等待回车。
    test_version_uses_source_and_frozen_metadata：验证源码读取项目版本而冻结程序读取发行元数据。
"""

import io
import runpy
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from conftest import make_response, success

from labpass_cli import cli
from labpass_cli.config import RunSettings
from labpass_cli.version import get_version
from njupt_auth import AuthenticationResult, AuthError, NetworkEnvironment
from njupt_auth.redaction import Redactor
from njupt_auth.transport import ResourceSessionFactory


# 作用：按预设序列提供控制台输入并记录提示。
# 说明：兼容 input 的可调用测试替身；序列元素可以是字符串或要抛出的异常。
# 说明：输入耗尽时转为 EOFError，使测试在认证前安全结束。
class Inputs:
    # 作用：保存输入迭代器及可选的共享提示记录。
    # 参数：
    #     self：当前实例。
    #     values：按调用顺序提供的输入字符串或异常对象序列。
    #     events：可复用的提示记录列表；省略时创建独立列表。 默认值为 None。
    # 返回：无返回值（None）。
    def __init__(self, values, events=None):
        self.values = iter(values)
        self.events = events if events is not None else []

    # 作用：记录当前提示并返回下一项预设输入。
    # 参数：
    #     self：当前实例。
    #     prompt：交互流程请求显示的提示文本。
    # 返回：下一个输入值；遇到异常对象时抛出该异常，耗尽时抛出 EOFError。
    def __call__(self, prompt):
        self.events.append(prompt)
        try:
            value = next(self.values)
        except StopIteration:
            raise EOFError from None
        if isinstance(value, BaseException):
            raise value
        return value


# 作用：创建仅含虚构资源材料的认证结果用于交互测试。
# 参数：无。
# 返回：拥有主资源会话及独立工厂的 AuthenticationResult。
# 说明：不执行认证或权限探测，后续课程请求仍须由模拟传输接管。
def fake_auth_result():
    base = "https://example.test/jeecg-boot"
    factory = ResourceSessionFactory(
        base, "fake-token", requests.cookies.RequestsCookieJar(), via_vpn=False, redactor=Redactor()
    )
    return AuthenticationResult(factory(), base, factory)


# 作用：验证源码入口应先输出版本并在首次输入结束时退出。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     capsys：pytest 标准输出捕获夹具，用于检查版本首行。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：直接运行入口脚本但注入立即结束的输入，断言退出码和首个提示；全程不进入真实认证。
def test_source_entrypoint_version_first_and_eof_before_network(monkeypatch, capsys):
    events = []
    monkeypatch.setattr("builtins.input", Inputs([], events))
    main = Path(__file__).resolve().parents[1] / "main.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(main), run_name="__main__")
    assert exit_info.value.code == 2
    assert capsys.readouterr().out.splitlines()[0] == "LabPass " + get_version()
    assert events == ["是否自定义设置？默认请选N[y/N]："]


# 作用：验证版本与首提示顺序以及默认运行设置。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：以模拟 execute 检查 debug 关闭、四线程和默认校外环境，同时确认没有创建日志文件。
def test_version_and_first_prompt_then_default_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    events = []
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    inputs = Inputs(["n", ""], events)
    assert cli.run_cli(input_fn=inputs, output_fn=events.append) == 0
    assert events[0] == "LabPass " + get_version()
    assert events[1] == "是否自定义设置？默认请选N[y/N]："
    assert execute.call_args.args[0] == RunSettings()
    assert execute.call_args.kwargs["environment"] is NetworkEnvironment.EXTRANET
    assert not (tmp_path / "labpass_log.txt").exists()


# 作用：验证非法选项重新询问并最终采用有效自定义设置。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：依次注入无效 y/n、线程数和网络选项，检查最终两线程、调试日志和校园网环境。
def test_invalid_inputs_reprompt_and_custom_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    inputs = Inputs(["bad", "Y", "bad", "Y", "0", "5", "abc", "2", "", "bad", "2"])
    assert cli.run_cli(input_fn=inputs, output_fn=lambda _: None, stream=io.StringIO()) == 0
    assert execute.call_args.args[0] == RunSettings(debug=True, workers=2)
    assert execute.call_args.kwargs["environment"] is NetworkEnvironment.INTRANET
    assert (tmp_path / "labpass_log.txt").exists()


# 作用：验证已有日志文件导致认证前退出且文件字节保持不变。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：在临时目录预先写入固定内容，断言 execute 未调用并返回退出码 2。
def test_debug_log_conflict_fails_before_auth(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "labpass_log.txt"
    path.write_bytes(b"existing-log")
    execute = Mock()
    monkeypatch.setattr(cli, "execute", execute)
    output = []
    inputs = Inputs(["y", "y", "", ""])
    assert cli.run_cli(input_fn=inputs, output_fn=output.append) == 2
    execute.assert_not_called()
    assert path.read_bytes() == b"existing-log"
    assert "已存在" in output[-1]


# 作用：验证日志目标不可创建时不会继续执行认证。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：用同名目录制造创建冲突，检查错误退出码及 execute 未调用。
def test_log_creation_error_fails_before_auth(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "labpass_log.txt").mkdir()
    execute = Mock()
    monkeypatch.setattr(cli, "execute", execute)
    assert cli.run_cli(input_fn=Inputs(["y", "y", "", ""]), output_fn=lambda _: None) == 2
    execute.assert_not_called()


# 作用：验证密码走普通输入且保留两侧空白。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：同时断言账号两侧空白被去除，隐藏输入函数完全未调用。
def test_password_uses_visible_input_without_stripping(monkeypatch):
    expected = object()
    auth = Mock(return_value=expected)
    secret = Mock(side_effect=AssertionError("password must not use getpass"))
    monkeypatch.setattr(cli, "authenticate", auth)
    result = cli._authenticate(
        NetworkEnvironment.INTRANET,
        Inputs([" synthetic-user ", " fake password "]),
        secret,
        Redactor(),
    )
    assert result is expected
    assert auth.call_args.args == ("synthetic-user", " fake password ")
    secret.assert_not_called()


# 作用：验证账号密码失败后可切换为隐藏输入的校园网 Token。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：使用模拟认证拒绝触发回退，检查隐藏输入及 Token 导入的调用参数。
def test_password_failure_can_switch_to_hidden_token(monkeypatch):
    monkeypatch.setattr(cli, "authenticate", Mock(side_effect=AuthError("synthetic rejection")))
    imported = Mock(return_value=object())
    monkeypatch.setattr(cli, "import_token", imported)
    secret = Mock(return_value="fake-hidden-token")
    result = cli._authenticate(
        NetworkEnvironment.EXTRANET,
        Inputs(["synthetic-user", "fake-password", "2"]),
        secret,
        Redactor(),
    )
    assert result is imported.return_value
    secret.assert_called_once()
    assert imported.call_args.args == ("fake-hidden-token",)


# 作用：验证浏览器失败后重新显示回退菜单并允许退出。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：浏览器只调用一次，随后选择退出得到用户未完成登录的 AuthError。
def test_browser_failure_returns_to_fallback_choice(monkeypatch):
    monkeypatch.setattr(cli, "authenticate", Mock(side_effect=AuthError("synthetic rejection")))
    browser = Mock(side_effect=AuthError("synthetic browser failure"))
    monkeypatch.setattr(cli, "authenticate_in_browser", browser)
    with pytest.raises(AuthError, match="用户未完成"):
        cli._authenticate(
            NetworkEnvironment.EXTRANET,
            Inputs(["synthetic-user", "fake-password", "1", "0"]),
            Mock(),
            Redactor(),
        )
    browser.assert_called_once()


# 作用：在模拟 HTTP 下用真实业务客户端验证各类执行结果的退出码。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     mode：参数化场景，涵盖已完成、空列表、成功、单课失败、认证失效及非法响应。
#     exit_code：当前场景预期的整数退出码。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：同时检查资源关闭、可完成运行的汇总以及单课程业务错误显示。
@pytest.mark.parametrize(
    "mode,exit_code",
    [
        ("finished", 0),
        ("empty", 0),
        ("success", 0),
        ("failure", 1),
        ("expired", 2),
        ("malformed", 2),
        ("video_pending", 1),
        ("verify_expired", 2),
        ("invalid_duration", 1),
        ("overflow_recovered", 0),
    ],
)
def test_exit_codes_with_real_business_client(monkeypatch, install_transport, mode, exit_code):
    result = fake_auth_result()
    monkeypatch.setattr(cli, "authenticate", Mock(return_value=result))
    finished = False
    finish_count = 0

    # 作用：根据执行场景构造课程列表、题目及完成接口的响应。
    # 参数：
    #     session：模拟传输入口传来的资源会话，本处理器不改变其 Cookie。
    #     request：待判定方法及接口路径的准备请求。
    #     kwargs：传输选项，当前场景只需兼容处理器签名。
    # 返回：当前 mode 对应的内存 Response。
    def handle(session, request, kwargs):
        nonlocal finished, finish_count
        if request.url.endswith("myCourseList"):
            if mode == "expired" or (mode == "verify_expired" and finished):
                return make_response(status=401)
            if mode == "malformed":
                return make_response(text="invalid JSON")
            return make_response(
                success(
                    []
                    if mode == "empty"
                    else [
                        {
                            "id": "course",
                            "courseName": "synthetic",
                            "isFinish": mode in {"finished", "video_pending"} or finished,
                            "duration": None if mode == "invalid_duration" else "123.456789",
                            "watchDuration": (
                                "100.10"
                                if mode == "overflow_recovered" and finish_count < 2
                                else "100.00"
                                if (mode == "finished" or finished) and mode != "video_pending"
                                else "0.00"
                            ),
                        }
                    ]
                )
            )
        if request.method == "GET":
            return make_response(success([]))
        if mode == "failure":
            return make_response(
                {"success": False, "code": 500, "message": "synthetic business failure"}
            )
        if request.url.endswith("/finish"):
            finished = True
            finish_count += 1
        return make_response(success())

    install_transport(handle)
    stream = io.StringIO()
    code = cli.run_cli(
        input_fn=Inputs(["n", "", "synthetic-user", "fake-password"]),
        output_fn=lambda _: None,
        stream=stream,
    )
    assert code == exit_code
    if mode == "overflow_recovered":
        assert finish_count == 2
    assert result.session.closed
    if exit_code in {0, 1}:
        assert "执行汇总" in stream.getvalue()
    if mode == "failure":
        assert "synthetic business failure" in stream.getvalue()
    if mode == "video_pending":
        assert "视频完成状态未确认" in stream.getvalue()
        assert "所有待处理课程均已完成" not in stream.getvalue()


# 作用：验证启动或网络选择阶段的 EOF 与中断退出码。
# 参数：
#     inputs：要顺序提供的输入与异常对象列表。
#     code：该输入序列预期的退出码，EOF 为 2，中断为 130。
# 返回：无返回值（None）；测试函数通过断言验证预期。
@pytest.mark.parametrize(
    "inputs,code",
    [
        ([EOFError()], 2),
        ([KeyboardInterrupt()], 130),
        (["n", EOFError()], 2),
        (["n", KeyboardInterrupt()], 130),
    ],
)
def test_eof_and_interrupt_exit_codes(inputs, code):
    assert (
        cli.run_cli(input_fn=Inputs(inputs), output_fn=lambda _: None, stream=io.StringIO()) == code
    )


# 作用：验证登录失败返回 2 且诊断输出不包含已登记的密码。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：模拟错误直接包含虚构密码，检查统一日志脱敏确实生效。
def test_login_failure_returns_two_without_credentials_in_output(monkeypatch):
    monkeypatch.setattr(cli, "authenticate", Mock(side_effect=AuthError("fake-password")))
    stream = io.StringIO()
    assert (
        cli.run_cli(
            input_fn=Inputs(["n", "", "synthetic-user", "fake-password", "0"]),
            output_fn=lambda _: None,
            stream=stream,
        )
        == 2
    )
    assert "fake-password" not in stream.getvalue()


# 作用：验证旧命令行选项不会改变交互设置。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：注入旧参数后仍按默认输入得到四线程配置，由模拟执行器避免业务请求。
def test_old_flags_are_not_parsed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--version", "--workers", "99"])
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    assert cli.run_cli(input_fn=Inputs(["n", ""]), output_fn=lambda _: None) == 0
    assert execute.call_args.args[0].workers == 4


# 作用：验证只有冻结程序退出前等待回车。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：分别模拟源码与 EXE 环境，检查 SystemExit 状态及暂停输入调用次数。
def test_entrypoint_pause_only_for_exe(monkeypatch):
    monkeypatch.setattr(cli, "run_cli", lambda: 1)
    pause = Mock(return_value="")
    monkeypatch.setattr("builtins.input", pause)
    with pytest.raises(SystemExit) as error:
        cli.entrypoint()
    assert error.value.code == 1
    pause.assert_not_called()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(SystemExit):
        cli.entrypoint()
    pause.assert_called_once()


# 作用：验证源码读取项目版本而冻结程序读取发行元数据。
# 参数：
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：源码预期值直接来自项目元数据，冻结分支替换元数据读取函数以保持离线。
def test_version_uses_source_and_frozen_metadata(monkeypatch):
    from labpass_cli import version as module

    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as file:
        expected = tomllib.load(file)["project"]["version"]
    assert get_version() == expected
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(module, "version", lambda name: "synthetic-metadata-version")
    assert get_version() == "synthetic-metadata-version"
