"""Repo resolution + ls-remote sha helper."""

from __future__ import annotations

import subprocess

import pytest

from git_wheels_index import repo_resolution
from git_wheels_index.config import Config
from git_wheels_index.exceptions import RepoResolutionError
from git_wheels_index.repo_resolution import (
    extract_repo_url,
    resolve_repo_url,
    resolve_sha,
)


def test_extract_repo_url_dotgit_returns_str():
    url = extract_repo_url("x", {"Source": "https://gitlab.com/a/b.git"})
    assert isinstance(url, str) and url.endswith("b.git")


def test_extract_repo_url_github_owner_repo():
    url = extract_repo_url("x", {"Homepage": "https://github.com/org/repo"})
    assert url == "https://github.com/org/repo"


def test_extract_repo_url_skips_sponsors_and_subpages():
    with pytest.raises(RepoResolutionError):
        extract_repo_url("x", {"A": "https://github.com/sponsors/foo",
                               "B": "https://github.com/org/repo/issues"})


def test_resolver_override_wins(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an override must not trigger a PyPI lookup")

    monkeypatch.setattr(repo_resolution, "get_project_urls_from_pypi", boom)
    cfg = Config(repo_overrides={"foo": "https://github.com/o/foo"})
    assert resolve_repo_url(cfg, "foo") == "https://github.com/o/foo"


def test_resolver_final_error_is_actionable(monkeypatch):
    monkeypatch.setattr(repo_resolution, "get_project_urls_from_pypi", lambda *a, **k: {})
    with pytest.raises(RepoResolutionError, match=r"\[repo_overrides\]"):
        resolve_repo_url(Config(), "mystery-pkg")


def test_resolver_pypi_json_heuristic(monkeypatch):
    monkeypatch.setattr(
        repo_resolution, "get_project_urls_from_pypi",
        lambda *a, **k: {"Source": "https://github.com/o/foo"},
    )
    assert resolve_repo_url(Config(), "foo") == "https://github.com/o/foo"


def test_resolve_sha_returns_first_column(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[:2] == ["git", "ls-remote"]
        return subprocess.CompletedProcess(argv, 0, "deadbeef" * 5 + "\tHEAD\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_sha("https://x/y") == "deadbeef" * 5


def test_resolve_sha_passes_through_full_sha():
    full = "a" * 40
    assert resolve_sha("https://x/y", full) == full


def test_resolve_sha_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no"),
    )
    assert resolve_sha("https://x/y") is None


def test_resolve_sha_strips_fragment(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["url"] = argv[2]
        return subprocess.CompletedProcess(argv, 0, "c" * 40 + "\tHEAD\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve_sha("https://x/y#subdirectory=z")
    assert seen["url"] == "https://x/y"
