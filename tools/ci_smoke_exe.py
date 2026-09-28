"""Exercise the Windows executable without credentials or school network requests."""

import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "labpass.exe"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_exit_two(result: subprocess.CompletedProcess[str], description: str) -> None:
    require(
        result.returncode == 2,
        f"{description}：实际退出码 {result.returncode}；"
        f"stdout={result.stdout[-1000:]!r}；stderr={result.stderr[-1000:]!r}",
    )


def run(
    executable: Path, working_directory: Path, answers: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    return subprocess.run(
        [str(executable)],
        input=answers,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        cwd=working_directory,
        env=environment,
        check=False,
    )


def main() -> None:
    require(EXE.is_file(), "尚未构建 dist/labpass.exe")
    with (ROOT / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    archive_names = {name.replace("\\", "/") for name in CArchiveReader(str(EXE)).toc}
    require(f"labpass-{version}.dist-info/METADATA" in archive_names, "EXE 缺少发行元数据")
    require("playwright/driver/node.exe" in archive_names, "EXE 缺少 Playwright 驱动")
    with tempfile.TemporaryDirectory(prefix="labpass-exe-") as directory:
        temporary = Path(directory)
        executable_directory = temporary / "exe"
        working_directory = temporary / "cwd"
        executable_directory.mkdir()
        working_directory.mkdir()
        executable = executable_directory / "labpass.exe"
        shutil.copy2(EXE, executable)

        initial = run(executable, working_directory, "")
        lines = initial.stdout.splitlines()
        require_exit_two(initial, "EXE 在首次输入 EOF 时未返回 2")
        require(bool(lines) and lines[0] == "*" * 64, "EXE 未先显示固定横幅")
        require(f"LabPass {version}" in lines, "EXE 未显示发行版本")
        require("是否自定义设置" in initial.stdout, "EXE 的中文设置提示未正确编码")
        require("[y/N]" in initial.stdout, "EXE 缺少首个设置提示")
        require(initial.stdout.split("[y/N]", 1)[1].strip(), "EXE 退出前未显示暂停提示")
        require("请输入学号" not in initial.stdout, "EXE 验收不应进入凭据输入")
        require(not (executable_directory / "labpass_log.txt").exists(), "默认模式创建了日志")

        debug_answers = "y\ny\n\n\n"
        debug = run(executable, working_directory, debug_answers)
        log_file = executable_directory / "labpass_log.txt"
        require_exit_two(debug, "debug 模式在网络选择 EOF 时未返回 2")
        require(log_file.is_file(), "EXE 未在自身目录创建日志")
        require(not (working_directory / "labpass_log.txt").exists(), "EXE 将日志写进工作目录")
        original = log_file.read_bytes()
        conflict = run(executable, working_directory, debug_answers)
        require_exit_two(conflict, "已有日志文件时 EXE 未返回 2")
        require(conflict.stdout.split("[y/N]", 1)[1].strip(), "已有日志文件时未报告冲突")
        require(log_file.read_bytes() == original, "已有日志被覆盖或追加")
        require("请输入学号" not in debug.stdout + conflict.stdout, "EXE 验收进入了凭据输入")
    print("Windows EXE banner, version, EOF, pause, and logging checks passed")


if __name__ == "__main__":
    main()
