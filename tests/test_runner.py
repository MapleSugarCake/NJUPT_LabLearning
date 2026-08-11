import threading
import time

import pytest

from labpass.exceptions import ApiError, AuthenticationExpiredError, SubmissionUncertainError
from labpass.models import Course, CourseStatus, Question
from labpass.runner import CourseRunner


class Tracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.clone_ids: list[int] = []
        self.events: list[tuple[str, str, str | None]] = []
        self.failed_courses: set[str] = set()
        self.uncertain_courses: set[str] = set()
        self.expired_courses: set[str] = set()


class FakeRootClient:
    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker

    def clone(self) -> "FakeWorkerClient":
        worker = FakeWorkerClient(self.tracker)
        self.tracker.clone_ids.append(id(worker))
        return worker


class FakeWorkerClient:
    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker

    def __enter__(self) -> "FakeWorkerClient":
        with self.tracker.lock:
            self.tracker.active += 1
            self.tracker.max_active = max(self.tracker.max_active, self.tracker.active)
        return self

    def __exit__(self, *_: object) -> None:
        with self.tracker.lock:
            self.tracker.active -= 1

    def list_questions(self, course_id: str) -> list[Question]:
        self.tracker.events.append((course_id, "questions", None))
        if course_id in self.tracker.expired_courses:
            raise AuthenticationExpiredError("expired")
        time.sleep(0.02)
        return [Question("q1", "A"), Question("q2", ["B", "C"])]

    def submit_answer(self, course_id: str, question: Question) -> None:
        self.tracker.events.append((course_id, "answer", question.id))
        if course_id in self.tracker.uncertain_courses:
            raise SubmissionUncertainError("uncertain")
        if course_id in self.tracker.failed_courses:
            raise ApiError("rejected")

    def finish_course(self, course_id: str) -> None:
        self.tracker.events.append((course_id, "finish", None))


def courses(count: int) -> list[Course]:
    return [Course(str(index), f"课程 {index}") for index in range(count)]


def test_runner_caps_concurrency_at_four_and_uses_independent_clients() -> None:
    tracker = Tracker()

    results = CourseRunner(FakeRootClient(tracker), workers=4).run(courses(8))

    assert tracker.max_active == 4
    assert len(tracker.clone_ids) == 8
    assert len(set(tracker.clone_ids)) == 8
    assert all(result.status is CourseStatus.SUCCESS for result in results)


def test_questions_are_submitted_sequentially_before_finish() -> None:
    tracker = Tracker()

    result = CourseRunner(FakeRootClient(tracker), workers=1).run(courses(1))[0]

    assert tracker.events == [
        ("0", "questions", None),
        ("0", "answer", "q1"),
        ("0", "answer", "q2"),
        ("0", "finish", None),
    ]
    assert result.answered_count == 2


def test_course_failure_does_not_stop_other_courses() -> None:
    tracker = Tracker()
    tracker.failed_courses.add("0")

    results = CourseRunner(FakeRootClient(tracker), workers=2).run(courses(2))
    by_id = {result.course.id: result for result in results}

    assert by_id["0"].status is CourseStatus.FAILED
    assert by_id["1"].status is CourseStatus.SUCCESS


def test_uncertain_post_is_marked_for_manual_verification() -> None:
    tracker = Tracker()
    tracker.uncertain_courses.add("0")

    result = CourseRunner(FakeRootClient(tracker), workers=1).run(courses(1))[0]

    assert result.status is CourseStatus.FAILED
    assert result.uncertain


def test_authentication_expiry_aborts_the_run() -> None:
    tracker = Tracker()
    tracker.expired_courses.add("0")

    with pytest.raises(AuthenticationExpiredError):
        CourseRunner(FakeRootClient(tracker), workers=1).run(courses(3))


@pytest.mark.parametrize("workers", [0, 5])
def test_runner_rejects_worker_counts_outside_range(workers: int) -> None:
    with pytest.raises(ValueError):
        CourseRunner(FakeRootClient(Tracker()), workers=workers)
