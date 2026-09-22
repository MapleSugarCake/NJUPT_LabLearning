"""实现交互配置、登录回退、课程执行和进程退出流程。

本文件定义：
    logger：控制台交互与执行汇总的模块日志记录器。
    Input：提示输入函数的类型别名。
    Output：提示输出函数的类型别名。
    _yes_no：循环读取大小写不敏感的 y/n 选择。
    _settings：通过首个交互选择构造仅本次生效的运行设置。
    _network：在认证前要求用户明确选择网络环境。
    _authenticate：执行账号密码认证，并在失败后提供登录方式回退。
    _progress：将结构化课程进度渲染为适当级别的日志。
    _completed：输出一门课程的完成结果和整体进度计数。
    _print_summary：输出本轮课程统计及逐门失败原因。
    execute：在已配置日志的环境下完成认证、课程筛选及调度。
    run_cli：组织控制台完整流程并将成功、失败和中断转换为退出码。
    entrypoint：运行交互流程并向系统发出退出状态。
"""

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

# 控制台交互与执行汇总的模块日志记录器。
# 配置完成后通过统一脱敏出口记录进度、错误和调试信息。
logger = logging.getLogger(__name__)
# 提示输入函数的类型别名。
# 表示接收一个字符串提示并返回字符串的 Callable，用于普通或隐藏输入注入。
Input = Callable[[str], str]
# 提示输出函数的类型别名。
# 表示接收字符串并返回 None 的 Callable，用于启动提示及可测试输出。
Output = Callable[[str], None]


# 作用：循环读取大小写不敏感的 y/n 选择。
# 参数：
#     prompt：每次询问时传给输入函数的提示文本。
#     input_fn：接收提示文本并返回输入字符串的可调用对象。
#     output_fn：输出普通提示文本的可调用对象。
# 返回：输入 y 返回 True；输入 n 或直接回车返回 False。
# 说明：非法输入输出说明并重新询问；EOF 和中断交给外层统一处理。
def _yes_no(prompt: str, input_fn: Input, output_fn: Output) -> bool:
    while True:
        answer = input_fn(prompt).strip().lower()
        if answer in {"", "n"}:
            return False
        if answer == "y":
            return True
        output_fn("请输入 y 或 n（回车默认 n）")


# 作用：通过首个交互选择构造仅本次生效的运行设置。
# 参数：
#     input_fn：接收提示文本并返回输入字符串的可调用对象。
#     output_fn：输出普通提示文本的可调用对象。
# 返回：包含 debug 开关及课程线程数的 RunSettings。
# 说明：默认 debug 关闭、线程数 4；自定义时只接受 1–4 的整数，非法输入继续询问。
def _settings(input_fn: Input, output_fn: Output) -> RunSettings:
    if not _yes_no("是否自定义设置？默认请选N[y/N]：", input_fn, output_fn):
        return RunSettings()
    debug = _yes_no("是否开启 debug 日志？[y/N]：", input_fn, output_fn)
    while True:
        value = input_fn("课程并发线程数 [1–4，默认 4]：").strip()
        try:
            return RunSettings(debug=debug, workers=int(value) if value else DEFAULT_WORKERS)
        except ValueError:
            output_fn("线程数必须是 1–4 的整数")


# 作用：在认证前要求用户明确选择网络环境。
# 参数：
#     input_fn：接收提示文本并返回输入字符串的可调用对象。
#     output_fn：输出普通提示文本的可调用对象。
# 返回：校外 VPN 或校园网直连对应的 NetworkEnvironment。
# 说明：回车和选项 1 对应校外，选项 2 对应校园网；不发请求进行自动探测。
def _network(input_fn: Input, output_fn: Output) -> NetworkEnvironment:
    while True:
        answer = input_fn("网络环境 [1 校外 VPN（默认）/ 2 校园网直连]：").strip()
        if answer in {"", "1"}:
            return NetworkEnvironment.EXTRANET
        if answer == "2":
            return NetworkEnvironment.INTRANET
        output_fn("请输入 1 或 2")


# 作用：执行账号密码认证，并在失败后提供登录方式回退。
# 参数：
#     environment：登录前明确选择的校园网直连或校外 VPN 环境。
#     input_fn：接收提示文本并返回输入字符串的可调用对象。
#     secret_input：用于隐藏输入 Token 的可调用对象，不用于密码输入。
#     redactor：本轮共享的脱敏上下文，用于凭据登记及全部日志输出。
# 返回：所选方式成功后的 AuthenticationResult。
# 说明：密码通过普通 input_fn 明文读取并保留两侧空白；Token 使用 secret_input 隐藏读取。
# 说明：账号密码失败后可选浏览器、校园网 Token 或退出，不自动重复提交密码。
# 说明：认证材料登记到共享脱敏上下文；退出或回退失败通过安全异常和日志交给外层处理。
def _authenticate(
    environment: NetworkEnvironment,
    input_fn: Input,
    secret_input: Input,
    redactor: Redactor,
) -> AuthenticationResult:
    username = input_fn("请输入学号：").strip()
    password = input_fn("请输入密码：")
    redactor.remember(username, password)
    try:
        return authenticate(username, password, environment=environment, redactor=redactor)
    except AuthError as exc:
        logger.error("账号密码登录失败：%s", exc)
        logger.debug("账号密码认证详情", exc_info=True)
    finally:
        password = ""  # noqa: F841 - 释放此处的密码引用，不代表擦除内存。

    while True:
        choice = input_fn("登录回退 [1 浏览器 / 2 手动 Token / 0 退出（默认）]：").strip()
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
                    token = ""  # noqa: F841 - 释放此处的 Token 引用，不代表擦除内存。
            logger.warning("请输入 0、1 或 2")
        except AuthError as exc:
            logger.error("登录未完成：%s", exc)
            logger.debug("回退认证详情", exc_info=True)


# 作用：将结构化课程进度渲染为适当级别的日志。
# 参数：
#     event：包含课程、阶段及题目计数的 CourseProgress。
# 返回：无返回值（None）；写入进度日志。
# 说明：开始事件使用普通信息级别，逐题答题进度仅在调试级别输出。
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


# 作用：输出一门课程的完成结果和整体进度计数。
# 参数：
#     result：刚结束的课程结果，包含状态、计数和安全错误说明。
#     count：本轮已经收集到的课程结果数量。
#     total：本轮待处理课程的总数。
# 返回：无返回值（None）；写入成功或失败日志。
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


# 作用：输出本轮课程统计及逐门失败原因。
# 参数：
#     summary：包含发现、跳过、处理结果和总耗时的 RunSummary。
# 返回：无返回值（None）；写入最终汇总日志。
# 说明：对提交结果不确定的课程附加网页核对说明；无失败时提示确认网页最终状态。
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


# 作用：在已配置日志的环境下完成认证、课程筛选及调度。
# 参数：
#     settings：已校验的本轮运行设置，提供并发课程线程数。
#     environment：登录前明确选择的校园网直连或校外 VPN 环境。
#     input_fn：接收提示文本并返回输入字符串的可调用对象。
#     secret_input：用于隐藏输入 Token 的可调用对象，不用于密码输入。
#     redactor：本轮共享的脱敏上下文，用于凭据登记及全部日志输出。
# 返回：全部待处理课程成功或为空时返回 0，至少一门失败时返回 1。
# 说明：创建唯一运行级写入协调器，跳过已经完成的课程并渲染进度与汇总。
# 说明：线程结束后关闭主客户端和认证结果；认证、课程列表或全局失效异常交由 run_cli 转换退出码。
def execute(
    settings: RunSettings,
    *,
    environment: NetworkEnvironment,
    input_fn: Input,
    secret_input: Input,
    redactor: Redactor,
) -> int:
    """在已配置日志的环境下完成认证、课程筛选及调度。"""
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


# 作用：组织控制台完整流程并将成功、失败和中断转换为退出码。
# 参数：
#     input_fn：普通输入函数；省略时使用 input，包含明文密码输入。 默认值为 None。
#     secret_input：隐藏 Token 输入函数；省略时使用 getpass.getpass。 默认值为 None。
#     output_fn：启动阶段提示输出函数，默认使用 print。 默认值为 print。
#     stream：日志控制台出口的文本流；省略时使用标准输出。 默认值为 None。
# 返回：整数退出码：0 为成功，1 为单课程失败，2 为启动或全局错误，130 为用户中断。
# 说明：先输出版本并询问设置，再配置日志、选择网络及认证；日志创建冲突在认证前返回错误。
# 说明：正常或异常退出均离开日志上下文并清理脱敏登记，测试可注入全部输入输出以保持离线。
def run_cli(
    *,
    input_fn: Input | None = None,
    secret_input: Input | None = None,
    output_fn: Output = print,
    stream: TextIO | None = None,
) -> int:
    """组织控制台完整流程并将成功、失败和中断转换为退出码。"""
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


# 作用：运行交互流程并向系统发出退出状态。
# 参数：无。
# 返回：抛出携带退出码的 SystemExit，不返回值。
# 说明：冻结的 EXE 在结束前等待回车，并容忍暂停阶段的 EOF 或中断；源码入口直接退出。
def entrypoint() -> None:
    exit_code = run_cli()
    if getattr(sys, "frozen", False):
        with suppress(EOFError, KeyboardInterrupt):
            input("按回车键退出…")
    raise SystemExit(exit_code)
