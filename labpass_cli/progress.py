"""在普通模式显示视频发包进度，并协调日志与控制台刷新。"""

import shutil
import threading
import unicodedata
from io import TextIOBase
from typing import TextIO

from njupt_auth.redaction import Redactor
from njupt_safetylabpass import CourseProgress


def _columns(text: str) -> int:
    """计算终端列宽，避免中文进度行刷新后留下旧字符。"""
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def _fit(text: str, width: int) -> str:
    if _columns(text) <= width:
        return text
    result = ""
    for char in text:
        if _columns(result + char) > width - 1:
            break
        result += char
    return result + "…" if width > 0 else ""


class PacketProgressDisplay(TextIOBase):
    """终端原位刷新；重定向时逐行输出。所有写入共用锁，退出不关闭原始流。"""

    def __init__(self, stream: TextIO, redactor: Redactor) -> None:
        super().__init__()
        self._stream = stream
        self._redactor = redactor
        self._lock = threading.RLock()
        self._terminal = stream.isatty()
        self._course_id: str | None = None
        self._line = ""
        self._visible = False

    def _erase(self) -> None:
        if self._visible:
            self._stream.write("\r" + " " * _columns(self._line) + "\r")
            self._visible = False

    def _draw(self) -> None:
        if self._terminal and self._line:
            self._stream.write("\r" + self._line)
            self._visible = True

    def update(self, event: CourseProgress) -> None:
        """只显示视频包计数；总包数不包含答题或 finish。"""
        if event.stage != "video":
            return
        name = self._redactor.excerpt(event.course.name, limit=80)
        name = "".join(char for char in name if char.isprintable())
        suffix = f" | 已发 {event.sent_packets}/{event.total_packets} 包"
        if event.sent_packets == event.total_packets:
            suffix += "（等待完成核验）"
        prefix = "视频进度："
        if self._terminal:
            width = max(1, shutil.get_terminal_size().columns - 1)
            name = _fit(name, max(0, width - _columns(prefix + suffix)))
            line = _fit(prefix + name + suffix, width)
        else:
            line = prefix + name + suffix
        with self._lock:
            self._erase()
            self._course_id = event.course.id
            self._line = line
            if self._terminal:
                self._draw()
            else:
                self._stream.write(line + "\n")
            self._stream.flush()

    def write(self, text: str) -> int:
        """日志输出先清除进度行，再恢复当前进度，避免多线程输出混杂。"""
        with self._lock:
            self._erase()
            written = self._stream.write(text)
            if text.endswith("\n"):
                self._draw()
            return written

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def clear(self, course_id: str | None = None) -> None:
        """课程结束或运行终止时清理；其他课程的结束回调不清除当前课程。"""
        with self._lock:
            if course_id is not None and course_id != self._course_id:
                return
            self._erase()
            self._line = ""
            self._course_id = None
            self._stream.flush()

    def close(self) -> None:
        if not self.closed:
            self.clear()
            super().close()
