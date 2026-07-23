"""Small helpers for the index: the version-announcement transform and the
``pip wheel`` git-spec constructor.
"""

from __future__ import annotations

from packaging.version import InvalidVersion, Version


# --------------------------------------------------------------------------- #
# version announcement
# --------------------------------------------------------------------------- #
def announced_version(raw_version: str, sha: str) -> str:
    """The version the index advertises for a git build: ``<base>+g<shortsha>``.

    The raw version (from setuptools-scm, a hardcoded ``version``, or the build
    backend) is stripped to its public *base* — dropping any ``.devN`` / ``rcN`` /
    ``.postN`` / existing local segment — and the commit is appended as a PEP 440
    **local** segment. The result is a final release: it sorts above the same base
    release (so it wins when a dependency is uncapped) while its truthful base is
    still rejected by a real upper bound (``<9`` excludes ``9.0.0+g…``).
    """
    short = sha[:7]
    try:
        base = Version(raw_version).base_version
    except InvalidVersion:
        base = raw_version.strip() or "0"
    return f"{base}+g{short}"


# --------------------------------------------------------------------------- #
# git wheel-spec construction (for `pip wheel`)
# --------------------------------------------------------------------------- #
def git_wheel_spec(repo_url: str, ref: str | None, sha: str | None) -> str:
    """A ``pip wheel`` spec for a git source: ``git+<base>@<sha>[#subdirectory=…]``.

    ``repo_url`` may carry a ``#subdirectory=…`` fragment (kept) and other
    fragments such as ``egg=`` (dropped). Pins to ``sha`` when known, else to
    ``ref``/the default branch.
    """
    base, _, frag = repo_url.partition("#")
    sub = next((p for p in frag.split("&") if p.startswith("subdirectory=")), "")
    subfrag = f"#{sub}" if sub else ""
    pin = f"@{sha}" if sha else (f"@{ref}" if ref else "")
    return f"git+{base}{pin}{subfrag}"
