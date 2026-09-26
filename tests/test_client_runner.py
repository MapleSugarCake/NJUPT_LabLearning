"""验证课程载荷、顺序、并发上限和取消语义的离线回归。

本文件定义：
    BASE：业务测试使用的虚构实验室 API 基址。
    client：为业务测试提供带认证工厂和共享协调器的真实客户端。
    test_course_normalization_dedup_and_finished_values：
        验证课程名称回退、完成标记解析及重复课程去重。
    test_three_question_ids_precise_payload_and_finish_id：
        验证三个题目标识与课程列表标识分别进入正确载荷。
    test_malformed_question_answers_are_rejected：验证缺失、空值、空选项或错误类型的答案被拒绝。
    test_lock_conflict_is_one_post：验证两种锁错误拼写在 HTTP 或业务错误下均只提交一次。
    test_uncertain_post_does_not_retry：验证写入连接失败或超时报结果不确定且不重发。
    test_authentication_failure_cancels_entire_run：
        验证 HTTP、业务认证失效及跳转都会阻止后续课程请求。
    test_course_response_validation：验证课程列表外层结构、结果类型及标识字段必须有效。
    test_course_sequence_and_no_finish_after_failure：验证逐题提交顺序及中途失败后不标记课程完成。
    test_empty_course_finishes_and_emits_progress：验证无题课程仍标记完成并发出开始进度事件。
    test_real_clients_have_overlapping_reads_serial_posts_and_independent_sessions：
        验证四课程读取可重叠、所有写入串行且会话各自独立。
    test_queued_write_checks_cancellation_after_acquiring_lock：
        验证已排队写入在取得锁后仍检查全局认证失效。
    test_runner_keeps_other_courses_after_failure：验证单门课程业务失败不影响其他课程完成。
    test_runner_rejects_invalid_thread_counts：验证零、负数、超上限、小数和布尔线程数均被拒绝。
    test_runner_global_expiry_stops_pending_courses：验证认证失效会取消尚未开始的课程任务。
    test_run_cancelled_before_empty_course_finish：验证读取空题列表后发生取消也不能继续完成写入。
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import make_response, success

from labpass_cli.runner import CourseRunner
from njupt_auth.redaction import Redactor
from njupt_auth.transport import ResourceSessionFactory
from njupt_safetylabpass import Course, MutationCoordinator, Question, SafetyLabClient, run_course
from njupt_safetylabpass.exceptions import (
    ApiError,
    AuthenticationExpiredError,
    LockConflictError,
    ResponseFormatError,
    RunCancelledError,
    SubmissionUncertainError,
)

# 业务测试使用的虚构实验室 API 基址。
# 采用 example.test 地址，所有请求仍必须由离线夹具模拟。
BASE = "https://example.test/jeecg-boot"


def video_course(course_id, name="synthetic"):
    return Course(course_id, name, duration_seconds=Decimal("123.456789"))


def completed_courses(*ids):
    return make_response(
        success([{"id": value, "isFinish": "1", "watchDuration": "100.00"} for value in ids])
    )


# 作用：为业务测试提供带认证工厂和共享协调器的真实客户端。
# 参数：无。
# 返回：产出一个 SafetyLabClient；测试结束后关闭主会话和工厂。
# 说明：Token 与 Cookie 均为虚构材料，未安装模拟传输时请求会被全局夹具阻断。
@pytest.fixture
def client():
    factory = ResourceSessionFactory(
        BASE,
        "fake-resource-token",
        requests.cookies.RequestsCookieJar(),
        via_vpn=False,
        redactor=Redactor(),
    )
    with SafetyLabClient(
        factory(),
        api_base_url=BASE,
        session_factory=factory,
        mutation_coordinator=MutationCoordinator(),
    ) as client:
        yield client
    factory.close()


# 作用：验证课程名称回退、完成标记解析及重复课程去重。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：同标识优先保留未完成记录，并检查首次出现顺序。
def test_course_normalization_dedup_and_finished_values(client, install_transport):
    install_transport(
        lambda *args: make_response(
            success(
                [
                    {"id": "one", "courseName": "first", "isFinish": True, "watchDuration": "100"},
                    {"id": "one", "name": "pending", "isFinish": False},
                    {"id": "two", "type_dictText": "safety", "isFinish": "1"},
                    {"id": "three", "isFinish": "false"},
                ]
            )
        )
    )
    courses = client.list_courses()
    assert [(course.id, course.finished) for course in courses] == [
        ("one", False),
        ("two", True),
        ("three", False),
    ]
    assert courses[0].name == "pending"


# 作用：验证三个题目标识与课程列表标识分别进入正确载荷。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     answer：参数化的原始字符串或列表答案。
#     expected：该答案规范化后预期提交的值。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：四个标识使用不同虚构值，精确检查答题的 questionId、id、option 及完成接口的 id。
@pytest.mark.parametrize(
    "answer,expected",
    [(" A ", "A"), ("AB", "AB"), (" A, B ", ["A", "B"]), ([" A", "B "], ["A", "B"])],
)
def test_three_question_ids_precise_payload_and_finish_id(
    client, install_transport, answer, expected
):
    # 作用：提供含三个不同标识的题目响应并接受后续模拟写入。
    # 参数：
    #     session：触发模拟传输的课程资源会话，可用于记录会话隔离。
    #     request：准备完成的 HTTP 请求，供检查方法、地址、参数及请求体。
    #     kwargs：模拟发送入口收到的传输选项。
    # 返回：GET 返回题目列表，POST 返回成功外层结构。
    # 说明：原始答案读取外层 answer 参数，不从课程列表标识重新构造题目标识。
    def handle(session, request, kwargs):
        if request.method == "GET":
            return make_response(
                success(
                    [
                        {
                            "id": "relation-id",
                            "courseId": "question-course-id",
                            "questionId": "source-bank-id",
                            "correctAnswer": answer,
                        }
                    ]
                )
            )
        return make_response(success())

    calls = install_transport(handle)
    questions = client.list_questions("list-course-id")
    assert questions[0].source_question_id == "source-bank-id"
    client.submit_answer(questions[0])
    client.finish_course("list-course-id")
    assert json.loads(calls[1][1].body) == {
        "questionId": "relation-id",
        "id": "question-course-id",
        "option": expected,
    }
    assert json.loads(calls[2][1].body) == {"id": "list-course-id"}


# 作用：验证缺失、空值、空选项或错误类型的答案被拒绝。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     answer：参数化的非法 correctAnswer 输入。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：预期 ResponseFormatError，错误数据不会进入写入步骤。
@pytest.mark.parametrize("answer", [None, "", "A,", [], ["A", 1], 7])
def test_malformed_question_answers_are_rejected(client, install_transport, answer):
    install_transport(
        lambda *args: make_response(
            success([{"id": "relation", "courseId": "course", "correctAnswer": answer}])
        )
    )
    with pytest.raises(ResponseFormatError):
        client.list_questions("course")


# 作用：验证两种锁错误拼写在 HTTP 或业务错误下均只提交一次。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     message：正确或历史拼写的锁冲突消息。
#     status：返回该锁错误的 HTTP 状态，可为 200 或 500。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：断言 LockConflictError 包含未自动重试说明，调用数精确为一次。
@pytest.mark.parametrize("message", ["acquire lock fail", "aquire lock fail"])
@pytest.mark.parametrize("status", [200, 500])
def test_lock_conflict_is_one_post(client, install_transport, message, status):
    calls = install_transport(
        lambda *args: make_response(
            {"success": False, "code": 500, "message": message}, status=status
        )
    )
    with pytest.raises(LockConflictError, match="未自动重试"):
        client.submit_answer(Question("relation", "course", "A", "source"))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "duration,expected",
    [
        ("123.456789", "123"),
        (42, "42"),
        (1.25, "1"),
        ("290.000", "290"),
        ("0.001", "0"),
        ("1E+2", "100"),
        ("100.000000000000000001", "100"),
    ],
)
def test_video_duration_uses_seconds_not_list_percentage(
    client, install_transport, duration, expected
):
    def handle(session, request, kwargs):
        if request.method == "GET":
            return make_response(
                success([{"id": "video-list-id", "duration": duration, "watchDuration": "12.50"}])
            )
        return make_response(success())

    calls = install_transport(handle)
    course = client.list_courses()[0]
    assert course.duration_seconds == Decimal(str(duration))
    assert course.video_percent == Decimal("12.50")
    client.submit_video_progress(course)
    assert len(calls) == 2
    assert urlsplit(calls[1][1].url).path.endswith("/finishRate")
    assert json.loads(calls[1][1].body) == {"id": "video-list-id", "watchDuration": expected}
    assert Decimal(expected) <= course.duration_seconds < Decimal(expected) + 1


@pytest.mark.parametrize("percent", ["100.01", "125.50", 101])
def test_video_overflow_percentage_is_preserved(client, install_transport, percent):
    install_transport(
        lambda *args: make_response(
            success([{"id": "video", "isFinish": "1", "watchDuration": percent}])
        )
    )
    course = client.list_courses()[0]
    assert course.video_percent == Decimal(str(percent))
    assert not course.video_finished


@pytest.mark.parametrize("first", ["100.01", "99.99", "0", "150"])
@pytest.mark.parametrize("second", ["100.00", "100.01", "99.99"])
def test_non_100_progress_gets_only_one_extra_finish(client, install_transport, first, second):
    reads = 0

    def handle(session, request, kwargs):
        nonlocal reads
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "myCourseList":
            reads += 1
            return make_response(
                success(
                    [
                        {
                            "id": "video",
                            "isFinish": "1",
                            "watchDuration": first if reads == 1 else second,
                        }
                    ]
                )
            )
        if request.method == "GET":
            return make_response(success([]))
        return make_response(success())

    calls = install_transport(handle)
    result = run_course(client, video_course("video"))
    assert result.succeeded is (second == "100.00")
    assert reads == 2
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == [
        "queryCourseQuestionRelaByMainId",
        "finishRate",
        "finish",
        "myCourseList",
        "finish",
        "myCourseList",
    ]
    if not result.succeeded:
        assert f"视频进度={second}%" in result.error


@pytest.mark.parametrize(
    "percent,flag,ok",
    [
        ("100", "1", True),
        ("100", "0", False),
        (None, "1", False),
        ("NaN", "1", False),
        ("bad", "1", False),
    ],
)
def test_unknown_or_exact_progress_does_not_trigger_extra_finish(
    client, install_transport, percent, flag, ok
):
    def handle(session, request, kwargs):
        if urlsplit(request.url).path.endswith("myCourseList"):
            return make_response(
                success([{"id": "video", "isFinish": flag, "watchDuration": percent}])
            )
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    assert run_course(client, video_course("video")).succeeded is ok
    assert len(calls) == 4


@pytest.mark.parametrize("fault", ["timeout", "lock", "expired", "cancel", "read_error"])
def test_second_finish_failure_stops_without_more_writes(client, install_transport, fault):
    finishes = reads = 0

    def handle(session, request, kwargs):
        nonlocal finishes, reads
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "finish":
            finishes += 1
            if finishes == 2:
                if fault == "timeout":
                    raise requests.Timeout("synthetic timeout")
                if fault == "lock":
                    return make_response(
                        {"success": False, "code": 500, "message": "aquire lock fail"}
                    )
                if fault == "expired":
                    return make_response(status=401)
        if endpoint == "myCourseList":
            reads += 1
            if fault == "cancel":
                client.mutation_coordinator.cancel()
            if reads == 2 and fault == "read_error":
                return make_response(text="invalid JSON")
            return make_response(
                success([{"id": "video", "isFinish": "1", "watchDuration": "101"}])
            )
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    if fault in {"expired", "cancel"}:
        error = AuthenticationExpiredError if fault == "expired" else RunCancelledError
        with pytest.raises(error):
            run_course(client, video_course("video"))
    else:
        result = run_course(client, video_course("video"))
        assert not result.succeeded
        assert result.uncertain is (fault == "timeout")
    assert finishes == (1 if fault == "cancel" else 2)
    assert reads == (2 if fault == "read_error" else 1)
    assert sum(urlsplit(request.url).path.endswith("finishRate") for _, request, _ in calls) == 1


def test_integer_seconds_contract_completes_after_single_progress_post(client, install_transport):
    """模拟整数字符串协议，防止重新把小数时长直接写入；不是服务端行为实测。"""
    progress_saved = False
    finished = False

    def handle(session, request, kwargs):
        nonlocal progress_saved, finished
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "finishRate":
            body = json.loads(request.body)
            if not isinstance(body["watchDuration"], str) or not re.fullmatch(
                r"[0-9]+", body["watchDuration"]
            ):
                return make_response({"success": False, "code": 500, "message": "aquire lock fail"})
            assert body == {"id": "video", "watchDuration": "123"}
            progress_saved = True
        elif endpoint == "finish":
            assert progress_saved
            finished = True
        elif endpoint == "myCourseList":
            assert finished
            return completed_courses("video")
        else:
            return make_response(success([]))
        return make_response(success())

    calls = install_transport(handle)
    result = run_course(client, video_course("video"))
    assert result.succeeded
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == [
        "queryCourseQuestionRelaByMainId",
        "finishRate",
        "finish",
        "myCourseList",
    ]


@pytest.mark.parametrize("status", [200, 500])
def test_video_error_diagnostics_are_bounded_and_redacted(
    client, install_transport, caplog, status
):
    client.redactor.remember("synthetic-sensitive-value")
    calls = install_transport(
        lambda *args: make_response(
            {
                "success": False,
                "code": 500,
                "message": "aquire lock fail; synthetic-sensitive-value; " + "x" * 1000,
            },
            status=status,
        )
    )
    with (
        caplog.at_level(logging.DEBUG, logger="njupt_safetylabpass.client"),
        pytest.raises(LockConflictError),
    ):
        client.submit_video_progress(video_course("video"))
    diagnostics = [
        record.getMessage() for record in caplog.records if "API 业务错误" in record.getMessage()
    ]
    assert len(diagnostics) == 1 and len(diagnostics[0]) < 600
    assert "aquire lock fail" in diagnostics[0] and "code=500" in diagnostics[0]
    assert "synthetic-sensitive-value" not in caplog.text
    assert "上报整数秒数=123" in caplog.text
    assert len(calls) == 1


def test_video_non_json_error_page_is_not_logged(client, install_transport, caplog):
    install_transport(
        lambda *args: make_response(text="<html>private-server-page</html>", status=500)
    )
    with (
        caplog.at_level(logging.DEBUG, logger="njupt_safetylabpass.client"),
        pytest.raises(ApiError),
    ):
        client.submit_video_progress(video_course("video"))
    assert "private-server-page" not in caplog.text


@pytest.mark.parametrize(
    "duration",
    [None, "", "bad", "NaN", "sNaN", "Infinity", "-Infinity", 0, -1, True, False, [], {}],
)
def test_invalid_duration_is_course_failure_before_any_write(client, install_transport, duration):
    calls = install_transport(
        lambda *args: make_response(
            success([{"id": "bad", "duration": duration}, {"id": "good", "duration": "20.5"}])
        )
    )
    bad, good = client.list_courses()
    assert bad.duration_seconds is None
    assert good.duration_seconds == Decimal("20.5")
    result = run_course(client, bad)
    assert not result.succeeded and "视频总时长" in result.error
    assert len(calls) == 1
    with pytest.raises(ResponseFormatError):
        client.submit_video_progress(bad)
    assert len(calls) == 1


@pytest.mark.parametrize("percent", [None, "", "bad", "NaN", "Infinity", -1, True, [], {}])
def test_invalid_video_percentage_never_finishes(client, install_transport, percent):
    install_transport(
        lambda *args: make_response(
            success([{"id": "course", "isFinish": "1", "watchDuration": percent, "duration": "20"}])
        )
    )
    course = client.list_courses()[0]
    assert course.video_percent is None
    assert not course.video_finished


@pytest.mark.parametrize(
    "flag,percent,expected",
    [
        ("1", "100.00", True),
        (True, 100, True),
        ("1", "99.99", False),
        (None, "100.00", False),
        ("0", "100.00", False),
        ("1", "0.00", False),
    ],
)
def test_video_completion_requires_both_fields(client, install_transport, flag, percent, expected):
    install_transport(
        lambda *args: make_response(
            success([{"id": "course", "isFinish": flag, "watchDuration": percent}])
        )
    )
    assert client.list_courses()[0].video_finished is expected


def test_duplicate_incomplete_video_takes_precedence(client, install_transport):
    install_transport(
        lambda *args: make_response(
            success(
                [
                    {"id": "course", "isFinish": "1", "watchDuration": "100"},
                    {"id": "course", "isFinish": "1", "watchDuration": "0"},
                ]
            )
        )
    )
    assert not client.list_courses()[0].video_finished


@pytest.mark.parametrize(
    "failure", ["rate", "finish", "pending", "missing", "malformed", "network", "expired"]
)
def test_video_failure_stops_writes_and_never_reports_success(client, install_transport, failure):
    def handle(session, request, kwargs):
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "myCourseList":
            if failure == "network":
                raise requests.ConnectionError("synthetic read failure")
            if failure == "expired":
                return make_response(status=401)
            if failure == "malformed":
                return make_response(text="not JSON")
            if failure == "missing":
                return completed_courses("different-course")
            return make_response(
                success([{"id": "video", "isFinish": "1", "watchDuration": "0.00"}])
            )
        if request.method == "GET":
            return make_response(success([]))
        if (endpoint == "finishRate" and failure == "rate") or (
            endpoint == "finish" and failure == "finish"
        ):
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    client.session._sleep = lambda _: None
    calls = install_transport(handle)
    if failure == "expired":
        with pytest.raises(AuthenticationExpiredError):
            run_course(client, video_course("video"))
        with pytest.raises(AuthenticationExpiredError):
            client.submit_video_progress(video_course("next"))
    else:
        result = run_course(client, video_course("video"))
        assert not result.succeeded
        if failure not in {"rate", "finish"}:
            assert "视频完成状态未确认" in result.error
    endpoints = [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls]
    expected = ["queryCourseQuestionRelaByMainId", "finishRate"]
    if failure != "rate":
        expected.append("finish")
    if failure not in {"rate", "finish"}:
        expected.extend(["myCourseList"] * (3 if failure == "network" else 1))
    if failure == "pending":
        expected.extend(["finish", "myCourseList"])
    assert endpoints == expected


@pytest.mark.parametrize(
    "failure",
    ["timeout", "connection", "redirect307", "redirect308", "lock", "lock_typo", "http_lock"],
)
def test_video_post_is_never_replayed(client, install_transport, failure):
    def handle(session, request, kwargs):
        if failure == "timeout":
            raise requests.Timeout("synthetic timeout")
        if failure == "connection":
            raise requests.ConnectionError("synthetic connection failure")
        if failure.startswith("redirect"):
            return make_response(
                status=int(failure[-3:]), headers={"Location": BASE + "/elsewhere"}
            )
        return make_response(
            {
                "success": False,
                "code": 500,
                "message": ("aquire lock fail" if failure == "lock_typo" else "acquire lock fail"),
            },
            status=500 if failure == "http_lock" else 200,
        )

    calls = install_transport(handle)
    exception = LockConflictError if "lock" in failure else SubmissionUncertainError
    if failure.startswith("redirect"):
        exception = AuthenticationExpiredError
    with pytest.raises(exception):
        client.submit_video_progress(video_course("video"))
    assert len(calls) == 1
    assert calls[0][2]["timeout"] == (10, 30)


def test_video_progress_retains_vpn_scope_and_parameters(install_transport):
    base = "https://example.test/http/fake-vpn/jeecg-boot"
    cookies = requests.cookies.RequestsCookieJar()
    cookies.set("fake-gateway", "synthetic-cookie", domain="example.test", path="/")
    factory = ResourceSessionFactory(base, "fake-token", cookies, via_vpn=True, redactor=Redactor())
    calls = install_transport(lambda *args: make_response(success()))
    try:
        with SafetyLabClient(
            factory(),
            api_base_url=base,
            session_factory=factory,
            mutation_coordinator=MutationCoordinator(),
        ) as client:
            client.submit_video_progress(video_course("list-course"))
        request = calls[0][1]
        assert request.url == base + "/jcedutec/courseSource/finishRate?enlink-vpn"
        assert request.headers["X-Access-Token"] == "fake-token"
        assert "fake-gateway=synthetic-cookie" in request.headers["Cookie"]
        assert json.loads(request.body) == {"id": "list-course", "watchDuration": "123"}
        assert len(calls) == 1 and calls[0][0].closed
    finally:
        factory.close()


# 作用：验证写入连接失败或超时报结果不确定且不重发。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     error：参数化的 requests 超时或连接异常类型。
# 返回：无返回值（None）；测试函数通过断言验证预期。
@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_uncertain_post_does_not_retry(client, install_transport, error):
    # 作用：在写入模拟传输中抛出选定网络异常。
    # 参数：
    #     *args：传输处理器位置参数，此故障实现只保留调用兼容性。
    # 返回：不返回；抛出外层 error 指定的异常。
    def fail(*args):
        raise error("synthetic failure")

    calls = install_transport(fail)
    with pytest.raises(SubmissionUncertainError):
        client.finish_course("course")
    assert len(calls) == 1


# 作用：验证 HTTP、业务认证失效及跳转都会阻止后续课程请求。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     http：参数化的 HTTP 状态。
#     body：对应状态下返回的模拟业务 JSON。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：首次读取失败后尝试另一门课程的完成请求，断言协调器在发送前阻止它。
@pytest.mark.parametrize(
    "http,body",
    [
        (401, {}),
        (403, {}),
        (200, {"success": False, "code": 401}),
        (200, {"success": True, "code": 403}),
        (500, {"success": False, "code": 401}),
        (503, {"success": False, "code": 403}),
        (302, {}),
    ],
)
def test_authentication_failure_cancels_entire_run(client, install_transport, http, body):
    calls = install_transport(lambda *args: make_response(body, status=http))
    with pytest.raises(AuthenticationExpiredError):
        client.list_courses()
    with pytest.raises(AuthenticationExpiredError):
        client.finish_course("other-course")
    assert len(calls) == 1


# 作用：验证课程列表外层结构、结果类型及标识字段必须有效。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     payload：参数化的非法课程响应 JSON。
# 返回：无返回值（None）；测试函数通过断言验证预期。
@pytest.mark.parametrize("payload", [[], {"result": []}, success({}), success([{}])])
def test_course_response_validation(client, install_transport, payload):
    install_transport(lambda *args: make_response(payload))
    with pytest.raises(ResponseFormatError):
        client.list_courses()


# 作用：验证逐题提交顺序及中途失败后不标记课程完成。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
#     failure_index：要模拟失败的题目序号，为 None 时全部成功。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：记录每次读取和写入，精确核对到失败题为止的顺序与成功提交计数。
@pytest.mark.parametrize("failure_index", [None, 1, 2])
def test_course_sequence_and_no_finish_after_failure(client, install_transport, failure_index):
    events = []

    # 作用：记录课程步骤并在指定题目提交时返回业务失败。
    # 参数：
    #     session：触发模拟传输的课程资源会话，可用于记录会话隔离。
    #     request：准备完成的 HTTP 请求，供检查方法、地址、参数及请求体。
    #     kwargs：模拟发送入口收到的传输选项。
    # 返回：题目列表、写入成功或指定失败的模拟响应。
    # 说明：题目按三个关系标识依次返回，完成请求用课程列表标识单独记录。
    def handle(session, request, kwargs):
        if urlsplit(request.url).path.endswith("myCourseList"):
            events.append("verify")
            return completed_courses("list-course")
        if request.method == "GET":
            events.append("read")
            return make_response(
                success(
                    [
                        {
                            "id": f"relation-{i}",
                            "courseId": "question-course",
                            "questionId": f"source-{i}",
                            "correctAnswer": "AB",
                        }
                        for i in (1, 2, 3)
                    ]
                )
            )
        body = json.loads(request.body)
        action = (
            "video:" + body["id"]
            if "watchDuration" in body
            else body.get("questionId", "finish:" + body["id"])
        )
        events.append(action)
        if action == f"relation-{failure_index}":
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    install_transport(handle)
    result = run_course(client, video_course("list-course", "fake course"))
    if failure_index is None:
        assert events == [
            "read",
            "relation-1",
            "relation-2",
            "relation-3",
            "video:list-course",
            "finish:list-course",
            "verify",
        ]
        assert result.succeeded and result.answered_count == 3
    else:
        assert events == ["read"] + [f"relation-{i}" for i in range(1, failure_index + 1)]
        assert not result.succeeded and result.answered_count == failure_index - 1


# 作用：验证无题课程仍标记完成并发出开始进度事件。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：无题视频也必须上报进度、标记完成并回读，结果成功且答题数为零。
def test_empty_course_finishes_and_emits_progress(client, install_transport):
    def handle(session, request, kwargs):
        if urlsplit(request.url).path.endswith("myCourseList"):
            return completed_courses("empty")
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    events = []
    result = run_course(client, video_course("empty", "empty course"), progress=events.append)
    assert result.succeeded and result.answered_count == 0
    assert [request.method for _, request, _ in calls] == ["GET", "POST", "POST", "GET"]
    assert json.loads(calls[1][1].body) == {"id": "empty", "watchDuration": "123"}
    assert events[0].stage == "started"


# 作用：验证四课程读取可重叠、所有写入串行且会话各自独立。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：通过屏障和受锁保护的计数观察 GET 最大并发 4、POST 最大并发 1。
# 说明：检查八门课程各用独立 CookieJar 和适配器，协调器相同，所有课程会话最终关闭。
@pytest.mark.parametrize("recover", [False, True])
def test_real_clients_have_overlapping_reads_serial_posts_and_independent_sessions(
    client, install_transport, recover
):
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    counts = {"GET": 0, "POST": 0}
    maxima = {"GET": 0, "POST": 0}
    sessions = set()
    status_reads = {}

    # 作用：在模拟读取中同步线程并统计各类请求的最大并发。
    # 参数：
    #     session：触发模拟传输的课程资源会话，可用于记录会话隔离。
    #     request：准备完成的 HTTP 请求，供检查方法、地址、参数及请求体。
    #     kwargs：模拟发送入口收到的传输选项。
    # 返回：携带课程对应题目的 GET 响应或成功写入响应。
    # 说明：GET 等待四方屏障，POST 短暂等待以暴露重叠；finally 保证活跃计数回落。
    def handle(session, request, kwargs):
        with lock:
            sessions.add(session)
            counts[request.method] += 1
            maxima[request.method] = max(maxima[request.method], counts[request.method])
        try:
            if urlsplit(request.url).path.endswith("myCourseList"):
                status_reads[session] = status_reads.get(session, 0) + 1
                if recover and status_reads[session] == 1:
                    return make_response(
                        success(
                            [
                                {"id": str(i), "isFinish": "1", "watchDuration": "100.25"}
                                for i in range(8)
                            ]
                        )
                    )
                return completed_courses(*(str(i) for i in range(8)))
            if request.method == "GET":
                barrier.wait(timeout=5)
                course = parse_qs(urlsplit(request.url).query)["id"][0]
                return make_response(
                    success(
                        [
                            {
                                "id": "relation-" + course,
                                "courseId": "qcourse-" + course,
                                "questionId": "source-" + course,
                                "correctAnswer": "A",
                            }
                        ]
                    )
                )
            time.sleep(0.005)
            return make_response(success())
        finally:
            with lock:
                counts[request.method] -= 1

    calls = install_transport(handle)
    observed = []
    clone = client.clone

    # 作用：记录每个实际创建的课程客户端以检查资源隔离。
    # 参数：无。
    # 返回：原 clone 方法创建的客户端。
    # 说明：调用外层保存的原方法并追加到 observed，不改变共享协调器或会话逻辑。
    def track_clone():
        worker = clone()
        observed.append(worker)
        return worker

    client.clone = track_clone
    results = CourseRunner(client, 4).run([video_course(str(i)) for i in range(8)])
    assert all(result.succeeded for result in results)
    assert maxima == {"GET": 4, "POST": 1}
    assert len(sessions) == 8 and all(session.closed for session in sessions)
    assert all(worker.mutation_coordinator is client.mutation_coordinator for worker in observed)
    assert len({id(worker.session.cookies) for worker in observed}) == 8
    assert len({id(worker.session.get_adapter("https://")) for worker in observed}) == 8
    assert len(calls) == (56 if recover else 40)


# 作用：验证已排队写入在取得锁后仍检查全局认证失效。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：主线程先占锁，再取消运行；等待中的任务应抛出认证失效异常且没有 HTTP 请求。
@pytest.mark.parametrize("video", [False, True])
def test_queued_write_checks_cancellation_after_acquiring_lock(client, install_transport, video):
    calls = install_transport(lambda *args: make_response(success()))
    entered = threading.Event()
    coordinator = client.mutation_coordinator
    with ThreadPoolExecutor(max_workers=1) as executor:
        with coordinator.serialized():
            # 作用：通知主线程任务已启动，然后尝试进入课程完成写入。
            # 参数：无。
            # 返回：无返回值（None）；取消后由写入流程抛出 AuthenticationExpiredError。
            # 说明：通过外层事件保证取消发生在任务开始之后，写入会等待主线程持有的锁。
            def task():
                entered.set()
                if video:
                    client.submit_video_progress(video_course("queued"))
                else:
                    client.finish_course("queued")

            future = executor.submit(task)
            assert entered.wait(2)
            coordinator.cancel(authentication_failed=True)
        with pytest.raises(AuthenticationExpiredError):
            future.result(timeout=2)
    assert not calls


# 作用：验证单门课程业务失败不影响其他课程完成。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：两门课程共享协调器，一门完成请求失败，另一门应成功。
def test_runner_keeps_other_courses_after_failure(client, install_transport):
    # 作用：为两门空题课程分别模拟完成成功或业务失败。
    # 参数：
    #     session：触发模拟传输的课程资源会话，可用于记录会话隔离。
    #     request：准备完成的 HTTP 请求，供检查方法、地址、参数及请求体。
    #     kwargs：模拟发送入口收到的传输选项。
    # 返回：空题目列表或按课程标识选择的写入响应。
    def handle(session, request, kwargs):
        if urlsplit(request.url).path.endswith("myCourseList"):
            return completed_courses("good")
        if request.method == "GET":
            return make_response(success([]))
        if json.loads(request.body)["id"] == "bad":
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    install_transport(handle)
    results = CourseRunner(client, 2).run([video_course("bad"), video_course("good")])
    assert sum(result.succeeded for result in results) == 1


# 作用：验证零、负数、超上限、小数和布尔线程数均被拒绝。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     workers：参数化的非法并发设置值。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：构造 CourseRunner 时即抛出 ValueError，不启动线程。
@pytest.mark.parametrize("workers", [0, 5, -1, 1.5, True])
def test_runner_rejects_invalid_thread_counts(client, workers):
    with pytest.raises(ValueError):
        CourseRunner(client, workers)


# 作用：验证认证失效会取消尚未开始的课程任务。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：用单线程提交二十门课程，首次读取返回 401 后请求总数仍为一次，已用会话关闭。
def test_runner_global_expiry_stops_pending_courses(client, install_transport):
    calls = install_transport(lambda *args: make_response(status=401))
    with pytest.raises(AuthenticationExpiredError):
        CourseRunner(client, 1).run([video_course(str(i)) for i in range(20)])
    assert len(calls) == 1
    assert calls[0][0].closed


# 作用：验证读取空题列表后发生取消也不能继续完成写入。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：请求总数为一次 GET，run_course 传播 RunCancelledError。
def test_run_cancelled_before_empty_course_finish(client, install_transport):
    # 作用：在返回空题列表前设置共享取消信号。
    # 参数：
    #     *args：模拟传输入口的位置参数，本处理器只操作外层客户端的协调器。
    # 返回：包含空题目列表的成功模拟响应。
    def handle(*args):
        client.mutation_coordinator.cancel()
        return make_response(success([]))

    calls = install_transport(handle)
    with pytest.raises(RunCancelledError):
        run_course(client, video_course("empty"))
    assert len(calls) == 1
