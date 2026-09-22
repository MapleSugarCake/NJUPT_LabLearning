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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import make_response, success

from labpass_cli.runner import CourseRunner
from njupt_auth.redaction import Redactor
from njupt_auth.transport import ResourceSessionFactory
from njupt_safetylabpass import Course, MutationCoordinator, Question, SafetyLabClient, run_course
from njupt_safetylabpass.exceptions import (
    AuthenticationExpiredError,
    LockConflictError,
    ResponseFormatError,
    RunCancelledError,
    SubmissionUncertainError,
)

# 业务测试使用的虚构实验室 API 基址。
# 采用 example.test 地址，所有请求仍必须由离线夹具模拟。
BASE = "https://example.test/jeecg-boot"


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
                    {"id": "one", "courseName": "first", "isFinish": True},
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
        action = body.get("questionId", "finish:" + body["id"])
        events.append(action)
        if action == f"relation-{failure_index}":
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    install_transport(handle)
    result = run_course(client, Course("list-course", "fake course"))
    if failure_index is None:
        assert events == ["read", "relation-1", "relation-2", "relation-3", "finish:list-course"]
        assert result.succeeded and result.answered_count == 3
    else:
        assert events == ["read"] + [f"relation-{i}" for i in range(1, failure_index + 1)]
        assert not result.succeeded and result.answered_count == failure_index - 1


# 作用：验证无题课程仍标记完成并发出开始进度事件。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：请求顺序应为一次 GET 和一次 POST，结果成功且答题数为零。
def test_empty_course_finishes_and_emits_progress(client, install_transport):
    calls = install_transport(
        lambda session, request, kwargs: make_response(
            success([] if request.method == "GET" else None)
        )
    )
    events = []
    result = run_course(client, Course("empty", "empty course"), progress=events.append)
    assert result.succeeded and result.answered_count == 0
    assert [request.method for _, request, _ in calls] == ["GET", "POST"]
    assert events[0].stage == "started"


# 作用：验证四课程读取可重叠、所有写入串行且会话各自独立。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：通过屏障和受锁保护的计数观察 GET 最大并发 4、POST 最大并发 1。
# 说明：检查八门课程各用独立 CookieJar 和适配器，协调器相同，所有课程会话最终关闭。
def test_real_clients_have_overlapping_reads_serial_posts_and_independent_sessions(
    client, install_transport
):
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    counts = {"GET": 0, "POST": 0}
    maxima = {"GET": 0, "POST": 0}
    sessions = set()

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
    results = CourseRunner(client, 4).run([Course(str(i), "synthetic") for i in range(8)])
    assert all(result.succeeded for result in results)
    assert maxima == {"GET": 4, "POST": 1}
    assert len(sessions) == 8 and all(session.closed for session in sessions)
    assert all(worker.mutation_coordinator is client.mutation_coordinator for worker in observed)
    assert len({id(worker.session.cookies) for worker in observed}) == 8
    assert len({id(worker.session.get_adapter("https://")) for worker in observed}) == 8
    assert len(calls) == 24


# 作用：验证已排队写入在取得锁后仍检查全局认证失效。
# 参数：
#     client：已配置虚假认证材料的业务客户端夹具，测试结束后关闭会话和工厂。
#     install_transport：离线传输安装夹具，用模拟处理器替换网络发送并记录调用。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：主线程先占锁，再取消运行；等待中的任务应抛出认证失效异常且没有 HTTP 请求。
def test_queued_write_checks_cancellation_after_acquiring_lock(client, install_transport):
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
        if request.method == "GET":
            return make_response(success([]))
        if json.loads(request.body)["id"] == "bad":
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    install_transport(handle)
    results = CourseRunner(client, 2).run([Course("bad", "bad"), Course("good", "good")])
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
        CourseRunner(client, 1).run([Course(str(i), "synthetic") for i in range(20)])
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
        run_course(client, Course("empty", "synthetic"))
    assert len(calls) == 1
