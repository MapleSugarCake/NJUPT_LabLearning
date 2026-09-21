"""Course-level scheduling and progress callbacks, without business sequencing."""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from njupt_safetylabpass import Course, CourseProgress, CourseResult, SafetyLabClient, run_course
from njupt_safetylabpass.exceptions import AuthenticationExpiredError, RunCancelledError
from njupt_safetylabpass.models import CourseStatus

from .config import RunSettings

logger = logging.getLogger(__name__)


class CourseRunner:
    """Own the thread pool; every submitted course opens and closes its own client."""

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
