"""Interactive console workflow with no command-line argument parsing."""

import getpass
import logging
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TextIO

from njupt_auth import (
    AuthenticationResult,
    AuthError,
    NetworkEnvironment,
    authenticate,
    authenticate_in_browser,
    import_token,
)
from njupt_auth.redaction import Redactor
from njupt_safetylabpass import CourseProgress, CourseResult, MutationCoordinator, SafetyLabClient
from njupt_safetylabpass.exceptions import LabPassError

from .config import DEFAULT_WORKERS, LOG_FILENAME, RunSettings
from .logging_utils import configure_logging
from .models import RunSummary
from .runner import CourseRunner
from .version import get_version

logger = logging.getLogger(__name__)
Input = Callable[[str], str]
Output = Callable[[str], None]


def _yes_no(prompt: str, input_fn: Input, output_fn: Output) -> bool:
    while True:
        answer = input_fn(prompt).strip().lower()
        if answer in {"", "n"}:
            return False
        if answer == "y":
            return True
        output_fn("请输入 y 或 n（回车默认 n）")


def _settings(input_fn: Input, output_fn: Output) -> RunSettings:
    if not _yes_no("是否自定义设置？[y/N]：", input_fn, output_fn):
        return RunSettings()
    debug = _yes_no("是否开启 debug 日志？[y/N]：", input_fn, output_fn)
    while True:
        value = input_fn("课程并发线程数 [1–4，默认 4]：").strip()
        try:
            return RunSettings(debug=debug, workers=int(value) if value else DEFAULT_WORKERS)
        except ValueError:
            output_fn("线程数必须是 1–4 的整数")


def _network(input_fn: Input, output_fn: Output) -> NetworkEnvironment:
    while True:
        answer = input_fn("网络环境 [1 校外 VPN（默认）/ 2 校园网直连]：").strip()
        if answer in {"", "1"}:
            return NetworkEnvironment.EXTRANET
        if answer == "2":
            return NetworkEnvironment.INTRANET
        output_fn("请输入 1 或 2")


def _authenticate(
    environment: NetworkEnvironment,
    input_fn: Input,
    secret_input: Input,
    redactor: Redactor,
) -> AuthenticationResult:
    username = input_fn("请输入学号：").strip()
    password = input_fn("请输入密码（明文显示）：")
    redactor.remember(username, password)
    try:
        return authenticate(username, password, environment=environment, redactor=redactor)
    except AuthError as exc:
        logger.error("账号密码登录失败：%s", exc)
        logger.debug("账号密码认证详情", exc_info=True)
    finally:
        password = ""  # noqa: F841 - release this reference, without claiming memory erasure.

    while True:
        choice = input_fn("登录回退 [1 浏览器 / 2 校园网 Token / 0 退出（默认）]：").strip()
        if choice in {"", "0"}:
            raise AuthError("用户未完成登录")
        try:
            if choice == "1":
                logger.info("请在新打开的 Edge 中完成实验室登录；等待上限 5 分钟")
                return authenticate_in_browser(environment=environment, redactor=redactor)
            if choice == "2":
                logger.warning("Token 模式仅适用于校园网，请确认设备已连接校园网")
                token = secret_input("请输入 X-Access-Token（隐藏输入）：")
                redactor.remember(token)
                try:
                    return import_token(token, redactor=redactor)
                finally:
                    token = ""  # noqa: F841
            logger.warning("请输入 0、1 或 2")
        except AuthError as exc:
            logger.error("登录未完成：%s", exc)
            logger.debug("回退认证详情", exc_info=True)


def _progress(event: CourseProgress) -> None:
    if event.stage == "started":
        logger.info("开始处理：%s（%s）", event.course.name, event.course.id)
    else:
        logger.debug(
            "课程 %s：已提交 %d/%d 道题",
            event.course.id,
            event.answered_count,
            event.question_count,
        )


def _completed(result: CourseResult, count: int, total: int) -> None:
    if result.succeeded:
        logger.info(
            "[%d/%d] 完成：%s（%s），已提交 %d 道题",
            count,
            total,
            result.course.name,
            result.course.id,
            result.answered_count,
        )
    else:
        logger.error(
            "[%d/%d] 失败：%s（%s）— %s",
            count,
            total,
            result.course.name,
            result.course.id,
            result.error,
        )


def _print_summary(summary: RunSummary) -> None:
    logger.info(
        "执行汇总：发现 %d，跳过 %d，成功 %d，失败 %d，总耗时 %.1f 秒",
        summary.discovered,
        summary.already_finished,
        summary.succeeded,
        summary.failed,
        summary.elapsed_seconds,
    )
    for result in summary.results:
        if not result.succeeded:
            suffix = "（提交结果不确定，请到网页核对）" if result.uncertain else ""
            logger.error(
                "- %s（%s）：%s%s", result.course.name, result.course.id, result.error, suffix
            )
    if not summary.failed:
        logger.info("所有待处理课程均已完成，请到网页确认最终状态")


def execute(
    settings: RunSettings,
    *,
    environment: NetworkEnvironment,
    input_fn: Input,
    secret_input: Input,
    redactor: Redactor,
) -> int:
    """Run authentication and course processing under configured logging."""
    started = time.perf_counter()
    authentication = _authenticate(environment, input_fn, secret_input, redactor)
    coordinator = MutationCoordinator()
    with (
        authentication,
        SafetyLabClient(
            authentication.session,
            api_base_url=authentication.api_base_url,
            session_factory=authentication.session_factory,
            mutation_coordinator=coordinator,
        ) as client,
    ):
        logger.info("登录成功，正在获取课程列表…")
        courses = client.list_courses()
        pending = [course for course in courses if not course.finished]
        finished = len(courses) - len(pending)
        logger.info(
            "发现 %d 门课程：%d 门已完成，%d 门待处理", len(courses), finished, len(pending)
        )
        results = CourseRunner(
            client,
            settings.workers,
            progress=_progress,
            completed=_completed,
        ).run(pending)
    summary = RunSummary(len(courses), finished, tuple(results), time.perf_counter() - started)
    _print_summary(summary)
    return 1 if summary.failed else 0


def run_cli(
    *,
    input_fn: Input | None = None,
    secret_input: Input | None = None,
    output_fn: Output = print,
    stream: TextIO | None = None,
) -> int:
    """Return 0/1/2/130; inputs are injectable for completely offline tests."""
    input_fn = input if input_fn is None else input_fn
    secret_input = getpass.getpass if secret_input is None else secret_input
    redactor = Redactor()
    try:
        current_version = get_version()
        output_fn(f"LabPass {current_version}")
        settings = _settings(input_fn, output_fn)
        with configure_logging(settings.debug, redactor=redactor, stream=stream):
            try:
                logger.debug("本次运行版本：%s；课程线程数：%d", current_version, settings.workers)
                environment = _network(input_fn, output_fn)
                return execute(
                    settings,
                    environment=environment,
                    input_fn=input_fn,
                    secret_input=secret_input,
                    redactor=redactor,
                )
            except (AuthError, LabPassError) as exc:
                logger.error("运行终止：%s", exc)
                logger.debug("运行终止详情", exc_info=True)
                return 2
            except EOFError:
                logger.error("输入已结束，运行终止")
                return 2
            except KeyboardInterrupt:
                logger.warning("用户已中断运行")
                return 130
            except Exception:
                logger.error("程序发生未预期错误；请在启动时开启 debug 查看详情")
                logger.debug("未预期错误详情", exc_info=True)
                return 2
    except FileExistsError:
        output_fn(f"错误：{LOG_FILENAME} 已存在；请先自行移动或删除，程序不会覆盖或追加")
        return 2
    except OSError:
        output_fn("错误：无法读取版本元数据或创建调试日志，请检查目录权限")
        return 2
    except EOFError:
        output_fn("输入已结束，运行终止")
        return 2
    except KeyboardInterrupt:
        output_fn("用户已中断运行")
        return 130
    except Exception:
        output_fn("启动失败，请检查安装及项目版本元数据")
        return 2
    finally:
        redactor.clear()


def entrypoint() -> None:
    exit_code = run_cli()
    if getattr(sys, "frozen", False):
        with suppress(EOFError, KeyboardInterrupt):
            input("按回车键退出…")
    raise SystemExit(exit_code)
