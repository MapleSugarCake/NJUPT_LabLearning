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
from unittest.mock import call
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import make_response, success

from labpass_cli.runner import CourseRunner
from njupt_auth.redaction import Redactor
from njupt_auth.transport import ResourceSessionFactory
from njupt_safetylabpass import Course, MutationCoordinator, Question, SafetyLabClient, run_course
from njupt_safetylabpass import client as client_module
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
        ("123.456789", [str(value) for value in range(10, 121, 10)]),
        (42, ["10", "20", "30", "40"]),
        (1.25, ["0"]),
        ("292.455896", [str(value) for value in range(10, 291, 10)]),
        ("289.9", [str(value) for value in range(10, 281, 10)]),
        ("290.000", [str(value) for value in range(10, 291, 10)]),
        ("10", ["10"]),
        ("20", ["10", "20"]),
        ("9.999", ["0"]),
        ("0.001", ["0"]),
        ("1E+2", [str(value) for value in range(10, 101, 10)]),
        ("100.000000000000000001", [str(value) for value in range(10, 101, 10)]),
        ("19.999999999999999999999999999999999999", ["10"]),
        ("99.999999999999999999999999999999999999", [str(value) for value in range(10, 91, 10)]),
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
    assert len(calls) == len(expected) + 1
    assert [json.loads(request.body) for _, request, _ in calls[1:]] == [
        {"id": "video-list-id", "watchDuration": value} for value in expected
    ]
    for _, request, _ in calls[1:]:
        assert urlsplit(request.url).path.endswith("/finishRate")
        value = json.loads(request.body)["watchDuration"]
        assert isinstance(value, str)
        assert re.fullmatch(r"[0-9]+", value)
        assert int(value) % 10 == 0
        assert Decimal(value) <= course.duration_seconds
    assert Decimal(expected[-1]) <= course.duration_seconds < Decimal(expected[-1]) + 10


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


@pytest.mark.parametrize("percent", ["100.01", "99.99", "0", "150"])
def test_non_100_progress_fails_after_single_finish(client, install_transport, percent):
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
                            "watchDuration": percent,
                        }
                    ]
                )
            )
        if request.method == "GET":
            return make_response(success([]))
        return make_response(success())

    calls = install_transport(handle)
    result = run_course(client, video_course("video"))
    assert not result.succeeded
    assert reads == 1
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == [
        "queryCourseQuestionRelaByMainId",
        *["finishRate"] * 12,
        "finish",
        "myCourseList",
    ]
    assert f"视频进度={percent}%" in result.error


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
def test_unknown_or_exact_progress_uses_single_finish(client, install_transport, percent, flag, ok):
    def handle(session, request, kwargs):
        if urlsplit(request.url).path.endswith("myCourseList"):
            return make_response(
                success([{"id": "video", "isFinish": flag, "watchDuration": percent}])
            )
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    assert run_course(client, video_course("video")).succeeded is ok
    assert len(calls) == 15
    assert sum(urlsplit(request.url).path.endswith("/finish") for _, request, _ in calls) == 1


@pytest.mark.parametrize(
    "fault", ["timeout", "connection", "lock", "expired", "cancel", "interrupt", "read_error"]
)
def test_single_finish_failure_stops_without_more_writes(client, install_transport, fault):
    finishes = reads = 0

    def handle(session, request, kwargs):
        nonlocal finishes, reads
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "finish":
            finishes += 1
            if fault == "timeout":
                raise requests.Timeout("synthetic timeout")
            if fault == "connection":
                raise requests.ConnectionError("synthetic connection failure")
            if fault == "lock":
                return make_response({"success": False, "code": 500, "message": "aquire lock fail"})
            if fault == "expired":
                return make_response(status=401)
            if fault == "cancel":
                client.mutation_coordinator.cancel()
            if fault == "interrupt":
                raise KeyboardInterrupt
        if endpoint == "myCourseList":
            reads += 1
            if fault == "read_error":
                return make_response(text="invalid JSON")
            return make_response(
                success([{"id": "video", "isFinish": "1", "watchDuration": "101"}])
            )
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    if fault in {"expired", "cancel", "interrupt"}:
        error = {
            "expired": AuthenticationExpiredError,
            "cancel": RunCancelledError,
            "interrupt": KeyboardInterrupt,
        }[fault]
        with pytest.raises(error):
            run_course(client, video_course("video"))
    else:
        result = run_course(client, video_course("video"))
        assert not result.succeeded
        assert result.uncertain is (fault in {"timeout", "connection"})
    assert finishes == 1
    assert reads == (1 if fault == "read_error" else 0)
    assert [
        json.loads(request.body)["watchDuration"]
        for _, request, _ in calls
        if urlsplit(request.url).path.endswith("finishRate")
    ] == [str(value) for value in range(10, 121, 10)]


@pytest.mark.parametrize(
    "duration,expected",
    [
        ("123.456789", [str(value) for value in range(10, 121, 10)]),
        ("9.999", ["0"]),
        ("19.999", ["10"]),
    ],
)
def test_complete_progress_sequence_completes_without_extra_posts(
    client, install_transport, duration, expected
):
    """模拟完整十秒序列，覆盖零秒和十秒边界；不是服务端行为实测。"""
    progress_saved = []
    finished = False

    def handle(session, request, kwargs):
        nonlocal finished
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "finishRate":
            body = json.loads(request.body)
            watch_duration = body["watchDuration"]
            if (
                not isinstance(watch_duration, str)
                or not re.fullmatch(r"[0-9]+", watch_duration)
                or int(watch_duration) % 10 != 0
            ):
                return make_response({"success": False, "code": 500, "message": "aquire lock fail"})
            assert body == {"id": "video", "watchDuration": expected[len(progress_saved)]}
            progress_saved.append(watch_duration)
        elif endpoint == "finish":
            assert progress_saved == expected
            finished = True
        elif endpoint == "myCourseList":
            assert finished
            return completed_courses("video")
        else:
            return make_response(success([]))
        return make_response(success())

    calls = install_transport(handle)
    result = run_course(client, Course("video", "synthetic", duration_seconds=Decimal(duration)))
    assert result.succeeded
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == (
        ["queryCourseQuestionRelaByMainId"]
        + ["finishRate"] * len(expected)
        + ["finish", "myCourseList"]
    )


@pytest.mark.parametrize("question_count", [0, 2])
@pytest.mark.parametrize("delays", [[1.0, 4.25, 6.0, 1.5], [6.0, 1.5, 4.25, 1.0]])
@pytest.mark.parametrize(
    "duration,values", [("42.5", ["10", "20", "30", "40"]), ("9.999", ["0"]), ("10", ["10"])]
)
def test_random_delays_between_progress_packets_and_before_finish(
    client,
    install_transport,
    monkeypatch,
    fast_video_delays,
    question_count,
    duration,
    values,
    delays,
):
    delays = delays[: len(values)]
    fast_video_delays.side_effect = delays
    now = 0.0
    events = []
    waits = []

    def wait(seconds):
        nonlocal now
        assert seconds == delays[len(waits)]
        waits.append(seconds)
        events.append(("wait", now))
        now += seconds
        return False

    monkeypatch.setattr(client.mutation_coordinator._cancelled, "wait", wait)

    def handle(session, request, kwargs):
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "myCourseList":
            events.append(("verify", now))
            return completed_courses("video")
        if request.method == "GET":
            events.append(("questions", now))
            return make_response(
                success(
                    [
                        {
                            "id": f"answer-{i}",
                            "courseId": "question-course",
                            "questionId": f"source-{i}",
                            "correctAnswer": "A",
                        }
                        for i in range(question_count)
                    ]
                )
            )
        body = json.loads(request.body)
        if endpoint == "finishRate":
            assert body["id"] == "video"
            events.append((body["watchDuration"], now))
        elif endpoint == "submitAnswer":
            assert body["id"] == "question-course"
            events.append((body["questionId"], now))
        else:
            assert endpoint == "finish" and body == {"id": "video"}
            events.append(("finish", now))
        return make_response(success())

    calls = install_transport(handle)
    result = run_course(client, Course("video", "synthetic", duration_seconds=Decimal(duration)))
    assert result.succeeded
    expected = [("questions", 0.0)] + [(f"answer-{i}", 0.0) for i in range(question_count)]
    elapsed = 0.0
    for value, delay in zip(values, delays, strict=True):
        expected.extend([(value, elapsed), ("wait", elapsed)])
        elapsed += delay
    expected.extend([("finish", elapsed), ("verify", elapsed)])
    assert events == expected
    assert waits == delays
    assert fast_video_delays.call_args_list == [call(1.0, 6.0)] * len(values)
    assert len(calls) == question_count + len(values) + 3


@pytest.mark.parametrize("fault", ["cancel", "expired", "interrupt"])
@pytest.mark.parametrize("stop_on", [1, 6, 12])
def test_cancellation_during_delay_stops_progress_and_finish(
    client, install_transport, monkeypatch, fast_video_delays, fault, stop_on
):
    delays = [1.0, 4.25, 6.0] * 4
    fast_video_delays.side_effect = delays
    waits = 0

    def wait(seconds):
        nonlocal waits
        assert seconds == delays[waits]
        waits += 1
        if waits == stop_on:
            if fault == "interrupt":
                raise KeyboardInterrupt
            client.mutation_coordinator.cancel(authentication_failed=fault == "expired")
        return False

    monkeypatch.setattr(client.mutation_coordinator._cancelled, "wait", wait)
    calls = install_transport(
        lambda session, request, kwargs: make_response(
            success([] if request.method == "GET" else None)
        )
    )
    error = {
        "cancel": RunCancelledError,
        "expired": AuthenticationExpiredError,
        "interrupt": KeyboardInterrupt,
    }[fault]
    with pytest.raises(error):
        run_course(client, video_course("video"))
    assert waits == stop_on
    assert fast_video_delays.call_args_list == [call(1.0, 6.0)] * stop_on
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == (
        ["queryCourseQuestionRelaByMainId"] + ["finishRate"] * stop_on
    )
    assert [json.loads(request.body)["watchDuration"] for _, request, _ in calls[1:]] == [
        str(value) for value in range(10, stop_on * 10 + 1, 10)
    ]


@pytest.mark.parametrize("expired", [False, True])
def test_coordinator_wait_is_woken_immediately_by_cancellation(monkeypatch, expired):
    coordinator = MutationCoordinator()
    entered = threading.Event()
    event_wait = coordinator._cancelled.wait

    def wait(seconds):
        entered.set()
        return event_wait(seconds)

    monkeypatch.setattr(coordinator._cancelled, "wait", wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(coordinator.wait, 30.0)
        try:
            assert entered.wait(timeout=2)
        finally:
            coordinator.cancel(authentication_failed=expired)
        error = AuthenticationExpiredError if expired else RunCancelledError
        with pytest.raises(error):
            future.result(timeout=2)


@pytest.mark.parametrize("expired", [False, True])
def test_coordinator_wait_checks_cancellation_before_waiting(monkeypatch, expired):
    coordinator = MutationCoordinator()
    coordinator.cancel(authentication_failed=expired)
    monkeypatch.setattr(coordinator._cancelled, "wait", lambda _: pytest.fail("已取消时不得等待"))
    error = AuthenticationExpiredError if expired else RunCancelledError
    with pytest.raises(error):
        coordinator.wait(3.0)


@pytest.mark.parametrize("missing", [None, "20", "40"])
def test_finish_checks_sequence_and_clears_progress_on_missing_packet(
    client, install_transport, missing
):
    """模拟服务端漏存已确认的包并在 finish 时清零，回读失败不得补交。"""
    received = []
    saved = []
    finishes = reads = 0
    finished = False

    def handle(session, request, kwargs):
        nonlocal finished, finishes, reads
        endpoint = urlsplit(request.url).path.rsplit("/", 1)[-1]
        if endpoint == "finishRate":
            body = json.loads(request.body)
            assert body["id"] == "video"
            received.append(body["watchDuration"])
            if body["watchDuration"] != missing:
                saved.append(body["watchDuration"])
        elif endpoint == "finish":
            assert json.loads(request.body) == {"id": "video"}
            finishes += 1
            finished = saved == ["10", "20", "30", "40"]
            if not finished:
                saved.clear()
        elif endpoint == "myCourseList":
            reads += 1
            assert finishes == 1
            return make_response(
                success(
                    [
                        {
                            "id": "video",
                            "isFinish": finished,
                            "watchDuration": "100" if finished else "0",
                        }
                    ]
                )
            )
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    result = run_course(client, Course("video", "synthetic", duration_seconds=Decimal("42.5")))
    assert received == ["10", "20", "30", "40"]
    assert result.succeeded is (missing is None)
    assert finishes == reads == 1
    assert len(calls) == 7
    if missing is not None:
        assert saved == []
        assert "视频完成状态未确认" in result.error and "视频进度=0%" in result.error


def test_progress_sequence_is_consumed_lazily(client, install_transport, monkeypatch):
    bounds = []

    def progress_range(start, stop, step):
        bounds.append((start, stop, step))
        yield 10
        pytest.fail("首包失败后不得提前构造或继续读取进度序列")

    monkeypatch.setattr(client_module, "range", progress_range, raising=False)
    calls = install_transport(
        lambda *args: make_response({"success": False, "code": 400, "message": "rejected"})
    )
    course = Course("video", "synthetic", duration_seconds=Decimal("1E+30"))
    with pytest.raises(ApiError):
        client.submit_video_progress(course)
    assert bounds == [(10, 10**30 + 1, 10)]
    assert len(calls) == 1
    assert json.loads(calls[0][1].body) == {"id": "video", "watchDuration": "10"}


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
    assert "上报整十秒数=10" in caplog.text
    assert "上报整十秒数=120" not in caplog.text
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
        expected.extend(["finishRate"] * 11 + ["finish"])
    if failure not in {"rate", "finish"}:
        expected.extend(["myCourseList"] * (3 if failure == "network" else 1))
    assert endpoints == expected


@pytest.mark.parametrize(
    "failure",
    ["timeout", "connection", "redirect307", "redirect308", "lock", "lock_typo", "http_lock"],
)
@pytest.mark.parametrize("failure_on", [1, 6, 12])
def test_video_post_is_never_replayed(client, install_transport, failure, failure_on):
    progress_posts = 0

    def handle(session, request, kwargs):
        nonlocal progress_posts
        progress_posts += 1
        if progress_posts < failure_on:
            return make_response(success())
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
    assert len(calls) == failure_on
    assert [json.loads(request.body)["watchDuration"] for _, request, _ in calls] == [
        str(value) for value in range(10, 121, 10)
    ][:failure_on]
    assert all(kwargs["timeout"] == (10, 30) for _, _, kwargs in calls)


@pytest.mark.parametrize("failure_on", [1, 6, 12])
@pytest.mark.parametrize("fault", ["business", "timeout"])
def test_progress_failure_stops_course_before_finish(
    client, install_transport, monkeypatch, fast_video_delays, failure_on, fault
):
    progress_posts = 0
    waits = []
    delays = [1.0, 4.25, 6.0] * 4
    fast_video_delays.side_effect = delays
    monkeypatch.setattr(
        client.mutation_coordinator._cancelled, "wait", lambda seconds: waits.append(seconds)
    )

    def handle(session, request, kwargs):
        nonlocal progress_posts
        if urlsplit(request.url).path.endswith("finishRate"):
            progress_posts += 1
            if progress_posts == failure_on:
                if fault == "timeout":
                    raise requests.Timeout("synthetic timeout")
                return make_response({"success": False, "code": 400, "message": "rejected"})
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    events = []
    result = run_course(client, video_course("video"), progress=events.append)
    assert not result.succeeded
    assert result.uncertain is (fault == "timeout")
    assert waits == delays[: failure_on - 1]
    assert fast_video_delays.call_args_list == [call(1.0, 6.0)] * (failure_on - 1)
    assert [(event.sent_packets, event.total_packets) for event in events[1:]] == [
        (count, 12) for count in range(failure_on)
    ]
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == (
        ["queryCourseQuestionRelaByMainId"] + ["finishRate"] * failure_on
    )


@pytest.mark.parametrize("fault", ["cancel", "expired", "interrupt"])
@pytest.mark.parametrize("stop_on", [1, 6, 12])
def test_cancel_during_progress_sequence_prevents_later_progress_and_finish(
    client, install_transport, fault, stop_on
):
    progress_posts = 0

    def handle(session, request, kwargs):
        nonlocal progress_posts
        if urlsplit(request.url).path.endswith("finishRate"):
            progress_posts += 1
            assert json.loads(request.body) == {
                "id": "video",
                "watchDuration": str(progress_posts * 10),
            }
            if progress_posts == stop_on:
                if fault == "interrupt":
                    raise KeyboardInterrupt
                if fault == "expired":
                    return make_response(status=401)
                client.mutation_coordinator.cancel()
        return make_response(success([] if request.method == "GET" else None))

    calls = install_transport(handle)
    error = {
        "cancel": RunCancelledError,
        "expired": AuthenticationExpiredError,
        "interrupt": KeyboardInterrupt,
    }[fault]
    with pytest.raises(error):
        run_course(client, video_course("video"))
    assert [urlsplit(request.url).path.rsplit("/", 1)[-1] for _, request, _ in calls] == (
        ["queryCourseQuestionRelaByMainId"] + ["finishRate"] * stop_on
    )


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
        assert len(calls) == 12 and all(session is calls[0][0] for session, _, _ in calls)
        assert calls[0][0].closed
        for (_, request, _), value in zip(
            calls, [str(value) for value in range(10, 121, 10)], strict=True
        ):
            assert request.url == base + "/jcedutec/courseSource/finishRate?enlink-vpn"
            assert request.headers["X-Access-Token"] == "fake-token"
            assert "fake-gateway=synthetic-cookie" in request.headers["Cookie"]
            assert json.loads(request.body) == {"id": "list-course", "watchDuration": value}
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
            "video:" + body["id"] + ":" + body["watchDuration"]
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
            *[f"video:list-course:{value}" for value in range(10, 121, 10)],
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
    assert [request.method for _, request, _ in calls] == ["GET"] + ["POST"] * 13 + ["GET"]
    assert [json.loads(request.body) for _, request, _ in calls[1:13]] == [
        {"id": "empty", "watchDuration": str(value)} for value in range(10, 121, 10)
    ]
    assert events[0].stage == "started"
    assert [event.stage for event in events[1:]] == ["video"] * 13
    assert [(event.sent_packets, event.total_packets) for event in events[1:]] == [
        (count, 12) for count in range(13)
    ]


# 作用：验证两线程及四线程读取可重叠、所有写入串行且会话各自独立。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：通过屏障和受锁保护的计数观察 GET 最大并发等于设置、POST 最大并发 1。
# 说明：检查八门课程各用独立 CookieJar 和适配器，协调器相同，所有课程会话最终关闭。
@pytest.mark.parametrize("incomplete", [False, True])
@pytest.mark.parametrize("workers", [2, 4])
def test_real_clients_have_overlapping_reads_serial_posts_and_independent_sessions(
    client, install_transport, incomplete, workers
):
    barrier = threading.Barrier(workers)
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
    # 说明：GET 等待与线程数匹配的屏障，POST 短暂等待以暴露重叠；finally 保证活跃计数回落。
    def handle(session, request, kwargs):
        with lock:
            sessions.add(session)
            counts[request.method] += 1
            maxima[request.method] = max(maxima[request.method], counts[request.method])
        try:
            if urlsplit(request.url).path.endswith("myCourseList"):
                status_reads[session] = status_reads.get(session, 0) + 1
                if incomplete:
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
    results = CourseRunner(client, workers).run([video_course(str(i)) for i in range(8)])
    assert all(result.succeeded is (not incomplete) for result in results)
    assert maxima == {"GET": workers, "POST": 1}
    assert len(sessions) == 8 and all(session.closed for session in sessions)
    assert all(worker.mutation_coordinator is client.mutation_coordinator for worker in observed)
    assert len({id(worker.session.cookies) for worker in observed}) == 8
    assert len({id(worker.session.get_adapter("https://")) for worker in observed}) == 8
    assert set(status_reads.values()) == {1}
    assert len(calls) == 128
    all_writes = [(source, request) for source, request, _ in calls if request.method == "POST"]
    for index, (source, request) in enumerate(all_writes):
        if json.loads(request.body).get("watchDuration") == "10":
            # 完整序列占用同一写入锁，其他课程不能在中间插入任何 POST。
            block = all_writes[index : index + 12]
            assert all(session is source for session, _ in block)
            assert [json.loads(item.body)["watchDuration"] for _, item in block] == [
                str(value) for value in range(10, 121, 10)
            ]
    for session in sessions:
        writes = [
            request
            for source, request, _ in calls
            if source is session and request.method == "POST"
        ]
        assert [json.loads(request.body)["watchDuration"] for request in writes[1:-1]] == [
            str(value) for value in range(10, 121, 10)
        ]
        assert urlsplit(writes[-1].url).path.endswith("/finish")


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
# 说明：两门课程共享协调器，一门进度或完成请求失败，另一门仍完成完整序列。
@pytest.mark.parametrize("failure_step", ["10", "60", "120", "finish"])
@pytest.mark.parametrize("fault", ["business", "timeout"])
def test_runner_keeps_other_courses_after_failure(client, install_transport, failure_step, fault):
    # 作用：在指定进度包或完成请求注入失败，验证其他课程继续执行。
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
        body = json.loads(request.body)
        if body["id"] == "bad" and body.get("watchDuration", "finish") == failure_step:
            if fault == "timeout":
                raise requests.Timeout("synthetic timeout")
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    calls = install_transport(handle)
    results = CourseRunner(client, 2).run([video_course("bad"), video_course("good")])
    by_id = {result.course.id: result for result in results}
    assert by_id["good"].succeeded and not by_id["bad"].succeeded
    assert by_id["bad"].uncertain is (fault == "timeout")
    for course_id in ("bad", "good"):
        payloads = [
            json.loads(request.body)
            for _, request, _ in calls
            if request.method == "POST" and json.loads(request.body)["id"] == course_id
        ]
        expected = [
            {"id": course_id, "watchDuration": str(value)} for value in range(10, 121, 10)
        ] + [{"id": course_id}]
        if course_id == "bad" and failure_step != "finish":
            expected = expected[: int(failure_step) // 10]
        assert payloads == expected
    assert all(session.closed for session, _, _ in calls)


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
