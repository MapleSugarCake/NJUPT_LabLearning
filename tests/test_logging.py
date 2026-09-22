import io
import logging
import re
import sys

import pytest

from labpass_cli.logging_utils import configure_logging, log_path
from njupt_auth.redaction import Redactor, safe_excerpt


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


def test_existing_file_untouched_and_disabled_logging_ignores_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "labpass_log.txt"
    path.write_text("original content", encoding="utf-8")
    with pytest.raises(FileExistsError), configure_logging(True, redactor=Redactor()):
        pytest.fail("exclusive creation must fail")
    with configure_logging(False, redactor=Redactor(), stream=io.StringIO()):
        logging.getLogger("labpass_cli").info("new content")
    assert path.read_text(encoding="utf-8") == "original content"


def test_log_directory_source_and_exe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert log_path() == tmp_path / "labpass_log.txt"
    executable = tmp_path / "different" / "labpass.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "extracted"), raising=False)
    assert log_path() == executable.parent / "labpass_log.txt"


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


def test_safe_excerpt_redacts_before_truncation_and_flattens_lines():
    redactor = Redactor()
    redactor.remember("secret+with/slash")
    result = redactor.excerpt("secret%2Bwith%2Fslash\n" + "x" * 600, limit=50)
    assert result.startswith("*** ") and len(result) == 51 and "\n" not in result
