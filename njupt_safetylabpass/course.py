"""顺序执行单门课程，并返回结构化进度和结果。

本文件定义：
    logger：单课程业务异常的模块日志记录器。
    run_course：顺序答题、上报完整视频时长、标记完成并回读核验。
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


# 作用：读取课程题目、按返回顺序提交，然后上报视频、标记完成并核验状态。
# 参数：
#     client：本课程使用的业务客户端，带有共享取消信号和写入协调器。
#     course：课程列表中的课程对象，完成接口沿用它的课程标识。
#     progress：可选进度回调，接收开始及逐题提交后的 CourseProgress。
#         默认 None，此时不发送进度事件。
# 返回：包含题目计数、成功提交数和错误状态的 CourseResult。
# 说明：答题后一次上报视频总时长、标记完成并回读核验；无题课程也执行视频步骤。
# 说明：首次完成写入成功且有效百分比不等于 100 时，最多补交一次 finish 再核验。
# 说明：认证失效登记全局取消后继续向上传播；普通取消同样传播，其他业务错误转为单课程失败结果。
# 说明：方法不创建线程或关闭传入客户端，课程会话生命周期由外层任务管理。
def run_course(
    client: SafetyLabClient,
    course: Course,
    *,
    progress: Callable[[CourseProgress], None] | None = None,
) -> CourseResult:
    """顺序答题、上报完整视频时长，并在完成状态回读达标后返回成功。"""
    questions = []
    answered = 0
    try:
        client.mutation_coordinator.check_cancelled()
        if progress:
            progress(CourseProgress(course, "started"))
        course.require_video_duration()
        questions = client.list_questions(course.id)
        for question in questions:
            client.submit_answer(question)
            answered += 1
            if progress:
                progress(CourseProgress(course, "answered", answered, len(questions)))
        client.submit_video_progress(course)
        client.finish_course(course.id)
        status = client.read_video_status(course.id)
        if status.video_percent is not None and status.video_percent != 100:
            logger.debug("视频进度不等于 100%，追加一次完成提交，不重发视频进度或答案")
            client.finish_course(course.id)
            status = client.read_video_status(course.id)
        status.require_video_finished()
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
