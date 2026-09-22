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


class Inputs:
    def __init__(self, values, events=None):
        self.values = iter(values)
        self.events = events if events is not None else []

    def __call__(self, prompt):
        self.events.append(prompt)
        try:
            value = next(self.values)
        except StopIteration:
            raise EOFError from None
        if isinstance(value, BaseException):
            raise value
        return value


def fake_auth_result():
    base = "https://example.test/jeecg-boot"
    factory = ResourceSessionFactory(
        base, "fake-token", requests.cookies.RequestsCookieJar(), via_vpn=False, redactor=Redactor()
    )
    return AuthenticationResult(factory(), base, factory)


def test_source_entrypoint_version_first_and_eof_before_network(monkeypatch, capsys):
    events = []
    monkeypatch.setattr("builtins.input", Inputs([], events))
    main = Path(__file__).resolve().parents[1] / "main.py"
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(main), run_name="__main__")
    assert exit_info.value.code == 2
    assert capsys.readouterr().out.splitlines()[0] == "LabPass " + get_version()
    assert events == ["是否自定义设置？[y/N]："]


def test_version_and_first_prompt_then_default_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    events = []
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    inputs = Inputs(["n", ""], events)
    assert cli.run_cli(input_fn=inputs, output_fn=events.append) == 0
    assert events[0] == "LabPass " + get_version()
    assert events[1].startswith("是否自定义设置？[y/N]")
    assert execute.call_args.args[0] == RunSettings()
    assert execute.call_args.kwargs["environment"] is NetworkEnvironment.EXTRANET
    assert not (tmp_path / "labpass_log.txt").exists()


def test_invalid_inputs_reprompt_and_custom_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    inputs = Inputs(["bad", "Y", "bad", "Y", "0", "5", "abc", "2", "bad", "2"])
    assert cli.run_cli(input_fn=inputs, output_fn=lambda _: None, stream=io.StringIO()) == 0
    assert execute.call_args.args[0] == RunSettings(debug=True, workers=2)
    assert execute.call_args.kwargs["environment"] is NetworkEnvironment.INTRANET
    assert (tmp_path / "labpass_log.txt").exists()


def test_debug_log_conflict_fails_before_auth(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "labpass_log.txt"
    path.write_bytes(b"existing-log")
    execute = Mock()
    monkeypatch.setattr(cli, "execute", execute)
    output = []
    inputs = Inputs(["y", "y", ""])
    assert cli.run_cli(input_fn=inputs, output_fn=output.append) == 2
    execute.assert_not_called()
    assert path.read_bytes() == b"existing-log"
    assert "已存在" in output[-1]


def test_log_creation_error_fails_before_auth(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "labpass_log.txt").mkdir()
    execute = Mock()
    monkeypatch.setattr(cli, "execute", execute)
    assert cli.run_cli(input_fn=Inputs(["y", "y", ""]), output_fn=lambda _: None) == 2
    execute.assert_not_called()


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


@pytest.mark.parametrize(
    "mode,exit_code",
    [
        ("finished", 0),
        ("empty", 0),
        ("success", 0),
        ("failure", 1),
        ("expired", 2),
        ("malformed", 2),
    ],
)
def test_exit_codes_with_real_business_client(monkeypatch, install_transport, mode, exit_code):
    result = fake_auth_result()
    monkeypatch.setattr(cli, "authenticate", Mock(return_value=result))

    def handle(session, request, kwargs):
        if request.url.endswith("myCourseList"):
            if mode == "expired":
                return make_response(status=401)
            if mode == "malformed":
                return make_response(text="invalid JSON")
            return make_response(
                success(
                    []
                    if mode == "empty"
                    else [
                        {"id": "course", "courseName": "synthetic", "isFinish": mode == "finished"}
                    ]
                )
            )
        if request.method == "GET":
            return make_response(success([]))
        if mode == "failure":
            return make_response(
                {"success": False, "code": 500, "message": "synthetic business failure"}
            )
        return make_response(success())

    install_transport(handle)
    stream = io.StringIO()
    code = cli.run_cli(
        input_fn=Inputs(["n", "", "synthetic-user", "fake-password"]),
        output_fn=lambda _: None,
        stream=stream,
    )
    assert code == exit_code
    assert result.session.closed
    if exit_code in {0, 1}:
        assert "执行汇总" in stream.getvalue()
    if mode == "failure":
        assert "synthetic business failure" in stream.getvalue()


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


def test_old_flags_are_not_parsed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--version", "--workers", "99"])
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    assert cli.run_cli(input_fn=Inputs(["n", ""]), output_fn=lambda _: None) == 0
    assert execute.call_args.args[0].workers == 4


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


def test_version_uses_source_and_frozen_metadata(monkeypatch):
    from labpass_cli import version as module

    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as file:
        expected = tomllib.load(file)["project"]["version"]
    assert get_version() == expected
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(module, "version", lambda name: "synthetic-metadata-version")
    assert get_version() == "synthetic-metadata-version"
