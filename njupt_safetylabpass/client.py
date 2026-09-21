"""Validated, thread-clonable client for the Lab Learning business API."""

import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Self

import requests

from njupt_auth.config import REQUEST_TIMEOUT
from njupt_auth.redaction import Redactor

from .coordination import MutationCoordinator
from .exceptions import (
    ApiError,
    AuthenticationExpiredError,
    LockConflictError,
    NetworkError,
    ResponseFormatError,
    SubmissionUncertainError,
)
from .models import Course, Question

logger = logging.getLogger(__name__)


class SafetyLabClient(AbstractContextManager["SafetyLabClient"]):
    """API client whose authenticated state can be cloned into worker Sessions."""

    def __init__(
        self,
        session: requests.Session,
        *,
        api_base_url: str,
        session_factory: Callable[[], requests.Session],
        mutation_coordinator: MutationCoordinator,
    ) -> None:
        self.session = session
        self.api_base_url = api_base_url.rstrip("/")
        self.redactor = getattr(session, "redactor", Redactor())
        self._session_factory = session_factory
        self.mutation_coordinator = mutation_coordinator

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    def clone(self) -> "SafetyLabClient":
        """Create an independent Session with a snapshot of authentication state."""

        session = self._session_factory()
        return SafetyLabClient(
            session,
            api_base_url=self.api_base_url,
            session_factory=self._session_factory,
            mutation_coordinator=self.mutation_coordinator,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        endpoint_name: str,
        *,
        require_result: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.mutation_coordinator.check_cancelled()
        started = time.perf_counter()
        logger.debug("API 请求开始：%s %s", method, endpoint_name)
        try:
            response = self.session.request(
                method, url, timeout=REQUEST_TIMEOUT, allow_redirects=False, **kwargs
            )
        except requests.Timeout:
            if method.upper() == "POST":
                raise SubmissionUncertainError(
                    f"{endpoint_name}请求超时，服务器是否已处理无法确认，请到网页核对"
                ) from None
            raise NetworkError(f"{endpoint_name}请求超时") from None
        except requests.RequestException:
            if method.upper() == "POST":
                raise SubmissionUncertainError(
                    f"{endpoint_name}连接中断，结果不确定；未重试，请到网页核对"
                ) from None
            raise NetworkError(f"{endpoint_name}网络请求失败") from None

        elapsed = time.perf_counter() - started
        logger.debug(
            "API 请求完成：%s %s -> HTTP %s（%.2fs）",
            method,
            endpoint_name,
            response.status_code,
            elapsed,
        )

        if response.status_code in {401, 403} or 300 <= response.status_code < 400:
            self.mutation_coordinator.cancel(authentication_failed=True)
            raise AuthenticationExpiredError("登录状态已失效或接口发生跳转，请重新运行并登录")
        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict) and str(error_payload.get("code")) in {"401", "403"}:
                self.mutation_coordinator.cancel(authentication_failed=True)
                raise AuthenticationExpiredError("服务器报告登录状态已失效，请重新认证")
            if method.upper() == "POST" and _is_lock_conflict(response.text):
                raise LockConflictError(
                    f"{endpoint_name}失败：服务器写入锁冲突（未自动重试），请稍后核对网页状态"
                )
            # Error pages may contain unknown secrets. Report status, not raw bodies.
            raise ApiError(f"{endpoint_name}失败（HTTP {response.status_code}）")

        try:
            payload = response.json()
        except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
            raise ResponseFormatError(f"{endpoint_name}返回的不是有效 JSON") from None
        if not isinstance(payload, dict):
            raise ResponseFormatError(f"{endpoint_name}返回的 JSON 顶层不是对象")

        code = payload.get("code")
        success = payload.get("success")
        failed_code = code is not None and str(code) not in {"0", "200"}
        message = self.redactor.excerpt(payload.get("message") or "服务器返回业务错误")
        if str(code) in {"401", "403"}:
            self.mutation_coordinator.cancel(authentication_failed=True)
            raise AuthenticationExpiredError(f"登录状态已失效：{message}")
        if method.upper() == "POST" and _is_lock_conflict(message):
            raise LockConflictError(
                f"{endpoint_name}失败：服务器写入锁冲突（未自动重试），请稍后核对网页状态"
            )
        if success is False or failed_code:
            raise ApiError(f"{endpoint_name}失败：{message}")
        if success is not True or code is None:
            raise ResponseFormatError(f"{endpoint_name}响应缺少有效 success/code")
        if require_result and "result" not in payload:
            raise ResponseFormatError(f"{endpoint_name}响应缺少 result 字段")
        return payload

    def list_courses(self) -> list[Course]:
        payload = self._request_json(
            "GET",
            self.api_base_url + "/jcedutec/courseSource/myCourseList",
            "获取课程列表",
            require_result=True,
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
        params = {"id": course_id}
        payload = self._request_json(
            "GET",
            self.api_base_url + "/jcedutec/courseSource/queryCourseQuestionRelaByMainId",
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
            submission_id = _required_text(item, "id", index)
            question_course_id = _required_text(item, "courseId", index)
            answer = _normalize_answer(item.get("correctAnswer"), index)
            questions.append(
                Question(
                    submission_id=submission_id,
                    course_id=question_course_id,
                    answer=answer,
                    source_question_id=_first_text(item, "questionId"),
                    kind=_first_text(item, "kind", "kind_dictText"),
                )
            )
        logger.debug(
            "课程 %s：已解析 %d 道题（提交 questionId 取响应 id，提交 id 取响应 courseId）",
            course_id,
            len(questions),
        )
        return questions

    def submit_answer(self, question: Question) -> None:
        with self.mutation_coordinator.serialized():
            self._request_json(
                "POST",
                self.api_base_url + "/jcedutec/courseSource/submitAnswer",
                "提交题目答案",
                require_result=False,
                json={
                    "questionId": question.submission_id,
                    "id": question.course_id,
                    "option": question.answer,
                },
            )

    def finish_course(self, course_id: str) -> None:
        with self.mutation_coordinator.serialized():
            self._request_json(
                "POST",
                self.api_base_url + "/jcedutec/courseSource/finish",
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


def _required_text(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if value is None or not str(value).strip():
        raise ResponseFormatError(f"第 {index} 道题目缺少 {key}")
    return str(value).strip()


def _normalize_answer(value: object, index: int) -> str | list[str]:
    if isinstance(value, str):
        answer = value.strip()
        if not answer:
            raise ResponseFormatError(f"第 {index} 道题目缺少 correctAnswer")
        if "," not in answer:
            return answer
        options = [option.strip() for option in answer.split(",")]
        if any(not option for option in options):
            raise ResponseFormatError(f"第 {index} 道题目的 correctAnswer 格式无效")
        return options

    if isinstance(value, list):
        if not value:
            raise ResponseFormatError(f"第 {index} 道题目缺少 correctAnswer")
        if not all(isinstance(option, str) for option in value):
            raise ResponseFormatError(f"第 {index} 道题目的 correctAnswer 类型无效")
        options = [option.strip() for option in value]
        if any(not option for option in options):
            raise ResponseFormatError(f"第 {index} 道题目的 correctAnswer 格式无效")
        return options

    if value is None:
        raise ResponseFormatError(f"第 {index} 道题目缺少 correctAnswer")
    raise ResponseFormatError(f"第 {index} 道题目的 correctAnswer 类型无效")


def _is_lock_conflict(message: str) -> bool:
    normalized = message.casefold()
    return "acquire lock fail" in normalized or "aquire lock fail" in normalized


def _is_finished(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return value is True or value == 1
