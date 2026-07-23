"""Exception hierarchy for git-wheels-index.

Only these types are raised for *expected* failure conditions. Inside the server a
:class:`BuildError` is caught per request and turned into a ``404`` (no git wheel
available for that project) plus a logged reason; the CLI turns a
:class:`ConfigError` into a formatted stderr message and a non-zero exit code.
"""

from __future__ import annotations


class GitWheelsIndexError(Exception):
    """Base class for all expected/handled errors."""


class ConfigError(GitWheelsIndexError):
    """The configuration file is missing, malformed, or has invalid keys."""


class RepoResolutionError(GitWheelsIndexError):
    """No source repository could be determined for a package."""


class BuildError(GitWheelsIndexError):
    """Building a wheel from a git checkout failed (clone, command, or wheel glob)."""
