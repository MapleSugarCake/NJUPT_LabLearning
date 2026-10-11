"""离线验证结果报告后补位、完成顺序及调度取消的资源边界。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from conftest import make_response, success
from test_cli import fake_auth_result

from labpass_cli import runner as runner_module
from labpass_cli.runner import CourseRunner
from njupt_safetylabpass import (
    Course,
    CourseProgress,
    CourseResult,
    CourseStatus,
    MutationCoordinator,
    SafetyLabClient,
)
from njupt_safetylabpass.exceptions import AuthenticationExpiredError, RunCancelledError


def course(course_id):
    return Course(course_id, f"synthetic-{course_id}", duration_seconds=Decimal("20.5"))


@pytest.fixture
def client():
    authentication = fake_auth_result()
    with (
        authentication,
        SafetyLabClient(
            authentication.session,
            api_base_url=authentication.api_base_url,
            session_factory=authentication.session_factory,
            mutation_coordinator=MutationCoordinator(),
        ) as client,
    ):
        yield client


@pytest.mark.parametrize("outcome", ["success", "failure", "uncertain"])
def test_single_worker_reports_and_closes_before_submitting_next(
    monkeypatch, client, install_transport, outcome
):
    submitted = []
    reported = []
    events = []
    workers = []
    caller = threading.current_thread()
    clone = client.clone

    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, fn, item):
            assert len(submitted) == len(reported)
            submitted.append(item.id)
            return super().submit(fn, item)

    def tracked_clone():
        assert len(workers) == len(reported)
        worker = clone()
        workers.append(worker)
        return worker

    def handle(session, request, kwargs):
        if urlsplit(request.url).path.endswith("myCourseList"):
            return make_response(
                success(
                    [
                        {
                            "id": value,
                            "isFinish": value != "first" or outcome == "success",
                            "watchDuration": "100"
                            if value != "first" or outcome == "success"
                            else "0",
                        }
                        for value in ("first", "second")
                    ]
                )
            )
        if request.method == "GET":
            value = parse_qs(urlsplit(request.url).query)["id"][0]
            if value == "second":
                assert reported == ["first"]
            return make_response(success([]))
        if session is workers[0].session and outcome == "uncertain":
            raise requests.Timeout("synthetic submission timeout")
        return make_response(success())

    def progress(event):
        assert threading.current_thread() is not caller
        if event.stage == "started":
            events.append(("started", event.course.id))

    def completed(result, count, total):
        assert threading.current_thread() is caller
        assert count == len(reported) + 1 and total == 2
        assert len(submitted) == count and len(workers) == count
        assert all(worker.session.closed for worker in workers)
        reported.append(result.course.id)
        events.append(("reported", result.course.id))

    monkeypatch.setattr(runner_module, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(client, "clone", tracked_clone)
    calls = install_transport(handle)
    results = CourseRunner(client, 1, progress=progress, completed=completed).run(
        [course("first"), course("second")]
    )
    assert events == [
        ("started", "first"),
        ("reported", "first"),
        ("started", "second"),
        ("reported", "second"),
    ]
    assert [result.succeeded for result in results] == [outcome == "success", True]
    assert results[0].uncertain is (outcome == "uncertain")
    assert all(session.closed for session, _, _ in calls)


@pytest.mark.parametrize("workers", [1, 2, 3, 4])
def test_runner_refills_one_slot_after_reporting_while_other_courses_continue(
    monkeypatch, client, workers
):
    items = [course(str(index)) for index in range(workers + 2)]
    started = {item.id: threading.Event() for item in items}
    release = {item.id: threading.Event() for item in items}
    sessions = {}
    submitted = []
    reported = []
    lock = threading.Lock()
    active = maximum = 0
    caller = threading.current_thread()
    first_finished = str(workers - 1)
    replacement = str(workers)

    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, fn, item):
            assert len(submitted) - len(reported) < workers
            submitted.append(item.id)
            return super().submit(fn, item)

    def process(worker, item, *, progress):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            sessions[item.id] = worker.session
        try:
            started[item.id].set()
            assert release[item.id].wait(5)
            return CourseResult(item, CourseStatus.SUCCESS)
        finally:
            with lock:
                active -= 1

    def completed(result, count, total):
        assert threading.current_thread() is caller
        assert count == len(reported) + 1 and total == len(items)
        assert sessions[result.course.id].closed
        assert len(submitted) - len(reported) <= workers
        if not reported:
            assert result.course.id == first_finished
            assert submitted == [str(index) for index in range(workers)]
            assert not started[replacement].is_set()
        reported.append(result.course.id)

    def control():
        try:
            for index in range(workers):
                assert started[str(index)].wait(5)
            release[first_finished].set()
            assert started[replacement].wait(5)
            assert reported == [first_finished]
            for index in range(workers - 1):
                assert not release[str(index)].is_set()
                assert not sessions[str(index)].closed
        finally:
            for event in release.values():
                event.set()

    monkeypatch.setattr(runner_module, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(runner_module, "run_course", process)
    with ThreadPoolExecutor(max_workers=1) as observer:
        observation = observer.submit(control)
        results = CourseRunner(client, workers, completed=completed).run(items)
        observation.result(timeout=5)
    assert maximum == workers
    assert len(results) == len(items) and all(result.succeeded for result in results)
    assert submitted == [item.id for item in items]
    assert reported == [result.course.id for result in results]
    assert all(session.closed for session in sessions.values())


def test_runner_preserves_completion_order_during_slow_callback(monkeypatch, client):
    items = [course(value) for value in ("first", "second", "third")]
    started = {item.id: threading.Event() for item in items}
    release = {item.id: threading.Event() for item in items}
    futures = {}
    reporting = threading.Event()
    resume = threading.Event()
    reported = []

    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, fn, item):
            future = super().submit(fn, item)
            futures[item.id] = future
            return future

    def process(worker, item, *, progress):
        started[item.id].set()
        assert release[item.id].wait(5)
        return CourseResult(item, CourseStatus.SUCCESS)

    def completed(result, count, total):
        reported.append(result.course.id)
        if result.course.id == "second":
            reporting.set()
            assert resume.wait(5)

    def control():
        try:
            for event in started.values():
                assert event.wait(5)
            release["second"].set()
            assert reporting.wait(5)
            for value in ("third", "first"):
                notified = threading.Event()
                futures[value].add_done_callback(lambda _, event=notified: event.set())
                release[value].set()
                assert notified.wait(5)
        finally:
            resume.set()
            for event in release.values():
                event.set()

    monkeypatch.setattr(runner_module, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(runner_module, "run_course", process)
    with ThreadPoolExecutor(max_workers=1) as observer:
        observation = observer.submit(control)
        results = CourseRunner(client, 3, completed=completed).run(items)
        observation.result(timeout=5)
    assert reported == ["second", "third", "first"]
    assert reported == [result.course.id for result in results]


@pytest.mark.parametrize("item_count", [1, 5])
@pytest.mark.parametrize("action", ["cancel", "expired", "interrupt", "callback_error"])
def test_callback_aborts_before_replacement(monkeypatch, client, item_count, action):
    cloned = []
    clone = client.clone

    def tracked_clone():
        worker = clone()
        cloned.append(worker)
        return worker

    def process(worker, item, *, progress):
        return CourseResult(item, CourseStatus.SUCCESS)

    def completed(result, count, total):
        assert cloned[0].session.closed
        if action == "interrupt":
            raise KeyboardInterrupt
        if action == "callback_error":
            raise RuntimeError("synthetic callback error")
        client.mutation_coordinator.cancel(authentication_failed=action == "expired")

    error = {
        "cancel": RunCancelledError,
        "expired": AuthenticationExpiredError,
        "interrupt": KeyboardInterrupt,
        "callback_error": RuntimeError,
    }[action]
    monkeypatch.setattr(client, "clone", tracked_clone)
    monkeypatch.setattr(runner_module, "run_course", process)
    with pytest.raises(error):
        CourseRunner(client, 1, completed=completed).run(
            [course(str(index)) for index in range(item_count)]
        )
    assert len(cloned) == 1 and cloned[0].session.closed
    with pytest.raises(AuthenticationExpiredError if action == "expired" else RunCancelledError):
        client.mutation_coordinator.check_cancelled()


@pytest.mark.parametrize("expired", [False, True])
def test_cancellation_interrupts_completion_wait_and_waits_for_session_close(
    monkeypatch, client, expired
):
    started = threading.Event()
    release = threading.Event()
    shutdown = threading.Event()
    sessions = []
    completed = Mock()

    class RecordingExecutor(ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            shutdown.set()
            return super().shutdown(*args, **kwargs)

    def process(worker, item, *, progress):
        sessions.append(worker.session)
        started.set()
        assert release.wait(5)
        return CourseResult(item, CourseStatus.SUCCESS)

    def control():
        try:
            assert started.wait(5)
            client.mutation_coordinator.cancel(authentication_failed=expired)
            assert shutdown.wait(5)
            assert not sessions[0].closed
        finally:
            release.set()

    monkeypatch.setattr(runner_module, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(runner_module, "run_course", process)
    with ThreadPoolExecutor(max_workers=1) as observer:
        observation = observer.submit(control)
        with pytest.raises(AuthenticationExpiredError if expired else RunCancelledError):
            CourseRunner(client, 1, completed=completed).run([course("first"), course("second")])
        observation.result(timeout=5)
    completed.assert_not_called()
    assert len(sessions) == 1 and sessions[0].closed


@pytest.mark.parametrize("stage", ["clone", "close"])
def test_worker_resource_expiry_immediately_cancels_run(monkeypatch, client, stage):
    if stage == "clone":
        monkeypatch.setattr(
            client, "clone", Mock(side_effect=AuthenticationExpiredError("synthetic expiry"))
        )
    else:
        worker = client.clone()
        close = worker.close

        def expired_close():
            close()
            raise AuthenticationExpiredError("synthetic expiry")

        monkeypatch.setattr(worker, "close", expired_close)
        monkeypatch.setattr(client, "clone", lambda: worker)
        monkeypatch.setattr(
            runner_module,
            "run_course",
            lambda worker, item, **_: CourseResult(item, CourseStatus.SUCCESS),
        )
    with pytest.raises(AuthenticationExpiredError):
        CourseRunner(client, 1)._process_course(course("first"))
    with pytest.raises(AuthenticationExpiredError):
        client.mutation_coordinator.check_cancelled()
    if stage == "close":
        assert worker.session.closed


def test_runner_without_completion_callback(monkeypatch, client):
    started = []

    def process(worker, item, *, progress):
        progress(CourseProgress(item, "started"))
        return CourseResult(item, CourseStatus.SUCCESS)

    monkeypatch.setattr(runner_module, "run_course", process)
    results = CourseRunner(client, 1, progress=lambda event: started.append(event.course.id)).run(
        [course(str(index)) for index in range(3)]
    )
    assert started == ["0", "1", "2"]
    assert [result.course.id for result in results] == started


def test_empty_runner_does_not_open_executor_or_sessions(monkeypatch, client):
    executor = Mock()
    clone = Mock()
    completed = Mock()
    monkeypatch.setattr(runner_module, "ThreadPoolExecutor", executor)
    monkeypatch.setattr(client, "clone", clone)
    assert CourseRunner(client, 4, completed=completed).run([]) == []
    executor.assert_not_called()
    clone.assert_not_called()
    completed.assert_not_called()
