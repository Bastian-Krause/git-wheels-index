"""Configuration loading and validation.

The config surface is deliberately small:

* ``[repo_overrides]`` — canonical name -> git URL corrections (so the index knows
  which repo to build a package's git wheel from).
* ``[sources.<name>]`` — per-package source directive:

  - ``git`` (``url``, ``ref``, optional pre-build ``commands`` / ``subdirectory``) —
    build this package's git wheel from ``ref``/``url`` instead of the resolved
    repo's default branch, optionally running ``commands`` in the checkout first.
  - ``pypi`` — **do not** serve a git wheel for this package; the index returns
    ``404`` so pip/uv only ever see PyPI's released wheels. (An escape hatch for
    packages you don't want built from source.)
  - ``prebuild`` (``repo``, ``ref``, ``subdirectory``, ``commands``, ``wheel``) — a
    recipe whose ``commands`` build the git wheel for an exotic package
    (bazel/protoc/Cython/…) and ``wheel`` globs the result.

The server caches built wheels under ``./.git-wheels-index/`` in its working directory,
keyed by commit SHA (and wheel tag). Recipe checkouts live under the same dir but are a
per-run build workspace — cloned fresh each run and removed at teardown.

Unknown keys raise :class:`ConfigError` so typos fail loudly.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from .exceptions import ConfigError

# The server's on-disk cache root (in its cwd). Not configurable via TOML; the CLI
# exposes it as --cache-dir for the rare case it must move.
DEFAULT_CACHE_DIR = "./.git-wheels-index"

_SOURCE_TYPES = ("git", "pypi", "prebuild")
_SOURCE_KEYS = {
    "git": {"type", "url", "ref", "commands", "subdirectory"},
    "pypi": {"type"},
    "prebuild": {"type", "repo", "ref", "subdirectory", "commands", "wheel"},
}
_TOP_LEVEL_KEYS = {"repo_overrides", "sources"}


@dataclass(frozen=True)
class SourceDirective:
    """A per-package instruction for how the index should treat a package."""

    type: str  # "git" | "pypi" | "prebuild"
    # git / prebuild
    url: str | None = None            # git: explicit repo URL (else resolve)
    repo: str | None = None           # prebuild: repo URL (required)
    ref: str | None = None            # git / prebuild: branch/tag/sha (default branch if None)
    subdirectory: str | None = None   # prebuild: path within checkout the commands run in
    commands: tuple[str, ...] = ()    # prebuild: build commands, in order (must produce a wheel)
    wheel: str | None = None          # prebuild: glob (relative to checkout) locating the wheel


@dataclass(frozen=True)
class Config:
    repo_overrides: dict[str, str] = field(default_factory=dict)
    sources: dict[str, SourceDirective] = field(default_factory=dict)
    source_path: Path | None = None

    def source_for(self, name: str) -> SourceDirective | None:
        return self.sources.get(canonicalize_name(name))


def find_default_config_path() -> Path | None:
    """Return the first existing default config path, or None."""
    local = Path("git-wheels-index.toml")
    if local.is_file():
        return local
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    user = base / "git-wheels-index" / "config.toml"
    if user.is_file():
        return user
    return None


def _require(condition: bool, message: str, path: Path | None) -> None:
    if not condition:
        where = f" in {path}" if path else ""
        raise ConfigError(message + where)


def _parse_commands(name: str, raw: Any, path: Path | None) -> tuple[str, ...]:
    commands = raw.get("commands", [])
    _require(
        isinstance(commands, list) and all(isinstance(c, str) for c in commands),
        f"[sources.{name}] 'commands' must be a list of strings",
        path,
    )
    return tuple(commands)


def _parse_source(name: str, raw: Any, path: Path | None) -> SourceDirective:
    _require(isinstance(raw, dict), f"[sources.{name}] must be a table", path)
    stype = raw.get("type")
    _require(
        stype in _SOURCE_TYPES,
        f"[sources.{name}] needs type = one of {_SOURCE_TYPES}, got {stype!r}",
        path,
    )
    unknown = set(raw) - _SOURCE_KEYS[stype]
    _require(
        not unknown,
        f"[sources.{name}] (type={stype}) has unknown key(s): {sorted(unknown)}",
        path,
    )

    if stype == "prebuild":
        _require(
            isinstance(raw.get("repo"), str) and raw["repo"],
            f"[sources.{name}] prebuild needs a 'repo' URL",
            path,
        )
        _require(
            isinstance(raw.get("wheel"), str) and raw["wheel"],
            f"[sources.{name}] prebuild needs a 'wheel' glob "
            f"(the commands must produce a wheel; e.g. wheel = \"dist/*.whl\")",
            path,
        )
        return SourceDirective(
            type="prebuild",
            repo=raw["repo"],
            ref=raw.get("ref"),
            subdirectory=raw.get("subdirectory"),
            commands=_parse_commands(name, raw, path),
            wheel=raw["wheel"],
        )
    if stype == "git":
        # optional pre-build hook: commands run in the checkout before the tool
        # builds the wheel (codegen / cythonize / apt / …).
        return SourceDirective(
            type="git",
            url=raw.get("url"),
            ref=raw.get("ref"),
            subdirectory=raw.get("subdirectory"),
            commands=_parse_commands(name, raw, path),
        )
    # pypi — no keys beyond type; means "never serve a git wheel for this package"
    return SourceDirective(type="pypi")


def load_config(path: Path | None) -> Config:
    """Load and validate a config file.

    If ``path`` is None, the default search path is used; if nothing is found,
    an empty (default) Config is returned.
    """
    if path is None:
        path = find_default_config_path()
    if path is None:
        return Config()

    _require(path.is_file(), f"config file not found: {path}", None)
    try:
        with path.open("rb") as fp:
            data = tomllib.load(fp)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"could not parse config {path}: {exc}") from exc

    unknown = set(data) - _TOP_LEVEL_KEYS
    _require(not unknown, f"unknown top-level key(s): {sorted(unknown)}", path)

    repo_overrides_raw = data.get("repo_overrides", {})
    _require(isinstance(repo_overrides_raw, dict), "[repo_overrides] must be a table", path)
    repo_overrides = {canonicalize_name(k): v for k, v in repo_overrides_raw.items()}

    sources_raw = data.get("sources", {})
    _require(isinstance(sources_raw, dict), "[sources] must be a table", path)
    sources = {
        canonicalize_name(name): _parse_source(name, raw, path)
        for name, raw in sources_raw.items()
    }

    return Config(
        repo_overrides=repo_overrides,
        sources=sources,
        source_path=path,
    )
