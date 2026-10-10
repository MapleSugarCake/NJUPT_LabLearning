"""离线验证发包计数、普通模式动态显示、日志协调和异常退出清理。"""

import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock

import pytest
from conftest import make_response, success
from test_cli import Inputs, fake_auth_result

from labpass_cli import cli
from labpass_cli.progress import PacketProgressDisplay
from njupt_auth.redaction import Redactor
from njupt_safetylabpass import (
    Course,
    CourseProgress,
    MutationCoordinator,
    SafetyLabClient,
    run_course,
)


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    "outcome,exit_code",
    [("success", 0), ("failure", 1), ("expired", 2), ("interrupt", 130), ("pending", 1)],
)
def test_cli_video_packet_display_and_cleanup(
    monkeypatch, tmp_path, install_transport, debug, terminal, outcome, exit_code
):
    monkeypatch.chdir(tmp_path)
    authentication = fake_auth_result()
    monkeypatch.setattr(cli, "authenticate", Mock(return_value=authentication))
    stream = TerminalStream() if terminal else io.StringIO()
    packets = 0
    finishes = 0

    def handle(session, request, kwargs):
        nonlocal packets, finishes
        if request.url.endswith("myCourseList"):
            return make_response(
                success(
                    [
                        {
                            "id": "course",
                            "courseName": "synthetic",
                            "duration": "35.9",
                            "isFinish": finishes > 0,
                            "watchDuration": "100" if finishes and outcome == "success" else "0",
                        }
                    ]
                )
            )
        if request.method == "GET":
            return make_response(success([]))
        if request.url.endswith("finishRate"):
            packets += 1
            assert json.loads(request.body)["watchDuration"] == str(packets * 10)
            if not debug:
                assert f"已发 {packets - 1}/3 包" in stream.getvalue()
            if packets == 2:
                if outcome == "expired":
                    return make_response(status=401)
                if outcome == "interrupt":
                    raise KeyboardInterrupt
                if outcome == "failure":
                    return make_response({"success": False, "code": 400, "message": "rejected"})
        else:
            assert request.url.endswith("/finish")
            finishes += 1
            if not debug:
                assert "已发 3/3 包（等待完成核验）" in stream.getvalue()
        return make_response(success())

    install_transport(handle)
    settings = ["y", "y", "", "n"] if debug else ["n"]
    assert (
        cli.run_cli(
            input_fn=Inputs(settings + ["", "synthetic-user", "fake-password"]),
            output_fn=lambda _: None,
            stream=stream,
        )
        == exit_code
    )
    output = stream.getvalue()
    if debug:
        assert "视频进度：" not in output
        assert "已发 " not in output
        log = (tmp_path / "labpass_log.txt").read_text(encoding="utf-8")
        assert "视频进度：" not in log and "\r" not in log
    else:
        expected = range(4) if outcome in {"success", "pending"} else range(2)
        counts = [int(value) for value in re.findall(r"已发 (\d+)/3 包", output)]
        assert counts == list(expected)
        assert not (tmp_path / "labpass_log.txt").exists()
        assert ("\r" in output) is terminal
    assert finishes == (1 if outcome in {"success", "pending"} else 0)
    assert output.endswith("\n") and not stream.closed
    assert authentication.session.closed
    if outcome == "pending":
        assert "视频完成状态未确认" in output
        assert "所有待处理课程均已完成" not in output


@pytest.mark.parametrize("duration,total", [("9.99", 1), ("10", 1), ("30.9", 3)])
def test_video_events_preserve_answer_counts_and_confirm_only_successful_packets(
    install_transport, duration, total
):
    authentication = fake_auth_result()
    events = []
    posts = []

    def handle(session, request, kwargs):
        if request.url.endswith("myCourseList"):
            assert events[-1].sent_packets == total
            return make_response(success([{"id": "course", "isFinish": 1, "watchDuration": 0}]))
        if request.method == "GET":
            return make_response(
                success(
                    [
                        {
                            "id": "answer-id",
                            "courseId": "question-course",
                            "questionId": "source-id",
                            "correctAnswer": "A",
                        }
                    ]
                )
            )
        posts.append(request.url.rsplit("/", 1)[-1])
        return make_response(success())

    install_transport(handle)
    with (
        authentication,
        SafetyLabClient(
            authentication.session,
            api_base_url=authentication.api_base_url,
            session_factory=authentication.session_factory,
            mutation_coordinator=MutationCoordinator(),
        ) as client,
    ):
        course = Course("course", "synthetic", duration_seconds=Decimal(duration))
        result = run_course(client, course, progress=events.append)
    assert not result.succeeded
    assert [event.stage for event in events] == ["started", "answered"] + ["video"] * (total + 1)
    assert [(event.sent_packets, event.total_packets) for event in events[2:]] == [
        (count, total) for count in range(total + 1)
    ]
    assert all(event.answered_count == event.question_count == 1 for event in events[2:])
    assert posts == ["submitAnswer"] + ["finishRate"] * total + ["finish"]


def test_terminal_display_redacts_and_restores_progress_around_other_logs():
    stream = TerminalStream()
    redactor = Redactor()
    redactor.remember("fake-secret")
    course = Course("active", "fake-secret\r\n\x1b中文课程")
    with PacketProgressDisplay(stream, redactor) as display:
        display.update(CourseProgress(course, "video", sent_packets=1, total_packets=3))
        display.write("其他课程已开始\n")
        assert stream.getvalue().endswith("视频进度：***  中文课程 | 已发 1/3 包")
        before = stream.getvalue()
        display.clear("other")
        assert stream.getvalue() == before
        display.clear("active")
        display.write("当前课程已失败\n")
        assert stream.getvalue().endswith("当前课程已失败\n")
    output = stream.getvalue()
    assert "fake-secret" not in output and "\x1b" not in output
    assert not stream.closed


def test_terminal_display_keeps_counts_visible_for_long_course_names(monkeypatch):
    monkeypatch.setattr(
        "labpass_cli.progress.shutil.get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    stream = TerminalStream()
    course = Course("long", "中文课程" * 40)
    with PacketProgressDisplay(stream, Redactor()) as display:
        display.update(CourseProgress(course, "video", sent_packets=29, total_packets=29))
        line = stream.getvalue().split("\r")[-1]
        assert line.endswith("已发 29/29 包（等待完成核验）")
        assert "…" in line
        assert len(line.replace("…", ".").encode("gbk", errors="replace")) < 80


def test_concurrent_log_and_progress_updates_remain_whole_lines():
    stream = io.StringIO()
    with PacketProgressDisplay(stream, Redactor()) as display:

        def update(index):
            course = Course(str(index), f"course-{index}")
            display.update(CourseProgress(course, "video", sent_packets=1, total_packets=2))
            display.write(f"完成回调-{index}\n")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(update, range(20)))
    lines = stream.getvalue().splitlines()
    assert len(lines) == 40
    assert set(lines) == {
        line
        for index in range(20)
        for line in (f"视频进度：course-{index} | 已发 1/2 包", f"完成回调-{index}")
    }
