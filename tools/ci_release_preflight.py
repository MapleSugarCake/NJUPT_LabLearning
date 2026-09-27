"""Validate an existing release tag before any publishing job starts."""

import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> None:
    tag = os.environ["LABPASS_RELEASE_TAG"]
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("发布工作流只能从 main 分支启动")
    if not re.fullmatch(r"v[0-9][A-Za-z0-9.+-]*", tag):
        raise ValueError("版本标签格式无效")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("仓库名称格式无效")

    commit = git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"],
        check=True,
    )
    project = tomllib.loads(git("show", f"{commit}:pyproject.toml"))["project"]
    if project["name"] != "labpass" or tag != "v" + project["version"]:
        raise ValueError("版本标签与被标记提交的 pyproject.toml 不一致")

    endpoint = f"https://api.github.com/repos/{repository}/releases/tags/{quote(tag, safe='')}"
    request = Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "LabPass-release-preflight",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=15):
            raise ValueError("同名 GitHub Release 已存在")
    except HTTPError as error:
        if error.code != 404:
            raise
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"commit={commit}\n")
    print(f"发布标签 {tag} 已验证：{commit}")


if __name__ == "__main__":
    main()
