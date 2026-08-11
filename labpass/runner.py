"""Bounded course-level concurrency and result aggregation."""

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from .client import LabPassClient
from .exceptions import (
    AuthenticationExpiredError,
    LabPassError,
    SubmissionUncertainError,
)
from .models import Course, CourseResult, CourseStatus

logger = logging.getLogger(__name__)


class CourseRunner:
    def __init__(self, client: LabPassClient, workers: int) -> None:
        if not 1 <= workers <= 4:
            raise ValueError("workers 必须在 1 到 4 之间")
        self.client = client
        self.workers = workers
        self._authentication_failed = threading.Event()

    def run(self, courses: list[Course]) -> list[CourseResult]:
        if not courses:
            return []

        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="course")
        futures: dict[Future[CourseResult], Course] = {
            executor.submit(self._process_course, course): course for course in courses
        }
        results: list[CourseResult] = []
        completed = 0
        try:
            for future in as_completed(futures):
                course = futures[future]
                try:
                    result = future.result()
                except AuthenticationExpiredError:
                    self._authentication_failed.set()
                    for pending in futures:
                        pending.cancel()
                    raise

                results.append(result)
                completed += 1
                if result.succeeded:
                    logger.info(
                        "[%d/%d] 完成：%s（%s），已提交 %d 道题",
                        completed,
                        len(courses),
                        course.name,
                        course.id,
                        result.answered_count,
                    )
                else:
                    logger.error(
                        "[%d/%d] 失败：%s（%s）— %s",
                        completed,
                        len(courses),
                        course.name,
                        course.id,
                        result.error,
                    )
            return results
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def _process_course(self, course: Course) -> CourseResult:
        if self._authentication_failed.is_set():
            raise AuthenticationExpiredError("任务因登录状态失效而取消")

        logger.info("开始处理：%s（%s）", course.name, course.id)
        questions = []
        answered = 0
        try:
            with self.client.clone() as worker_client:
                questions = worker_client.list_questions(course.id)
                for question in questions:
                    if self._authentication_failed.is_set():
                        raise AuthenticationExpiredError("任务因登录状态失效而取消")
                    worker_client.submit_answer(course.id, question)
                    answered += 1
                    logger.debug(
                        "课程 %s：题目 %s 提交成功（%d/%d）",
                        course.id,
                        question.id,
                        answered,
                        len(questions),
                    )
                worker_client.finish_course(course.id)
            return CourseResult(
                course=course,
                status=CourseStatus.SUCCESS,
                question_count=len(questions),
                answered_count=answered,
            )
        except AuthenticationExpiredError:
            self._authentication_failed.set()
            raise
        except SubmissionUncertainError as exc:
            logger.debug("课程 %s 的提交结果不确定", course.id, exc_info=True)
            return CourseResult(
                course=course,
                status=CourseStatus.FAILED,
                question_count=len(questions),
                answered_count=answered,
                error=str(exc),
                uncertain=True,
            )
        except LabPassError as exc:
            logger.debug("课程 %s 处理失败", course.id, exc_info=True)
            return CourseResult(
                course=course,
                status=CourseStatus.FAILED,
                question_count=len(questions),
                answered_count=answered,
                error=str(exc),
            )
        except Exception:
            logger.debug("课程 %s 发生未预期错误", course.id, exc_info=True)
            return CourseResult(
                course=course,
                status=CourseStatus.FAILED,
                question_count=len(questions),
                answered_count=answered,
                error="发生未预期错误；请使用 --debug 重试并查看详情",
            )
