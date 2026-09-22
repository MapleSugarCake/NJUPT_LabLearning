"""管理课程级线程池、完成回调和全局取消清理。

本文件定义：
    logger：课程任务启动与释放异常的模块日志记录器。
    CourseRunner：调度不同课程并等待每个课程会话完成释放。
    CourseRunner.__init__：保存主客户端、受限并发度及可选回调。
    CourseRunner._process_course：为一门课程打开独立客户端并执行顺序业务。
    CourseRunner.run：提交课程任务，按完成顺序收集结果并保证线程池关闭。
"""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from njupt_safetylabpass import Course, CourseProgress, CourseResult, SafetyLabClient, run_course
from njupt_safetylabpass.exceptions import AuthenticationExpiredError, RunCancelledError
from njupt_safetylabpass.models import CourseStatus

from .config import RunSettings

# 课程任务启动与释放异常的模块日志记录器。
# 仅在调试模式输出安全处理后的堆栈，常规结果通过结构化对象交给 CLI。
logger = logging.getLogger(__name__)


# 作用：调度不同课程并等待每个课程会话完成释放。
# 说明：并发度通过 RunSettings 校验，允许整数 1–4；单课程步骤由 run_course 执行。
# 说明：每项任务创建和关闭独立客户端，主线程按完成顺序收集结果并调用完成回调。
class CourseRunner:
    """调度不同课程并等待每个课程会话完成释放。"""

    # 作用：保存主客户端、受限并发度及可选回调。
    # 参数：
    #     self：当前实例。
    #     client：提供会话副本和共享写入协调器的主业务客户端。
    #     workers：课程线程数，必须为 1–4 的整数，非法值抛出 ValueError。
    #     progress：课程进度回调，在课程任务所在线程调用；省略时不报告事件。 默认值为 None。
    #     completed：完成回调，接收结果、已完成数量和总数，由收集结果的线程调用。 默认值为 None。
    # 返回：无返回值（None）。
    def __init__(
        self,
        client: SafetyLabClient,
        workers: int,
        *,
        progress: Callable[[CourseProgress], None] | None = None,
        completed: Callable[[CourseResult, int, int], None] | None = None,
    ) -> None:
        self.client = client
        self.workers = RunSettings(workers=workers).workers
        self.progress = progress
        self.completed = completed

    # 作用：为一门课程打开独立客户端并执行顺序业务。
    # 参数：
    #     self：当前实例。
    #     course：待处理的课程对象，使用它的课程列表标识执行业务。
    # 返回：该课程的 CourseResult。
    # 说明：开始前检查取消；上下文结束时关闭课程会话。认证失效和取消继续传播，其他任务异常转为失败
    #     结果。
    def _process_course(self, course: Course) -> CourseResult:
        self.client.mutation_coordinator.check_cancelled()
        try:
            with self.client.clone() as worker:
                return run_course(worker, course, progress=self.progress)
        except (AuthenticationExpiredError, RunCancelledError):
            raise
        except Exception:
            logger.debug("课程任务启动或释放失败", exc_info=True)
            return CourseResult(
                course,
                CourseStatus.FAILED,
                error="课程任务无法启动或释放；请开启 debug 查看详情",
            )

    # 作用：提交课程任务，按完成顺序收集结果并保证线程池关闭。
    # 参数：
    #     self：当前实例。
    #     courses：已经由调用方筛选出的待处理课程列表。
    # 返回：完成顺序排列的 CourseResult 列表；输入为空时返回空列表。
    # 说明：全局认证失效时登记相应取消状态；任何非正常结束都会取消待执行任务并阻止新的写入。
    # 说明：关闭线程池时等待已开始任务释放资源，不承诺撤回已经发送的请求。
    def run(self, courses: list[Course]) -> list[CourseResult]:
        if not courses:
            return []
        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="course")
        futures: dict[Future[CourseResult], Course] = {}
        completed_normally = False
        try:
            for course in courses:
                futures[executor.submit(self._process_course, course)] = course
            results = []
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if self.completed:
                    self.completed(result, len(results), len(courses))
            completed_normally = True
            return results
        except AuthenticationExpiredError:
            self.client.mutation_coordinator.cancel(authentication_failed=True)
            raise
        finally:
            if not completed_normally:
                self.client.mutation_coordinator.cancel()
                for future in futures:
                    future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
