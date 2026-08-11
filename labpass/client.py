"""Validated, thread-clonable client for the Lab Learning business API."""

import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Self

import requests

from .config import REQUEST_TIMEOUT, ApiEndpoints
from .exceptions import (
    ApiError,
    AuthenticationExpiredError,
    NetworkError,
    ResponseFormatError,
    SubmissionUncertainError,
)
from .http import configure_session
from .logging_utils import safe_excerpt
from .models import Course, Question

logger = logging.getLogger(__name__)


class LabPassClient(AbstractContextManager["LabPassClient"]):
    """API client whose authenticated state can be cloned into worker Sessions."""

    def __init__(
        self,
        session: requests.Session,
        endpoints: ApiEndpoints,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        self.session = session
        self.endpoints = endpoints
        self._session_factory = session_factory

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    def clone(self) -> "LabPassClient":
        """Create an independent Session with a snapshot of authentication state."""

        session = configure_session(self._session_factory())
        session.headers.update(dict(self.session.headers))
        session.cookies.update(self.session.cookies)
        return LabPassClient(session, self.endpoints, session_factory=self._session_factory)

    def _request_json(
        self,
        method: str,
        url: str,
        endpoint_name: str,
        *,
        require_result: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        logger.debug("API 请求开始：%s %s", method, endpoint_name)
        try:
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.Timeout:
            if method.upper() == "POST":
                raise SubmissionUncertainError(
                    f"{endpoint_name}请求超时，服务器是否已处理无法确认，请到网页核对"
                ) from None
            raise NetworkError(f"{endpoint_name}请求超时") from None
        except requests.RequestException as exc:
            raise NetworkError(f"{endpoint_name}网络请求失败：{safe_excerpt(exc)}") from None

        elapsed = time.perf_counter() - started
        logger.debug(
            "API 请求完成：%s %s -> HTTP %s（%.2fs）",
            method,
            endpoint_name,
            response.status_code,
            elapsed,
        )

        if response.status_code in {401, 403}:
            raise AuthenticationExpiredError("登录状态已失效，请重新运行并登录")
        if response.status_code >= 400:
            excerpt = safe_excerpt(response.text)
            detail = f"：{excerpt}" if excerpt else ""
            raise ApiError(f"{endpoint_name}失败（HTTP {response.status_code}）{detail}")

        try:
            payload = response.json()
        except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
            raise ResponseFormatError(
                f"{endpoint_name}返回的不是有效 JSON：{safe_excerpt(response.text)}"
            ) from None
        if not isinstance(payload, dict):
            raise ResponseFormatError(f"{endpoint_name}返回的 JSON 顶层不是对象")

        code = payload.get("code")
        success = payload.get("success")
        failed_code = code is not None and str(code) not in {"0", "200"}
        if success is False or failed_code:
            message = safe_excerpt(payload.get("message") or "服务器返回业务错误")
            if str(code) in {"401", "403"}:
                raise AuthenticationExpiredError(f"登录状态已失效：{message}")
            raise ApiError(f"{endpoint_name}失败：{message}")
        if require_result and "result" not in payload:
            raise ResponseFormatError(f"{endpoint_name}响应缺少 result 字段")
        return payload

    def _vpn_params(self) -> dict[str, object]:
        if not self.endpoints.requires_vpn_timestamp:
            return {}
        timestamp = self.session.cookies.get("vpn_timestamp")
        return {"_t": timestamp} if timestamp else {}

    def list_courses(self) -> list[Course]:
        payload = self._request_json(
            "GET",
            self.endpoints.courses,
            "获取课程列表",
            require_result=True,
            params=self._vpn_params(),
        )
        raw_courses = payload.get("result")
        if raw_courses is None:
            raw_courses = []
        if not isinstance(raw_courses, list):
            raise ResponseFormatError("获取课程列表的 result 不是数组")

        unique: dict[str, Course] = {}
        for index, item in enumerate(raw_courses, start=1):
            if not isinstance(item, dict):
                raise ResponseFormatError(f"第 {index} 条课程数据不是对象")
            course_id = item.get("id")
            if course_id is None or not str(course_id).strip():
                raise ResponseFormatError(f"第 {index} 条课程数据缺少 id")
            course_id = str(course_id)
            type_name = _first_text(item, "type_dictText", "typeName")
            name = _first_text(item, "courseName", "name", "title", "type_dictText")
            course = Course(
                id=course_id,
                name=name or f"课程 {course_id}",
                finished=_is_finished(item.get("isFinish")),
                type_name=type_name,
            )

            previous = unique.get(course_id)
            if previous is None or (previous.finished and not course.finished):
                unique[course_id] = course
            else:
                logger.debug("忽略重复课程记录：%s", course_id)
        return list(unique.values())

    def list_questions(self, course_id: str) -> list[Question]:
        params = self._vpn_params()
        params["id"] = course_id
        payload = self._request_json(
            "GET",
            self.endpoints.questions,
            "获取课程题目",
            require_result=True,
            params=params,
        )
        raw_questions = payload.get("result")
        if raw_questions is None:
            return []
        if not isinstance(raw_questions, list):
            raise ResponseFormatError("获取课程题目的 result 不是数组")

        questions: list[Question] = []
        for index, item in enumerate(raw_questions, start=1):
            if not isinstance(item, dict):
                raise ResponseFormatError(f"第 {index} 道题目数据不是对象")
            question_id = item.get("questionId", item.get("id"))
            answer = item.get("correctAnswer")
            if question_id is None or not str(question_id).strip():
                raise ResponseFormatError(f"第 {index} 道题目缺少 questionId")
            if answer is None or answer == "" or answer == []:
                raise ResponseFormatError(f"第 {index} 道题目缺少 correctAnswer")
            questions.append(Question(id=str(question_id), answer=answer))
        return questions

    def submit_answer(self, course_id: str, question: Question) -> None:
        self._request_json(
            "POST",
            self.endpoints.submit_answer,
            "提交题目答案",
            require_result=False,
            json={"id": course_id, "option": question.answer, "questionId": question.id},
        )

    def finish_course(self, course_id: str) -> None:
        self._request_json(
            "POST",
            self.endpoints.finish_course,
            "标记课程完成",
            require_result=False,
            json={"id": course_id},
        )


def _first_text(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _is_finished(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return value is True or value == 1
