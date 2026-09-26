"""校验课程 API 响应、转换领域模型并串行提交业务写入。

本文件定义：
    logger：课程 API 请求和解析过程的模块日志记录器。
    SafetyLabClient：使用已认证会话访问课程资源，并可为课程任务复制客户端。
    SafetyLabClient.__init__：保存认证资源和业务写入协调器。
    SafetyLabClient.__enter__：进入客户端上下文并提供当前实例。
    SafetyLabClient.__exit__：退出客户端上下文时关闭拥有的会话。
    SafetyLabClient.close：关闭当前客户端拥有的资源会话。
    SafetyLabClient.clone：为单门课程创建使用独立会话的客户端。
    SafetyLabClient._request_json：发送业务请求并统一校验 HTTP、认证状态及 JSON 外层结构。
    SafetyLabClient.list_courses：读取课程列表并规范化字段与重复记录。
    SafetyLabClient.list_questions：读取题目并保留提交标识、来源标识及答案的独立语义。
    SafetyLabClient.submit_answer：在共享写入锁内提交一条完整题目的答案。
    SafetyLabClient.submit_video_progress：一次上报课程列表中的视频完整秒数。
    SafetyLabClient.verify_video_finished：回读并核验完成标记及视频百分比。
    SafetyLabClient.read_video_status：回读课程状态，保留超过 100 的实际百分比。
    SafetyLabClient.finish_course：在共享写入锁内按课程列表标识标记完成。
    _first_text：按候选键顺序取得第一个非空文本值。
    _parse_decimal：解析视频总秒数或百分比，无效字段保留为未知。
    _required_text：提取必须存在且非空的题目标识字段。
    _normalize_answer：校验答案类型并按逗号规则规范化提交内容。
    _is_lock_conflict：兼容识别服务端两种写入锁冲突拼写。
    _is_finished：将课程完成标记转换为布尔值。
"""

import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import ROUND_DOWN, Decimal, InvalidOperation
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
    RunCancelledError,
    SubmissionUncertainError,
)
from .models import Course, Question

# 课程 API 请求和解析过程的模块日志记录器。
# 记录接口阶段、耗时和安全诊断，由 CLI 的运行级脱敏出口处理。
logger = logging.getLogger(__name__)


# 作用：使用已认证会话访问课程资源，并可为课程任务复制客户端。
# 说明：继承 AbstractContextManager；当前实例拥有传入会话，关闭时只释放该会话。
# 说明：副本拥有独立会话，但共享认证快照工厂和运行级 MutationCoordinator；本类不负责登录。
class SafetyLabClient(AbstractContextManager["SafetyLabClient"]):
    """使用已认证会话访问课程资源，并可为课程任务复制客户端。"""

    # 作用：保存认证资源和业务写入协调器。
    # 参数：
    #     self：当前实例。
    #     session：归当前客户端管理的已认证 requests 会话。
    #     api_base_url：实验室资源 API 基址，保存时去除尾斜杠。
    #     session_factory：用于创建独立已认证会话的可调用对象。
    #     mutation_coordinator：本轮所有客户端共享的写入锁与取消协调器。
    # 返回：无返回值（None）。
    # 说明：复用会话上的脱敏上下文；会话未提供时使用新的 Redactor。
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

    # 作用：进入客户端上下文并提供当前实例。
    # 参数：
    #     self：当前实例。
    # 返回：当前 SafetyLabClient 实例。
    def __enter__(self) -> Self:
        return self

    # 作用：退出客户端上下文时关闭拥有的会话。
    # 参数：
    #     self：当前实例。
    #     *_：上下文协议传入的异常信息，本方法只执行资源清理。
    # 返回：None，不抑制上下文内的异常。
    def __exit__(self, *_: object) -> None:
        self.close()

    # 作用：关闭当前客户端拥有的资源会话。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    # 说明：认证快照工厂由本轮认证结果在所有线程结束后统一关闭。
    def close(self) -> None:
        self.session.close()

    # 作用：为单门课程创建使用独立会话的客户端。
    # 参数：
    #     self：当前实例。
    # 返回：共享基址、会话工厂及写入协调器的新 SafetyLabClient。
    # 说明：新客户端由课程任务关闭；工厂已关闭等创建异常由调用方处理。
    def clone(self) -> "SafetyLabClient":
        """为单门课程创建使用独立会话的客户端。"""

        session = self._session_factory()
        return SafetyLabClient(
            session,
            api_base_url=self.api_base_url,
            session_factory=self._session_factory,
            mutation_coordinator=self.mutation_coordinator,
        )

    # 作用：发送业务请求并统一校验 HTTP、认证状态及 JSON 外层结构。
    # 参数：
    #     self：当前实例。
    #     method：HTTP 请求方法，决定网络故障和锁错误的分类。
    #     url：待访问的完整资源接口地址。
    #     endpoint_name：用于日志和错误说明的安全接口名称。
    #     require_result：是否要求 JSON 外层存在 result 键，不在此处校验其具体类型。
    #     **kwargs：透传给会话的查询参数、请求体等请求选项。
    # 返回：通过校验的完整 JSON 字典。
    # 说明：请求前检查取消，明确设置超时并禁止自动跳转；写入互斥由上层业务方法取得。
    # 说明：HTTP 或业务认证失效会取消整轮；写入超时、连接中断报告结果不确定，其他格式与业务错误分
    #     别抛出。
    # 说明：只输出脱敏且有界的服务端 message；原始错误页不写入异常。
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
            if isinstance(error_payload, dict):
                logger.debug(
                    "API 业务错误：%s；code=%s；message=%s",
                    endpoint_name,
                    self.redactor.excerpt(error_payload.get("code")),
                    self.redactor.excerpt(error_payload.get("message")),
                )
            if isinstance(error_payload, dict) and str(error_payload.get("code")) in {"401", "403"}:
                self.mutation_coordinator.cancel(authentication_failed=True)
                raise AuthenticationExpiredError("服务器报告登录状态已失效，请重新认证")
            if method.upper() == "POST" and _is_lock_conflict(response.text):
                raise LockConflictError(
                    f"{endpoint_name}失败：服务器写入锁冲突（未自动重试），请稍后核对网页状态"
                )
            # 错误页可能含有尚未登记的敏感值，因此只报告状态码，不输出原始正文。
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
        if success is False or failed_code or _is_lock_conflict(message):
            logger.debug(
                "API 业务错误：%s；code=%s；message=%s",
                endpoint_name,
                self.redactor.excerpt(code),
                message,
            )
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

    # 作用：读取课程列表并规范化字段与重复记录。
    # 参数：
    #     self：当前实例。
    # 返回：按首次出现顺序排列、按课程标识去重的 Course 列表。
    # 说明：空 result 视为空列表；同标识记录优先保留未完成项，名称缺失时生成课程占位名称。
    # 说明：列表结构、条目类型或课程标识无效时抛出 ResponseFormatError。
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
                duration_seconds=_parse_decimal(item.get("duration"), positive=True),
                video_percent=_parse_decimal(item.get("watchDuration"), percentage=True),
            )

            previous = unique.get(course_id)
            if previous is None or (previous.video_finished and not course.video_finished):
                unique[course_id] = course
            else:
                logger.debug("忽略重复课程记录：%s", course_id)
        return list(unique.values())

    # 作用：读取题目并保留提交标识、来源标识及答案的独立语义。
    # 参数：
    #     self：当前实例。
    #     course_id：课程列表中的课程标识，用于查询题目或标记课程完成。
    # 返回：与服务端返回顺序一致的 Question 列表，空 result 返回空列表。
    # 说明：提交标识取响应 id，所属课程取 courseId，题库来源取 questionId；
    #     答案交由规范化函数校验。
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

    # 作用：在共享写入锁内提交一条完整题目的答案。
    # 参数：
    #     self：当前实例。
    #     question：完整 Question，提供 submission_id、course_id 和规范化答案。
    # 返回：提交成功时返回 None。
    # 说明：载荷 questionId 取 submission_id，id 取 course_id，option 取 answer；不使用题库来源标
    #     识。
    # 说明：取得锁后检查取消，失败 POST 不自动重发，异常交给单课程流程处理。
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

    def submit_video_progress(self, course: Course) -> None:
        """截取总时长的整数秒，一次上报；不将不足一秒的尾部扩为一秒。"""
        duration = course.require_video_duration()
        watch_duration = format(duration.to_integral_value(rounding=ROUND_DOWN), "f")
        with self.mutation_coordinator.serialized():
            logger.debug("视频时长：总秒数=%s；上报整数秒数=%s", duration, watch_duration)
            self._request_json(
                "POST",
                self.api_base_url + "/jcedutec/courseSource/finishRate",
                "提交视频进度",
                require_result=False,
                json={"id": course.id, "watchDuration": watch_duration},
            )

    def verify_video_finished(self, course_id: str) -> None:
        """只回读一次课程列表；不轮询状态，也不重发任何写入。"""
        self.read_video_status(course_id).require_video_finished()

    def read_video_status(self, course_id: str) -> Course:
        """读取服务端真实状态，异常不能作为再次提交 finish 的依据。"""
        try:
            courses = self.list_courses()
        except (AuthenticationExpiredError, RunCancelledError):
            raise
        except (ApiError, NetworkError) as exc:
            raise ApiError(f"视频完成状态未确认：回读失败，{exc}") from exc
        course = next((item for item in courses if item.id == course_id), None)
        if course is None:
            raise ApiError("视频完成状态未确认：课程列表中缺少该课程，请到网页核对")
        logger.debug(
            "视频完成状态：课程=%s；完成标记=%s；进度百分比=%s",
            self.redactor.excerpt(course_id),
            course.finished,
            self.redactor.excerpt(course.video_percent),
        )
        return course

    # 作用：在共享写入锁内按课程列表标识标记完成。
    # 参数：
    #     self：当前实例。
    #     course_id：课程列表中的课程标识，用于查询题目或标记课程完成。
    # 返回：标记成功时返回 None。
    # 说明：载荷仅包含课程列表 id；取得锁后再次检查取消，并遵守 POST 不重放的约束。
    def finish_course(self, course_id: str) -> None:
        with self.mutation_coordinator.serialized():
            self._request_json(
                "POST",
                self.api_base_url + "/jcedutec/courseSource/finish",
                "标记课程完成",
                require_result=False,
                json={"id": course_id},
            )


# 作用：按候选键顺序取得第一个非空文本值。
# 参数：
#     item：当前待解析的服务端字段字典。
#     *keys：任意数量的候选字段名，按传入顺序作为优先级。
# 返回：去除两侧空白后的字符串，全部缺失或为空时返回 None。
def _first_text(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _parse_decimal(
    value: object, *, positive: bool = False, percentage: bool = False
) -> Decimal | None:
    """解析时长或百分比；无效字段保留为未知，不使其他课程无法执行。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if positive and number <= 0:
        return None
    if percentage and number < 0:
        return None
    return number


# 作用：提取必须存在且非空的题目标识字段。
# 参数：
#     item：当前待解析的服务端字段字典。
#     key：必填字段名，用于读取数据及构造错误说明。
#     index：从 1 开始的题目序号，仅用于格式错误说明。
# 返回：转换为字符串并去除两侧空白的字段值。
# 说明：字段缺失或为空时抛出带题目序号的 ResponseFormatError。
def _required_text(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if value is None or not str(value).strip():
        raise ResponseFormatError(f"第 {index} 道题目缺少 {key}")
    return str(value).strip()


# 作用：校验答案类型并按逗号规则规范化提交内容。
# 参数：
#     value：服务端 correctAnswer 原值，可为字符串或字符串列表。
#     index：从 1 开始的题目序号，仅用于格式错误说明。
# 返回：无逗号的字符串，或已清理空白的字符串列表。
# 说明：只有含逗号的字符串才拆分，AB 保持原样；列表必须非空且每项均为非空字符串。
# 说明：缺失、空选项及不支持类型均抛出 ResponseFormatError。
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


# 作用：兼容识别服务端两种写入锁冲突拼写。
# 参数：
#     message：待检查的服务端错误文本，比较时忽略字母大小写。
# 返回：包含 acquire lock fail 或 aquire lock fail 时为 True。
def _is_lock_conflict(message: str) -> bool:
    normalized = message.casefold()
    return "acquire lock fail" in normalized or "aquire lock fail" in normalized


# 作用：将课程完成标记转换为布尔值。
# 参数：
#     value：服务端 isFinish 原始值，字符串先去除两侧空白并转为小写。
# 返回：字符串 1、true、yes 或布尔真、等于数字 1 的值返回 True。
def _is_finished(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return value is True or value == 1
