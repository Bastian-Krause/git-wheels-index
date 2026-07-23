"""Config loading/validation for the repurposed (index-model) schema."""

from __future__ import annotations

import pytest

from git_wheels_index.config import find_default_config_path, load_config
from git_wheels_index.exceptions import ConfigError


def _write(tmp_path, text):
    p = tmp_path / "cfg.toml"
    p.write_text(text)
    return p


def test_empty_config_when_no_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(None)
    assert cfg.repo_overrides == {} and cfg.sources == {}


def test_repo_overrides_canonicalized(tmp_path):
    p = _write(tmp_path, '[repo_overrides]\nPaho_MQTT = "https://x/y"\n')
    cfg = load_config(p)
    assert cfg.repo_overrides["paho-mqtt"] == "https://x/y"


def test_git_and_pypi_sources(tmp_path):
    p = _write(
        tmp_path,
        '[sources.foo]\ntype = "git"\nurl = "https://x/foo"\nref = "v2"\n'
        '[sources.bar]\ntype = "pypi"\n',
    )
    cfg = load_config(p)
    assert cfg.source_for("foo").type == "git"
    assert cfg.source_for("foo").ref == "v2"
    assert cfg.source_for("bar").type == "pypi"


def test_prebuild_requires_repo_and_wheel(tmp_path):
    p = _write(tmp_path, '[sources.p]\ntype = "prebuild"\nrepo = "https://x/p"\n')
    with pytest.raises(ConfigError, match="wheel"):
        load_config(p)


def test_prebuild_full(tmp_path):
    p = _write(
        tmp_path,
        '[sources.p]\ntype = "prebuild"\nrepo = "https://x/p"\n'
        'commands = ["make"]\nwheel = "dist/*.whl"\n',
    )
    d = load_config(p).source_for("p")
    assert d.type == "prebuild" and d.commands == ("make",) and d.wheel == "dist/*.whl"


def test_prepare_section_is_now_unknown(tmp_path):
    # [prepare] was removed in the index model — it must be rejected as unknown.
    p = _write(tmp_path, '[prepare]\ndir = "./x"\n')
    with pytest.raises(ConfigError, match="unknown top-level"):
        load_config(p)


def test_unknown_source_key_rejected(tmp_path):
    p = _write(tmp_path, '[sources.foo]\ntype = "git"\nbogus = 1\n')
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(p)


def test_pypi_rejects_extra_keys(tmp_path):
    p = _write(tmp_path, '[sources.foo]\ntype = "pypi"\nversion = "1.0"\n')
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(p)


def test_find_default_config_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "git-wheels-index.toml").write_text("")
    assert find_default_config_path() == __import__("pathlib").Path("git-wheels-index.toml")
