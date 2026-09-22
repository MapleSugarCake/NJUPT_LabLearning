"""为一轮课程业务提供共享写入锁及取消信号。

本文件定义：
    MutationCoordinator：协调所有课程客户端的串行写入和全局取消。
    MutationCoordinator.__init__：创建写入互斥锁及两个运行状态事件。
    MutationCoordinator.cancel：设置运行取消状态并可同时登记全局认证失效。
    MutationCoordinator.check_cancelled：在请求或任务开始前检查运行状态。
    MutationCoordinator.serialized：在共享互斥锁内提供允许执行写入的上下文。
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .exceptions import AuthenticationExpiredError, RunCancelledError


# 作用：协调所有课程客户端的串行写入和全局取消。
# 说明：主客户端及其课程副本共享同一实例，协调器不拥有 Session 或执行认证。
# 说明：取消和认证失效分别记录，取得写入锁后必须再次检查，阻止排队请求继续发送。
class MutationCoordinator:
    """协调所有课程客户端的串行写入和全局取消。"""

    # 作用：创建写入互斥锁及两个运行状态事件。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）；初始状态允许任务执行。
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._authentication_failed = threading.Event()

    # 作用：设置运行取消状态并可同时登记全局认证失效。
    # 参数：
    #     self：当前实例。
    #     authentication_failed：是否因登录状态失效而取消，默认 False。
    #         设为 True 时同时登记全局认证失败事件。
    # 返回：无返回值（None）；后续检查将抛出对应取消异常。
    # 说明：只能阻止后续请求，不能撤回已经发出的 HTTP 请求。
    def cancel(self, *, authentication_failed: bool = False) -> None:
        if authentication_failed:
            self._authentication_failed.set()
        self._cancelled.set()

    # 作用：在请求或任务开始前检查运行状态。
    # 参数：
    #     self：当前实例。
    # 返回：运行正常时返回 None。
    # 说明：优先抛出 AuthenticationExpiredError，其次抛出 RunCancelledError；不改变已有取消状态。
    def check_cancelled(self) -> None:
        if self._authentication_failed.is_set():
            raise AuthenticationExpiredError("任务因登录状态失效而取消")
        if self._cancelled.is_set():
            raise RunCancelledError("任务已取消")

    # 作用：在共享互斥锁内提供允许执行写入的上下文。
    # 参数：
    #     self：当前实例。
    # 返回：供 with 使用的上下文管理器，进入后产出 None。
    # 说明：先取得锁再检查取消状态；上下文退出或异常传播时释放锁，保证同时最多一个写入。
    @contextmanager
    def serialized(self) -> Iterator[None]:
        with self._lock:
            self.check_cancelled()
            yield
