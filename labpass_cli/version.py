"""从唯一项目版本源或生成的发行元数据读取版本。

本文件定义：
    get_version：根据源码或安装形态读取 LabPass 版本。
"""

import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


# 作用：根据源码或安装形态读取 LabPass 版本。
# 参数：无。
# 返回：项目版本字符串。
# 说明：源码环境优先读取根目录项目元数据并检查项目名与版本类型；冻结程序或已安装包读取发行元数据。
# 说明：元数据无效或缺失时抛出 ValueError，文件或解析错误向上传播；不使用硬编码回退版本。
def get_version() -> str:
    """根据源码或安装形态读取 LabPass 版本。"""
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
