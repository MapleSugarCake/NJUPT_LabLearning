"""Interactive command-line interface for LabPass."""

import argparse
import getpass
import logging
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress

from . import __version__
from .auth import AuthenticationResult, authenticate_automatically, authenticate_with_token
from .client import LabPassClient
from .config import DEFAULT_WORKERS, MAX_WORKERS
from .exceptions import AuthenticationError, AuthenticationExpiredError, LabPassError
from .logging_utils import configure_logging
from .models import RunSummary
from .runner import CourseRunner

logger = logging.getLogger(__name__)


def _worker_count(value: str) -> int:
    try:
        workers = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("线程数必须是整数") from None
    if not 1 <= workers <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"线程数必须在 1 到 {MAX_WORKERS} 之间")
    return workers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labpass",
        description="南京邮电大学实验室安全教育课程辅助脚本",
    )
    parser.add_argument(
        "--workers",
        type=_worker_count,
        default=DEFAULT_WORKERS,
        metavar="1-4",
        help="并发处理课程的线程数（默认：4）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="在控制台显示脱敏后的调试信息和异常堆栈",
    )
    parser.add_argument(
        "--login-mode",
        choices=("auto", "token"),
        default="auto",
        help="登录方式：auto 自动登录，token 校园网手动 Token（默认：auto）",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="冻结版程序结束时不等待回车",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _automatic_login(
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
) -> AuthenticationResult:
    username = input_fn("请输入学号：").strip()
    password = secret_input("【不显示已输入密码】请输入密码：")
    try:
        return authenticate_automatically(username, password)
    finally:
        password = ""  # noqa: F841 - explicitly release the plain-text reference.


def _token_login(secret_input: Callable[[str], str]) -> AuthenticationResult:
    logger.warning("手动 Token 模式使用校内直连地址，请确认设备已连接校园网")
    token = secret_input("【不显示已输入Token】请输入 X-Access-Token：")
    try:
        return authenticate_with_token(token)
    finally:
        token = ""  # noqa: F841 - explicitly release the plain-text reference.


def _authenticate(
    login_mode: str,
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
) -> AuthenticationResult:
    if login_mode == "token":
        return _token_login(secret_input)

    try:
        return _automatic_login(input_fn, secret_input)
    except AuthenticationError as exc:
        logger.error("自动登录失败：%s", exc)
        logger.debug("自动登录异常详情", exc_info=True)
        try:
            answer = input_fn("是否切换到校园网手动 Token 登录？[y/N]：").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            raise
        return _token_login(secret_input)


def execute(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    secret_input: Callable[[str], str] = getpass.getpass,
) -> int:
    started = time.perf_counter()
    logger.info("LabPass %s", __version__)
    logger.info("仅处理课程学习与课程答题；考试仍需本人手动完成")
    logger.info("本次最多使用 %d 个课程线程", args.workers)

    try:
        authentication = _authenticate(args.login_mode, input_fn, secret_input)
    except AuthenticationError as exc:
        logger.error("登录终止：%s", exc)
        return 2

    try:
        with LabPassClient(authentication.session, authentication.endpoints) as client:
            logger.info("正在获取课程列表…")
            courses = client.list_courses()
            pending = [course for course in courses if not course.finished]
            finished = len(courses) - len(pending)
            logger.info(
                "发现 %d 门课程：%d 门已完成，%d 门待处理",
                len(courses),
                finished,
                len(pending),
            )
            if finished:
                logger.info("已完成课程将自动跳过")

            results = CourseRunner(client, args.workers).run(pending)
    except AuthenticationExpiredError as exc:
        logger.error("任务中止：%s", exc)
        return 2
    except LabPassError as exc:
        logger.error("任务无法启动：%s", exc)
        logger.debug("任务启动异常详情", exc_info=True)
        return 2

    summary = RunSummary(
        discovered=len(courses),
        already_finished=finished,
        results=tuple(results),
        elapsed_seconds=time.perf_counter() - started,
    )
    _print_summary(summary)
    return 1 if summary.failed else 0


def _print_summary(summary: RunSummary) -> None:
    logger.info("-" * 56)
    logger.info(
        "执行汇总：发现 %d，跳过 %d，成功 %d，失败 %d，总耗时 %.1f 秒",
        summary.discovered,
        summary.already_finished,
        summary.succeeded,
        summary.failed,
        summary.elapsed_seconds,
    )
    if summary.failed:
        logger.error("以下课程需要检查：")
        for result in summary.results:
            if not result.succeeded:
                suffix = "（提交结果不确定，请到网页核对）" if result.uncertain else ""
                logger.error(
                    "- %s（%s）：%s%s", result.course.name, result.course.id, result.error, suffix
                )
    else:
        logger.info("所有待处理课程均已完成，请到网页确认最终状态")


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.debug)
    try:
        return execute(args)
    except KeyboardInterrupt:
        logger.warning("用户已中断运行")
        return 130
    except Exception:
        logger.error("程序发生未预期错误；请使用 --debug 重试")
        logger.debug("未预期错误详情", exc_info=True)
        return 2


def entrypoint() -> None:
    args = build_parser().parse_args()
    configure_logging(args.debug)
    try:
        exit_code = execute(args)
    except KeyboardInterrupt:
        logger.warning("用户已中断运行")
        exit_code = 130
    except Exception:
        logger.error("程序发生未预期错误；请使用 --debug 重试")
        logger.debug("未预期错误详情", exc_info=True)
        exit_code = 2
    finally:
        if getattr(sys, "frozen", False) and not args.no_pause:
            with suppress(EOFError, KeyboardInterrupt):
                input("按回车键退出…")
    raise SystemExit(exit_code)
