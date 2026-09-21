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

BASE = "https://example.test/jeecg-boot"


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


@pytest.mark.parametrize(
    "answer,expected",
    [(" A ", "A"), ("AB", "AB"), (" A, B ", ["A", "B"]), ([" A", "B "], ["A", "B"])],
)
def test_three_question_ids_precise_payload_and_finish_id(
    client, install_transport, answer, expected
):
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


@pytest.mark.parametrize("answer", [None, "", "A,", [], ["A", 1], 7])
def test_malformed_question_answers_are_rejected(client, install_transport, answer):
    install_transport(
        lambda *args: make_response(
            success([{"id": "relation", "courseId": "course", "correctAnswer": answer}])
        )
    )
    with pytest.raises(ResponseFormatError):
        client.list_questions("course")


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


@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_uncertain_post_does_not_retry(client, install_transport, error):
    def fail(*args):
        raise error("synthetic failure")

    calls = install_transport(fail)
    with pytest.raises(SubmissionUncertainError):
        client.finish_course("course")
    assert len(calls) == 1


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


@pytest.mark.parametrize("payload", [[], {"result": []}, success({}), success([{}])])
def test_course_response_validation(client, install_transport, payload):
    install_transport(lambda *args: make_response(payload))
    with pytest.raises(ResponseFormatError):
        client.list_courses()


@pytest.mark.parametrize("failure_index", [None, 1, 2])
def test_course_sequence_and_no_finish_after_failure(client, install_transport, failure_index):
    events = []

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


def test_real_clients_have_overlapping_reads_serial_posts_and_independent_sessions(
    client, install_transport
):
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    counts = {"GET": 0, "POST": 0}
    maxima = {"GET": 0, "POST": 0}
    sessions = set()

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


def test_queued_write_checks_cancellation_after_acquiring_lock(client, install_transport):
    calls = install_transport(lambda *args: make_response(success()))
    entered = threading.Event()
    coordinator = client.mutation_coordinator
    with ThreadPoolExecutor(max_workers=1) as executor:
        with coordinator.serialized():

            def task():
                entered.set()
                client.finish_course("queued")

            future = executor.submit(task)
            assert entered.wait(2)
            coordinator.cancel(authentication_failed=True)
        with pytest.raises(AuthenticationExpiredError):
            future.result(timeout=2)
    assert not calls


def test_runner_keeps_other_courses_after_failure(client, install_transport):
    def handle(session, request, kwargs):
        if request.method == "GET":
            return make_response(success([]))
        if json.loads(request.body)["id"] == "bad":
            return make_response({"success": False, "code": 500, "message": "synthetic failure"})
        return make_response(success())

    install_transport(handle)
    results = CourseRunner(client, 2).run([Course("bad", "bad"), Course("good", "good")])
    assert sum(result.succeeded for result in results) == 1


@pytest.mark.parametrize("workers", [0, 5, -1, 1.5, True])
def test_runner_rejects_invalid_thread_counts(client, workers):
    with pytest.raises(ValueError):
        CourseRunner(client, workers)


def test_runner_global_expiry_stops_pending_courses(client, install_transport):
    calls = install_transport(lambda *args: make_response(status=401))
    with pytest.raises(AuthenticationExpiredError):
        CourseRunner(client, 1).run([Course(str(i), "synthetic") for i in range(20)])
    assert len(calls) == 1
    assert calls[0][0].closed


def test_run_cancelled_before_empty_course_finish(client, install_transport):
    def handle(*args):
        client.mutation_coordinator.cancel()
        return make_response(success([]))

    calls = install_transport(handle)
    with pytest.raises(RunCancelledError):
        run_course(client, Course("empty", "synthetic"))
    assert len(calls) == 1
