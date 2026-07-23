"""Version announcement + git-spec construction."""

from __future__ import annotations

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from git_wheels_index.metadata import announced_version, git_wheel_spec

_SHA = "abc1234def5678000000000000000000000000ff"


@pytest.mark.parametrize(
    "raw, expected_base",
    [
        ("9.0.0.dev47+gdeadbee", "9.0.0"),   # setuptools-scm dev
        ("2.1.0rc1", "2.1.0"),               # prerelease
        ("1.4.2.post3", "1.4.2"),            # post release
        ("3.0", "3.0"),                      # hardcoded release
        ("1.2.3+local", "1.2.3"),            # already has a local segment
    ],
)
def test_announced_version_strips_to_base_plus_commit(raw, expected_base):
    out = announced_version(raw, _SHA)
    assert out == f"{expected_base}+gabc1234"
    # it must be a valid, *final* (non-prerelease) version
    v = Version(out)
    assert not v.is_prerelease and not v.is_devrelease
    assert v.local == "gabc1234"


def test_announced_version_sorts_above_release_but_under_cap():
    v = Version(announced_version("9.0.0.dev1+gx", _SHA))
    assert v > Version("9.0.0")                          # wins when uncapped
    assert not SpecifierSet("<9").contains(v)            # excluded by a real cap
    assert SpecifierSet("<10").contains(v)               # allowed under a looser cap


def test_announced_version_invalid_raw_falls_back():
    assert announced_version("not-a-version", _SHA) == "not-a-version+gabc1234"


def test_git_wheel_spec_pins_sha_and_keeps_subdirectory():
    spec = git_wheel_spec("https://x/y#subdirectory=py&egg=y", "main", _SHA)
    assert spec == f"git+https://x/y@{_SHA}#subdirectory=py"


def test_git_wheel_spec_ref_when_no_sha():
    assert git_wheel_spec("https://x/y", "v2", None) == "git+https://x/y@v2"


def test_git_wheel_spec_bare_when_nothing():
    assert git_wheel_spec("https://x/y", None, None) == "git+https://x/y"
