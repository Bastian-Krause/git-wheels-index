"""Resolve a package name to its source git repository.

Resolution chain (first hit wins):

1. an explicit ``[repo_overrides]`` entry (corrections always win);
2. a heuristic over the package's ``Project-URL`` metadata from the PyPI JSON API;
3. otherwise a clear, actionable :class:`RepoResolutionError`.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from packaging.utils import canonicalize_name

from .config import Config
from .exceptions import RepoResolutionError

logger = logging.getLogger("git_wheels_index")

_HTTP_TIMEOUT = 10.0
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def resolve_sha(url: str, ref: str | None = None) -> str | None:
    """Resolve a git ref (default ``HEAD``) to a full commit SHA via ``git ls-remote``.

    Returns ``None`` on any failure (network, unknown ref). A ref that is already a
    full 40-hex SHA is returned as-is (``ls-remote`` would not list it).
    """
    if ref and _SHA_RE.match(ref):
        return ref
    target = ref or "HEAD"
    # strip any URL fragment (#subdirectory=…) — ls-remote wants the bare repo
    bare = url.partition("#")[0]
    try:
        proc = subprocess.run(
            ["git", "ls-remote", bare, target],
            capture_output=True, text=True, timeout=_HTTP_TIMEOUT * 3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    first = proc.stdout.splitlines()[0].split()
    return first[0] if first and _SHA_RE.match(first[0]) else None


def extract_repo_url(pkg_name: str, project_urls: dict[str, str]) -> str:
    """Pick a source-repo URL out of a package's project_urls."""
    for raw in project_urls.values():
        url = urllib.parse.urlparse(raw)
        path = url.path.rstrip("/")

        if path.endswith(".git"):
            return url.geturl()
        if url.netloc == "github.com":
            if path.startswith("/sponsors"):
                continue
            # skip /pulls, /actions, /discussions, etc. — only owner/repo pages
            if path.count("/") > 2:
                continue
            return url.geturl()

    raise RepoResolutionError(
        f"no recognizable repository URL among {pkg_name}'s project URLs: {project_urls}"
    )


@functools.cache  # one PyPI hit per package per run (the /simple + /build routes both resolve)
def get_project_urls_from_pypi(pkg_name: str) -> dict[str, str]:
    """Fetch project_urls from the PyPI JSON API."""
    url = f"https://pypi.org/pypi/{urllib.parse.quote(pkg_name)}/json"
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        raise RepoResolutionError(f"PyPI lookup failed for {pkg_name}: {exc}") from exc
    return data.get("info", {}).get("project_urls") or {}


def resolve_repo_url(config: Config, pkg_name: str) -> str:
    """Resolve a package name to its git repository URL (override, then heuristic).

    Raises :class:`RepoResolutionError` either way — the PyPI lookup failing (X1)
    and no repo being found in the metadata (X2) both abort the run; the message
    tells them apart.
    """
    canonical = canonicalize_name(pkg_name)

    override = config.repo_overrides.get(canonical)
    if override:
        logger.debug("repo for %s from override: %s", pkg_name, override)
        return override

    # X1: a PyPI lookup failure (network / timeout / 5xx / not on PyPI) propagates.
    pypi_urls = get_project_urls_from_pypi(pkg_name)
    if pypi_urls:
        try:
            url = extract_repo_url(pkg_name, pypi_urls)
            logger.debug("repo for %s from PyPI project urls: %s", pkg_name, url)
            return url
        except RepoResolutionError:
            pass  # metadata had URLs but none looked like a repo -> X2 below

    # X2: no git repo could be determined.
    where = config.source_path or "the config file"
    raise RepoResolutionError(
        f"no git repository found for {pkg_name!r}. "
        f'Add [repo_overrides] {canonical} = "https://github.com/org/repo" '
        f'or [sources.{canonical}] type = "pypi" in {where}.'
    )
