"""在独立临时目录验证日志开关、路径、独占创建和脱敏。

本文件定义：
    test_debug_controls_both_outputs_and_redacts_tracebacks：
        验证调试开关控制双出口及完整异常堆栈脱敏。
    test_existing_file_untouched_and_disabled_logging_ignores_it：
        验证同名文件不能覆盖，关闭调试时也不改动已有文件。
    test_log_directory_source_and_exe：验证源码使用工作目录而 EXE 使用可执行文件目录。
    test_structured_redaction_includes_tgc_and_full_student_number：
        验证结构化密码、Cookie、票据、网关凭据和学号都被遮盖。
    test_safe_excerpt_redacts_before_truncation_and_flattens_lines：
        验证先脱敏再截断，并把多行诊断合并为单行。
"""

import io
import logging
import re
import sys

import pytest

from labpass_cli.logging_utils import configure_logging, log_path
from njupt_auth.redaction import Redactor, safe_excerpt


# 作用：验证调试开关控制双出口及完整异常堆栈脱敏。
# 参数：
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
#     debug：参数化的调试日志开关，分别检查开启与关闭行为。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：断言开启时文件与控制台一致且含时间，关闭时无文件或调试消息；退出后处理器已移除。
@pytest.mark.parametrize("debug", [True, False])
def test_debug_controls_both_outputs_and_redacts_tracebacks(tmp_path, monkeypatch, debug):
    monkeypatch.chdir(tmp_path)
    stream = io.StringIO()
    redactor = Redactor()
    redactor.remember(
        "fake password with spaces", "fake-resource-token", "B2099123456", "fake-ticket"
    )
    with configure_logging(debug, redactor=redactor, stream=stream):
        logger = logging.getLogger("njupt_auth.test")
        logger.debug("debug marker")
        logger.info("progress marker")
        try:
            raise ValueError(
                "fake password with spaces fake-resource-token B2099123456 "
                "ticket=fake-ticket Cookie: one=first; two=second"
            )
        except ValueError:
            logger.debug("synthetic traceback", exc_info=True)
    console = stream.getvalue()
    path = tmp_path / "labpass_log.txt"
    assert "progress marker" in console
    assert ("debug marker" in console) is debug
    assert path.exists() is debug
    if debug:
        file_output = path.read_text(encoding="utf-8")
        assert console == file_output
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", file_output)
        assert "Traceback" in file_output
        for secret in (
            "fake password with spaces",
            "fake-resource-token",
            "B2099123456",
            "fake-ticket",
            "first",
            "second",
        ):
            assert secret not in file_output
    assert not logging.getLogger("njupt_auth").handlers


# 作用：验证同名文件不能覆盖，关闭调试时也不改动已有文件。
# 参数：
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：预先写入固定内容；开启时应独占创建失败，关闭时保留原内容。
def test_existing_file_untouched_and_disabled_logging_ignores_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "labpass_log.txt"
    path.write_text("original content", encoding="utf-8")
    with pytest.raises(FileExistsError), configure_logging(True, redactor=Redactor()):
        pytest.fail("exclusive creation must fail")
    with configure_logging(False, redactor=Redactor(), stream=io.StringIO()):
        logging.getLogger("labpass_cli").info("new content")
    assert path.read_text(encoding="utf-8") == "original content"


# 作用：验证源码使用工作目录而 EXE 使用可执行文件目录。
# 参数：
#     tmp_path：pytest 提供的独立临时目录，用于隔离文件操作。
#     monkeypatch：pytest 替换夹具，用于临时替换对象或环境，测试结束后自动恢复。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：模拟冻结标记、可执行文件及解压目录，确保解压目录不会成为日志位置。
def test_log_directory_source_and_exe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert log_path() == tmp_path / "labpass_log.txt"
    executable = tmp_path / "different" / "labpass.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "extracted"), raising=False)
    assert log_path() == executable.parent / "labpass_log.txt"


# 作用：验证结构化密码、Cookie、票据、网关凭据和学号都被遮盖。
# 参数：
#     message：参数化的虚构敏感信息文本，涵盖键值、JSON 形态和认证头。
#     secrets：当前文本中必须完全消失的虚构秘密值列表。
# 返回：无返回值（None）；测试函数通过断言验证预期。
@pytest.mark.parametrize(
    "message,secrets",
    [
        (
            'password="fake pass with spaces" token=abc123; '
            "ticket=ST-example&sessionId=fake-session",
            ["fake pass with spaces", "abc123", "ST-example", "fake-session"],
        ),
        ("{'tgc': 'fake-tgc', 'Cookie': 'one=abc; two=def'}", ["fake-tgc", "abc", "def"]),
        (
            'https://example.test/?entoken=fake-gateway&next=page {"enToken": "fake other"}',
            ["fake-gateway", "fake other"],
        ),
        (
            "Authorization: Bearer fake-bearer\nB2099123456 2099123456",
            ["fake-bearer", "B2099123456", "2099123456"],
        ),
    ],
)
def test_structured_redaction_includes_tgc_and_full_student_number(message, secrets):
    result = safe_excerpt(message)
    assert all(secret not in result for secret in secrets)


# 作用：验证先脱敏再截断，并把多行诊断合并为单行。
# 参数：无。
# 返回：无返回值（None）；测试函数通过断言验证预期。
# 说明：使用 URL 编码的虚构秘密值和超长正文，检查固定遮盖前缀、最大摘要长度及无换行。
def test_safe_excerpt_redacts_before_truncation_and_flattens_lines():
    redactor = Redactor()
    redactor.remember("secret+with/slash")
    result = redactor.excerpt("secret%2Bwith%2Fslash\n" + "x" * 600, limit=50)
    assert result.startswith("*** ") and len(result) == 51 and "\n" not in result
