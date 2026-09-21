"""Single-course business workflow; no threads or console interaction."""

import logging
from collections.abc import Callable

from .client import SafetyLabClient
from .exceptions import (
    AuthenticationExpiredError,
    LabPassError,
    RunCancelledError,
    SubmissionUncertainError,
)
from .models import Course, CourseProgress, CourseResult, CourseStatus

logger = logging.getLogger(__name__)


def run_course(
    client: SafetyLabClient,
    course: Course,
    *,
    progress: Callable[[CourseProgress], None] | None = None,
) -> CourseResult:
    """Read, submit in response order, and finish only after every answer succeeds."""
    questions = []
    answered = 0
    try:
        client.mutation_coordinator.check_cancelled()
        if progress:
            progress(CourseProgress(course, "started"))
        questions = client.list_questions(course.id)
        for question in questions:
            client.submit_answer(question)
            answered += 1
            if progress:
                progress(CourseProgress(course, "answered", answered, len(questions)))
        client.finish_course(course.id)
        return CourseResult(course, CourseStatus.SUCCESS, len(questions), answered)
    except AuthenticationExpiredError:
        client.mutation_coordinator.cancel(authentication_failed=True)
        raise
    except RunCancelledError:
        raise
    except LabPassError as exc:
        logger.debug("单课程业务处理失败", exc_info=True)
        return CourseResult(
            course,
            CourseStatus.FAILED,
            len(questions),
            answered,
            error=str(exc),
            uncertain=isinstance(exc, SubmissionUncertainError),
        )
    except Exception:
        logger.debug("单课程发生未预期错误", exc_info=True)
        return CourseResult(
            course,
            CourseStatus.FAILED,
            len(questions),
            answered,
            error="发生未预期错误；请在启动时开启 debug 查看详情",
        )
