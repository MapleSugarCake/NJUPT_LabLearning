"""离线验证强制重新提交的交互、筛选、完整流程及失败边界。"""

import io
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import make_response, success
from test_cli import Inputs, fake_auth_result

from labpass_cli import cli
from labpass_cli.config import RunSettings


@pytest.mark.parametrize("answer,enabled", [("", False), ("n", False), ("y", True), (" Y ", True)])
def test_custom_force_setting_and_prompt_order(monkeypatch, answer, enabled):
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    inputs = Inputs(["y", "n", "", "invalid", answer, ""])
    output = []
    assert cli.run_cli(input_fn=inputs, output_fn=output.append, stream=io.StringIO()) == 0
    assert execute.call_args.args[0] == RunSettings(force_resubmit=enabled)
    assert inputs.events == [
        "是否自定义设置？默认请选N[y/N]：",
        "是否开启 debug 日志？[y/N]：",
        "课程并发线程数 [1–4，默认 4]：",
        "是否强制重新提交所有课程？[y/N]：",
        "是否强制重新提交所有课程？[y/N]：",
        "网络环境 [1 校外 VPN（默认）/ 2 校园网直连]：",
    ]
    assert "请输入 y 或 n（回车默认 n）" in output


@pytest.mark.parametrize("answer", ["", "n"])
def test_default_settings_do_not_ask_for_force_mode(monkeypatch, answer):
    execute = Mock(return_value=0)
    monkeypatch.setattr(cli, "execute", execute)
    inputs = Inputs([answer, ""])
    assert cli.run_cli(input_fn=inputs, output_fn=lambda _: None) == 0
    assert execute.call_args.args[0].force_resubmit is False
    assert len(inputs.events) == 2


@pytest.mark.parametrize("error,code", [(EOFError(), 2), (KeyboardInterrupt(), 130)])
def test_force_prompt_interruption_precedes_authentication(monkeypatch, error, code):
    authenticate = Mock()
    monkeypatch.setattr(cli, "authenticate", authenticate)
    assert cli.run_cli(input_fn=Inputs(["y", "n", "", error]), output_fn=lambda _: None) == code
    authenticate.assert_not_called()


def course_record(course_id="list-course", *, finished=True, duration="123.456789", percent="100"):
    return {
        "id": course_id,
        "courseName": f"synthetic {course_id}",
        "isFinish": finished,
        "duration": duration,
        "watchDuration": percent,
    }


@pytest.fixture
def run_cli_business(monkeypatch):
    """只替换认证；使用真实课程客户端、线程池及离线传输。"""

    def run(force=True):
        authentication = fake_auth_result()
        monkeypatch.setattr(cli, "authenticate", Mock(return_value=authentication))
        stream = io.StringIO()
        code = cli.run_cli(
            input_fn=Inputs(["y", "n", "1", "y" if force else "n", "", "fake-user", "fake-pass"]),
            output_fn=lambda _: None,
            stream=stream,
        )
        assert authentication.session.closed
        return code, stream.getvalue()

    return run


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("states", [[], [True, True], [True, False, True]])
def test_course_selection_and_summary(run_cli_business, install_transport, force, states):
    records = [course_record(str(i), finished=state) for i, state in enumerate(states)]
    selected = [str(i) for i, state in enumerate(states) if force or not state]
    question_reads = []
    list_reads = 0

    def handle(session, request, kwargs):
        nonlocal list_reads
        url = urlsplit(request.url)
        if url.path.endswith("myCourseList"):
            list_reads += 1
            # 重复记录仍只处理一次；回读新增课程不会进入本轮任务。
            rows = (
                records + records[:1]
                if list_reads == 1
                else [course_record(str(i)) for i in range(len(states))]
                + [course_record("late-course")]
            )
            return make_response(success(rows))
        if request.method == "GET":
            question_reads.append(parse_qs(url.query)["id"][0])
            return make_response(success([]))
        return make_response(success())

    calls = install_transport(handle)
    code, output = run_cli_business(force)
    assert code == 0
    assert question_reads == selected
    assert list_reads == 1 + len(selected)
    assert sum(request.method == "POST" for _, request, _ in calls) == 2 * len(selected)
    assert f"发现 {len(states)} 门课程：{sum(states)} 门已完成，{len(selected)} 门待处理" in output
    assert f"跳过 {len(states) - len(selected)}，成功 {len(selected)}，失败 0" in output
    assert ("强制重新提交所有课程模式已启用" in output) is force
    assert all(session.closed for session, _, _ in calls)


@pytest.mark.parametrize("question_count", [0, 2])
def test_finished_course_full_sequence_and_payload(
    run_cli_business, install_transport, question_count
):
    events = []

    def handle(session, request, kwargs):
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        body = json.loads(request.body) if request.body else None
        events.append((request.method, endpoint, body))
        if endpoint == "myCourseList":
            return make_response(success([course_record()]))
        if request.method == "GET":
            assert parse_qs(urlsplit(request.url).query) == {"id": ["list-course"]}
            return make_response(
                success(
                    [
                        {
                            "id": f"relation-{i}",
                            "courseId": "question-course",
                            "questionId": f"source-{i}",
                            "correctAnswer": " A, B " if i == 0 else "AB",
                        }
                        for i in range(question_count)
                    ]
                )
            )
        return make_response(success())

    calls = install_transport(handle)
    code, output = run_cli_business()
    assert code == 0
    assert events == [
        ("GET", "myCourseList", None),
        ("GET", "queryCourseQuestionRelaByMainId", None),
        *[
            (
                "POST",
                "submitAnswer",
                {
                    "questionId": f"relation-{i}",
                    "id": "question-course",
                    "option": ["A", "B"] if i == 0 else "AB",
                },
            )
            for i in range(question_count)
        ],
        ("POST", "finishRate", {"id": "list-course", "watchDuration": "123"}),
        ("POST", "finish", {"id": "list-course"}),
        ("GET", "myCourseList", None),
    ]
    assert "跳过 0，成功 1，失败 0" in output
    assert all(session.closed for session, _, _ in calls)


@pytest.mark.parametrize(
    "failure",
    [
        "duration",
        "questions",
        "answer-0",
        "answer-1",
        "video",
        "finish",
        "verify",
        "verify_incomplete",
        "verify_percent",
        "verify_unknown",
        "lock",
        "timeout",
        "expired",
        "verify_expired",
        "cancel",
        "interrupt",
    ],
)
def test_force_mode_preserves_failure_boundaries(
    monkeypatch,
    run_cli_business,
    install_transport,
    failure,
):
    coordinator = cli.MutationCoordinator()
    monkeypatch.setattr(cli, "MutationCoordinator", lambda: coordinator)
    events = []
    list_reads = 0

    def handle(session, request, kwargs):
        nonlocal list_reads
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "myCourseList":
            list_reads += 1
            if list_reads == 1:
                events.append("list")
                return make_response(
                    success(
                        [
                            course_record(
                                duration=None if failure == "duration" else "123.456789",
                            )
                        ]
                    )
                )
            action = "verify"
        elif request.method == "GET":
            action = "questions"
        elif endpoint == "submitAnswer":
            action = json.loads(request.body)["questionId"]
        else:
            action = "video" if endpoint == "finishRate" else "finish"
        events.append(action)
        if (
            failure == "expired"
            and action == "questions"
            or (failure == "verify_expired" and action == "verify")
        ):
            return make_response(status=401)
        if failure == "cancel" and action == "questions":
            coordinator.cancel()
        if failure == "interrupt" and action == "questions":
            raise KeyboardInterrupt
        if failure == "timeout" and action == "answer-0":
            raise requests.ReadTimeout("synthetic timeout")
        if failure == action or failure == "lock" and action == "answer-0":
            return make_response(
                {
                    "success": False,
                    "code": 400,
                    "message": (
                        "aquire lock fail" if failure == "lock" else "synthetic business failure"
                    ),
                }
            )
        if action == "questions":
            return make_response(
                success(
                    [
                        {
                            "id": f"answer-{i}",
                            "courseId": "question-course",
                            "questionId": f"source-{i}",
                            "correctAnswer": "A",
                        }
                        for i in range(2)
                    ]
                )
            )
        if action == "verify":
            return make_response(
                success(
                    [
                        course_record(
                            finished=failure != "verify_incomplete",
                            percent="80"
                            if failure == "verify_percent"
                            else ("invalid" if failure == "verify_unknown" else "100"),
                        )
                    ]
                )
            )
        return make_response(success())

    calls = install_transport(handle)
    code, output = run_cli_business()
    full = ["list", "questions", "answer-0", "answer-1", "video", "finish", "verify"]
    stop = {
        "duration": "list",
        "lock": "answer-0",
        "timeout": "answer-0",
        "expired": "questions",
        "cancel": "questions",
        "interrupt": "questions",
        "verify_expired": "verify",
        "verify_incomplete": "verify",
        "verify_percent": "verify",
        "verify_unknown": "verify",
    }.get(failure, failure)
    expected = full[: full.index(stop) + 1]
    if failure == "verify_percent":
        expected += ["finish", "verify"]
    assert events == expected
    assert code == (
        130
        if failure == "interrupt"
        else (2 if failure in {"expired", "verify_expired", "cancel"} else 1)
    )
    assert "成功 1" not in output
    if failure == "timeout":
        assert "不确定" in output
    assert all(session.closed for session, _, _ in calls)


@pytest.mark.parametrize("cancel", [False, True])
def test_force_mode_cancels_pending_finished_courses(
    monkeypatch,
    run_cli_business,
    install_transport,
    cancel,
):
    coordinator = cli.MutationCoordinator()
    monkeypatch.setattr(cli, "MutationCoordinator", lambda: coordinator)

    def handle(session, request, kwargs):
        if request.url.endswith("myCourseList"):
            return make_response(success([course_record(str(i)) for i in range(20)]))
        if cancel:
            coordinator.cancel()
            return make_response(success([]))
        return make_response(status=401)

    calls = install_transport(handle)
    code, output = run_cli_business()
    assert code == 2
    assert len(calls) == 2
    assert all(request.method == "GET" and session.closed for session, request, _ in calls)


def test_force_mode_course_failure_does_not_stop_other_courses(run_cli_business, install_transport):
    question_reads = []
    writes = []

    def handle(session, request, kwargs):
        if request.url.endswith("myCourseList"):
            return make_response(success([course_record("bad"), course_record("good")]))
        if request.method == "GET":
            course_id = parse_qs(urlsplit(request.url).query)["id"][0]
            question_reads.append(course_id)
            if course_id == "bad":
                return make_response({"success": False, "code": 400, "message": "bad course"})
            return make_response(success([]))
        writes.append(json.loads(request.body)["id"])
        return make_response(success())

    calls = install_transport(handle)
    code, output = run_cli_business()
    assert code == 1
    assert question_reads == ["bad", "good"]
    assert writes == ["good", "good"]
    assert "跳过 0，成功 1，失败 1" in output
    assert all(session.closed for session, _, _ in calls)
