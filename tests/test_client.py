from collections.abc import Iterable

import pytest
import requests
from conftest import make_response

from labpass.client import LabPassClient
from labpass.config import ApiEndpoints
from labpass.exceptions import (
    AuthenticationExpiredError,
    ResponseFormatError,
    SubmissionUncertainError,
)
from labpass.http import configure_session
from labpass.models import Question

ENDPOINTS = ApiEndpoints(
    courses="https://example.test/courses",
    questions="https://example.test/questions",
    submit_answer="https://example.test/answer",
    finish_course="https://example.test/finish",
    requires_vpn_timestamp=False,
)


class QueueSession(requests.Session):
    def __init__(self, responses: Iterable[requests.Response]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


class TimeoutSession(requests.Session):
    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        raise requests.Timeout


def test_course_list_normalizes_fields_and_deduplicates() -> None:
    session = QueueSession(
        [
            make_response(
                {
                    "success": True,
                    "code": 200,
                    "result": [
                        {"id": "1", "courseName": "旧名称", "isFinish": "1"},
                        {"id": "1", "courseName": "待完成课程", "isFinish": 0},
                        {"id": 2, "type_dictText": "安全基础", "isFinish": True},
                    ],
                }
            )
        ]
    )

    courses = LabPassClient(session, ENDPOINTS).list_courses()

    assert [(course.id, course.name, course.finished) for course in courses] == [
        ("1", "待完成课程", False),
        ("2", "安全基础", True),
    ]


def test_empty_question_result_is_valid() -> None:
    session = QueueSession([make_response({"success": True, "code": 200, "result": None})])

    assert LabPassClient(session, ENDPOINTS).list_questions("course") == []


def test_question_requires_id_and_answer() -> None:
    session = QueueSession(
        [make_response({"success": True, "code": 200, "result": [{"questionId": "q1"}]})]
    )

    with pytest.raises(ResponseFormatError, match="correctAnswer"):
        LabPassClient(session, ENDPOINTS).list_questions("course")


def test_http_authentication_failure_is_global() -> None:
    session = QueueSession([make_response(status=401, text="expired")])

    with pytest.raises(AuthenticationExpiredError):
        LabPassClient(session, ENDPOINTS).list_courses()


def test_malformed_course_result_is_rejected() -> None:
    session = QueueSession([make_response({"success": True, "code": 200, "result": {}})])

    with pytest.raises(ResponseFormatError, match="不是数组"):
        LabPassClient(session, ENDPOINTS).list_courses()


def test_post_timeout_is_reported_as_uncertain_and_not_retried() -> None:
    session = TimeoutSession()

    with pytest.raises(SubmissionUncertainError, match="无法确认"):
        LabPassClient(session, ENDPOINTS).submit_answer("course", Question("q1", "A"))


def test_retry_adapter_only_allows_get_requests() -> None:
    session = configure_session(requests.Session())
    retry = session.get_adapter("https://").max_retries

    assert retry.total == 2
    assert retry.allowed_methods == frozenset({"GET"})
