"""顺序执行单门课程，并返回结构化进度和结果。

本文件定义：
    logger：单课程业务异常的模块日志记录器。
    run_course：读取课程题目、按返回顺序提交，并在全部成功后标记完成。
"""

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

# 单课程业务异常的模块日志记录器。
# 仅在调试级别记录异常堆栈；具体脱敏和处理器配置由 CLI 负责。
logger = logging.getLogger(__name__)


# 作用：读取课程题目、按返回顺序提交，并在全部成功后标记完成。
# 参数：
#     client：本课程使用的业务客户端，带有共享取消信号和写入协调器。
#     course：课程列表中的课程对象，完成接口沿用它的课程标识。
#     progress：可选进度回调，接收开始及逐题提交后的 CourseProgress。
#         默认 None，此时不发送进度事件。
# 返回：包含题目计数、成功提交数和错误状态的 CourseResult。
# 说明：无题课程直接标记完成；任一答题失败后不发送完成请求。
# 说明：认证失效登记全局取消后继续向上传播；普通取消同样传播，其他业务错误转为单课程失败结果。
# 说明：方法不创建线程或关闭传入客户端，课程会话生命周期由外层任务管理。
def run_course(
    client: SafetyLabClient,
    course: Course,
    *,
    progress: Callable[[CourseProgress], None] | None = None,
) -> CourseResult:
    """读取课程题目、按返回顺序提交，并在全部成功后标记完成。"""
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
