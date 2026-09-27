"""Inspect built distributions and install each in a clean environment."""

import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PACKAGES = {"njupt_auth", "njupt_safetylabpass", "labpass_cli"}
SDIST_TOP_LEVEL = PACKAGES | {
    "tests",
    "main.py",
    "main.spec",
    "favicon.ico",
    "README.md",
    "AGENTS.md",
    ".gitignore",
    "pyproject.toml",
    "uv.lock",
    "PKG-INFO",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def inspect_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    for package in PACKAGES:
        require(f"{package}/__init__.py" in names, f"wheel 缺少 {package}")
    top_level = {name.split("/", 1)[0] for name in names}
    require(
        top_level == PACKAGES | {f"labpass-{version}.dist-info"},
        f"wheel 包含多余或缺失的顶层内容：{top_level}",
    )
    require(
        f"labpass-{version}.dist-info/METADATA" in names,
        "wheel 缺少发行元数据",
    )
    require(not any(name.startswith("labpass/") for name in names), "wheel 包含旧 labpass 包")


def inspect_sdist(path: Path, version: str) -> None:
    expected_root = f"labpass-{version}"
    with tarfile.open(path, "r:gz") as archive:
        names = set()
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            require(bool(parts) and parts[0] == expected_root, "sdist 根目录不符合版本")
            if len(parts) > 1:
                names.add("/".join(parts[1:]))
    top_level = {name.split("/", 1)[0] for name in names}
    require(
        top_level <= SDIST_TOP_LEVEL, f"sdist 包含额外目录或文件：{top_level - SDIST_TOP_LEVEL}"
    )
    for package in PACKAGES:
        require(f"{package}/__init__.py" in names, f"sdist 缺少 {package}")
    require("pyproject.toml" in names, "sdist 缺少构建配置")
    require(
        not any(name.lower().endswith((".har", ".log")) for name in names), "sdist 包含日志或 HAR"
    )


def install_and_check(path: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="labpass-package-") as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        child_env = os.environ.copy()
        child_env["PIP_NO_CACHE_DIR"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--quiet",
                str(path),
            ],
            check=True,
            cwd=directory,
            env=child_env,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                "import njupt_auth, njupt_safetylabpass, labpass_cli; "
                "from importlib.metadata import version; "
                f"assert version('labpass') == {version!r}",
            ],
            check=True,
            cwd=directory,
            env=child_env,
        )
        command = environment / (
            "Scripts/labpass.exe" if sys.platform == "win32" else "bin/labpass"
        )
        result = subprocess.run(
            [str(command)],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            cwd=directory,
            env=child_env,
            check=False,
        )
        require(result.returncode == 2, "安装后的入口在 EOF 时未返回 2")
        require(result.stdout.splitlines()[0] == f"LabPass {version}", "安装后的入口版本不是首行")
        require("是否自定义设置" in result.stdout, "安装后的入口缺少首个提示")
        require("请输入学号" not in result.stdout, "安装验收不应进入凭据输入")


def main() -> None:
    with (ROOT / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    wheels = list(DIST.glob("labpass-*.whl"))
    sdists = list(DIST.glob("labpass-*.tar.gz"))
    require(len(wheels) == len(sdists) == 1, "必须恰有一个 wheel 和一个 sdist")
    inspect_wheel(wheels[0], version)
    inspect_sdist(sdists[0], version)
    for artifact in (wheels[0], sdists[0]):
        install_and_check(artifact, version)
    print("wheel 和 sdist 内容、独立安装及三包导入已验证")


if __name__ == "__main__":
    main()
