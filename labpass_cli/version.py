"""Read the single project version, including generated distribution metadata."""

import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def get_version() -> str:
    """Prefer source metadata; installed/frozen distributions use generated metadata."""
    source = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not getattr(sys, "frozen", False) and source.is_file():
        with source.open("rb") as file:
            project = tomllib.load(file)["project"]
        if project.get("name") != "labpass" or not isinstance(project.get("version"), str):
            raise ValueError("项目版本元数据无效")
        return project["version"]
    try:
        return version("labpass")
    except PackageNotFoundError:
        raise ValueError("缺少 LabPass 版本元数据，请重新安装或构建") from None
