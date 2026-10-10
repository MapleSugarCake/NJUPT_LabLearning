"""离线验证发布版本取自标签提交，避免使用 main 上后续更新的版本。"""

from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from tools import ci_release_preflight


@pytest.mark.parametrize("version", ["2.0.0", "2.4.1"])
def test_release_version_comes_from_tagged_project(monkeypatch, tmp_path, version):
    commit = "a" * 40
    tag = f"v{version}"
    output = tmp_path / "github-output.txt"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "labpass"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    for name, value in {
        "LABPASS_RELEASE_TAG": tag,
        "GITHUB_REPOSITORY": "synthetic-owner/synthetic-repository",
        "GH_TOKEN": "synthetic-github-token",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(name, value)

    def git(*arguments):
        if arguments == ("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"):
            return commit
        if arguments == ("show", f"{commit}:pyproject.toml"):
            return f'[project]\nname = "labpass"\nversion = "{version}"\n'
        raise AssertionError(f"Unexpected git arguments: {arguments}")

    monkeypatch.setattr(ci_release_preflight, "git", git)
    monkeypatch.setattr(ci_release_preflight.subprocess, "run", Mock())
    monkeypatch.setattr(
        ci_release_preflight,
        "urlopen",
        Mock(side_effect=HTTPError("https://api.github.com/synthetic", 404, "Not Found", {}, None)),
    )

    ci_release_preflight.main()

    assert output.read_text(encoding="utf-8").splitlines() == [
        f"commit={commit}",
        f"version={version}",
    ]
