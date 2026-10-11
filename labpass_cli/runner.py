"""管理课程级线程池、完成回调和全局取消清理。

本文件定义：
    logger：课程任务启动与释放异常的模块日志记录器。
    CourseRunner：调度不同课程并等待每个课程会话完成释放。
    CourseRunner.__init__：保存主客户端、受限并发度及可选回调。
    CourseRunner._process_course：为一门课程打开独立客户端并执行顺序业务。
    CourseRunner.run：报告完成结果后补位，并保证取消清理和线程池关闭。
"""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, SimpleQueue

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
# 说明：已提交且尚未报告的任务不超过并发度，完成回调返回后才提交下一门课程。
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
        except AuthenticationExpiredError:
            self.client.mutation_coordinator.cancel(authentication_failed=True)
            raise
        except RunCancelledError:
            self.client.mutation_coordinator.cancel()
            raise
        except Exception:
            logger.debug("课程任务启动或释放失败", exc_info=True)
            return CourseResult(
                course,
                CourseStatus.FAILED,
                error="课程任务无法启动或释放；请开启 debug 查看详情",
            )

    # 作用：按完成通知顺序报告课程结果，然后补充任务并保证线程池关闭。
    # 参数：
    #     self：当前实例。
    #     courses：已经由调用方筛选出的待处理课程列表。
    # 返回：完成顺序排列的 CourseResult 列表；输入为空时返回空列表。
    # 说明：全局认证失效时登记相应取消状态；任何非正常结束都会取消待执行任务并阻止新的写入。
    # 说明：首次最多提交 workers 门；副本关闭、结果收集和完成回调结束后，才补充下一门。
    # 说明：完成通知只入队，结果回调仍由本方法的调用线程执行；短超时等待支持全局取消。
    # 说明：关闭线程池时等待已开始任务释放资源，不承诺撤回已经发送的请求。
    def run(self, courses: list[Course]) -> list[CourseResult]:
        if not courses:
            return []
        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="course")
        futures: set[Future[CourseResult]] = set()
        notifications: SimpleQueue[Future[CourseResult]] = SimpleQueue()
        pending = iter(courses)
        completed_normally = False

        def submit_next() -> None:
            self.client.mutation_coordinator.check_cancelled()
            course = next(pending, None)
            if course is not None:
                future = executor.submit(self._process_course, course)
                futures.add(future)
                future.add_done_callback(notifications.put)

        try:
            for _ in range(min(self.workers, len(courses))):
                submit_next()
            results = []
            while futures:
                try:
                    future = notifications.get(timeout=0.1)
                except Empty:
                    self.client.mutation_coordinator.check_cancelled()
                    continue
                futures.remove(future)
                result = future.result()
                results.append(result)
                if self.completed:
                    self.completed(result, len(results), len(courses))
                submit_next()
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
