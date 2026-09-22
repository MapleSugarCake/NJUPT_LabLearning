"""配置单次运行的控制台与文件日志，并统一脱敏。

本文件定义：
    RedactingFormatter：对完整日志文本及异常堆栈进行脱敏。
    RedactingFormatter.__init__：选择日志格式并保存共享脱敏上下文。
    RedactingFormatter.format：格式化一条日志及可能附带的异常堆栈后脱敏。
    log_path：按源码或冻结程序形态确定固定日志路径。
    configure_logging：在上下文内安装本轮日志处理器，并在退出时恢复原配置。
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from njupt_auth.redaction import Redactor

from .config import LOG_FILENAME


# 作用：对完整日志文本及异常堆栈进行脱敏。
# 说明：继承 logging.Formatter，先执行标准格式化，再使用运行级 Redactor 过滤结果。
# 说明：调试模式包含日期时间、级别与线程名，普通模式只保留消息文本。
class RedactingFormatter(logging.Formatter):
    # 作用：选择日志格式并保存共享脱敏上下文。
    # 参数：
    #     self：当前实例。
    #     redactor：运行级脱敏上下文，所有日志出口共用。
    #     debug：是否启用调试日志；决定日志级别、详细格式及文件出口。
    # 返回：无返回值（None）。
    def __init__(self, redactor: Redactor, *, debug: bool) -> None:
        super().__init__(
            "%(asctime)s | %(levelname)-7s | %(threadName)s | %(message)s"
            if debug
            else "%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.redactor = redactor

    # 作用：格式化一条日志及可能附带的异常堆栈后脱敏。
    # 参数：
    #     self：当前实例。
    #     record：日志系统提供的 LogRecord，可能包含消息参数及异常信息。
    # 返回：适合输出到控制台或文件的安全字符串。
    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))


# 作用：按源码或冻结程序形态确定固定日志路径。
# 参数：无。
# 返回：指向 labpass_log.txt 的 Path 对象。
# 说明：源码使用当前工作目录，冻结程序使用 sys.executable 所在目录，不使用解压目录。
def log_path() -> Path:
    """按源码或冻结程序形态确定固定日志路径。"""
    directory = (
        Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    )
    return directory / LOG_FILENAME


# 作用：在上下文内安装本轮日志处理器，并在退出时恢复原配置。
# 参数：
#     debug：是否启用调试日志；决定日志级别、详细格式及文件出口。
#     redactor：运行级脱敏上下文，所有日志出口共用。
#     stream：控制台日志写入的文本流；省略时使用 sys.stdout，便于测试捕获。 默认值为 None。
# 返回：供 with 使用的上下文管理器，配置完成后产出 None。
# 说明：debug 开启时以独占创建模式建立 UTF-8 日志文件；同名冲突或目录不可写直接向上传播。
# 说明：debug 关闭时只配置控制台出口；三个应用包使用同一组脱敏处理器。
# 说明：正常退出、初始化失败或上下文异常时均关闭已创建的处理器，并恢复原日志配置。
@contextmanager
def configure_logging(
    debug: bool, *, redactor: Redactor, stream: TextIO | None = None
) -> Iterator[None]:
    """在上下文内安装本轮日志处理器，并在退出时恢复原配置。"""
    handlers: list[logging.Handler] = []
    previous = []
    try:
        if debug:
            handlers.append(logging.FileHandler(log_path(), mode="x", encoding="utf-8"))
        handlers.append(logging.StreamHandler(stream if stream is not None else sys.stdout))
        for handler in handlers:
            handler.setLevel(logging.DEBUG if debug else logging.INFO)
            handler.setFormatter(RedactingFormatter(redactor, debug=debug))
        for name in ("njupt_auth", "njupt_safetylabpass", "labpass_cli"):
            logger = logging.getLogger(name)
            previous.append((logger, logger.handlers[:], logger.level, logger.propagate))
            logger.handlers = handlers[:]
            logger.setLevel(logging.DEBUG if debug else logging.INFO)
            logger.propagate = False
        yield
    finally:
        for logger, old_handlers, level, propagate in previous:
            logger.handlers = old_handlers
            logger.setLevel(level)
            logger.propagate = propagate
        for handler in handlers:
            handler.close()
