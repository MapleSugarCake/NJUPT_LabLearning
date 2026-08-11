from argparse import Namespace

import pytest
import requests

import labpass.cli as cli
from labpass.auth import AuthenticationResult
from labpass.config import INTRANET_API_ENDPOINTS
from labpass.exceptions import AuthenticationError
from labpass.models import Course, CourseResult, CourseStatus


class FakeClient:
    def __init__(self, courses: list[Course]) -> None:
        self.courses = courses

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def list_courses(self) -> list[Course]:
        return self.courses


def args() -> Namespace:
    return Namespace(workers=4, login_mode="auto", debug=False, no_pause=True)


def authentication() -> AuthenticationResult:
    return AuthenticationResult(requests.Session(), INTRANET_API_ENDPOINTS, "token")


def test_parser_rejects_more_than_four_workers() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["--workers", "5"])
    assert exc_info.value.code == 2


def test_execute_returns_zero_when_everything_is_already_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_authenticate", lambda *_: authentication())
    monkeypatch.setattr(
        cli,
        "LabPassClient",
        lambda *_: FakeClient([Course("1", "课程", finished=True)]),
    )

    assert cli.execute(args(), input_fn=lambda _: "", secret_input=lambda _: "") == 0


def test_execute_returns_one_when_a_course_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    course = Course("1", "课程")
    monkeypatch.setattr(cli, "_authenticate", lambda *_: authentication())
    monkeypatch.setattr(cli, "LabPassClient", lambda *_: FakeClient([course]))

    class FakeRunner:
        def __init__(self, *_: object) -> None:
            pass

        def run(self, _: list[Course]) -> list[CourseResult]:
            return [CourseResult(course, CourseStatus.FAILED, error="failed")]

    monkeypatch.setattr(cli, "CourseRunner", FakeRunner)

    assert cli.execute(args(), input_fn=lambda _: "", secret_input=lambda _: "") == 1


def test_execute_returns_two_when_authentication_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object) -> AuthenticationResult:
        raise AuthenticationError("invalid")

    monkeypatch.setattr(cli, "_authenticate", fail)

    assert cli.execute(args(), input_fn=lambda _: "", secret_input=lambda _: "") == 2


def test_auto_login_can_fall_back_to_manual_token(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = authentication()

    def fail_auto(*_: object) -> AuthenticationResult:
        raise AuthenticationError("changed")

    monkeypatch.setattr(cli, "_automatic_login", fail_auto)
    monkeypatch.setattr(cli, "_token_login", lambda _: expected)

    result = cli._authenticate("auto", lambda _: "y", lambda _: "secret")

    assert result is expected


def test_run_cli_returns_130_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(*_: object, **__: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "execute", interrupt)

    assert cli.run_cli([]) == 130
