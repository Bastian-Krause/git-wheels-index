"""CLI wrapper: arg splitting, injected env, and exit-code passthrough."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from git_wheels_index import cli
from git_wheels_index.problems import Problems


@pytest.fixture(autouse=True)
def _clean_injected_env(monkeypatch):
    # start each test with none of the vars the wrapper would inject already set
    for key in cli._index_env(0, [])[0]:
        monkeypatch.delenv(key, raising=False)


def test_split_command_on_double_dash():
    ours, cmd = cli._split_command(["--python", "3.12", "--", "pip", "install", "."])
    assert ours == ["--python", "3.12"]
    assert cmd == ["pip", "install", "."]


def test_split_command_without_double_dash():
    ours, cmd = cli._split_command(["--version"])
    assert ours == ["--version"] and cmd == []


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert args.config is None and args.python is None and args.verbose == 0


def test_index_env_has_pip_and_uv_vars():
    env, conflicts = cli._index_env(12345, ["pip", "install", "x"])
    assert conflicts == []
    base = "http://127.0.0.1:12345/simple/"
    assert env["PIP_EXTRA_INDEX_URL"] == base
    assert env["PIP_TRUSTED_HOST"] == "127.0.0.1"
    assert env["PIP_NO_CACHE_DIR"] == "1"
    assert env["UV_EXTRA_INDEX_URL"] == base
    assert env["UV_INDEX_STRATEGY"] == "unsafe-best-match"
    assert env["UV_NO_CACHE"] == "1"


def test_resolve_python_default_is_python3_on_path():
    import shutil
    assert cli._resolve_python(None) == (shutil.which("python3") or sys.executable)


def test_resolve_python_passthrough_existing_path(tmp_path):
    fake = tmp_path / "python3.99"
    fake.write_text("")
    assert cli._resolve_python(str(fake)) == str(fake)


def test_missing_command_errors(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--python", "3.12"])
    assert exc.value.code == 2
    assert "after `--`" in capsys.readouterr().err


class _FakeServer:
    def __init__(self):
        self.shut = False

    def shutdown(self):
        self.shut = True


class _FakeProc:
    """A subprocess that is already finished with ``returncode``."""

    def __init__(self, returncode=0):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def test_run_injects_env_and_returns_exit_code(tmp_path, monkeypatch):
    server = _FakeServer()
    monkeypatch.setattr(cli, "run_in_thread", lambda app: (server, 45678))

    captured = {}

    def fake_popen(command, env=None):
        captured["command"] = command
        captured["env"] = env
        return _FakeProc(7)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    rc = cli.main(["--cache-dir", str(tmp_path / "cache"), "--", "pip", "install", "x"])

    assert rc == 7                                   # command's exit code propagates
    assert captured["command"] == ["pip", "install", "x"]
    assert captured["env"]["PIP_EXTRA_INDEX_URL"] == "http://127.0.0.1:45678/simple/"
    assert captured["env"]["UV_INDEX_STRATEGY"] == "unsafe-best-match"
    assert server.shut is True                       # index torn down afterwards


def test_run_missing_command_binary_returns_127(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_in_thread", lambda app: (_FakeServer(), 1))

    def boom(command, env=None):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--", "nope-not-a-binary"])
    assert rc == 127


@pytest.fixture
def _stub_proc(monkeypatch):
    """Stub the server + subprocess so _run reaches the conflict check with no side effects."""
    monkeypatch.setattr(cli, "run_in_thread", lambda app: (_FakeServer(), 45678))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command, env=None: _FakeProc(0))


def test_conflicting_env_errors(tmp_path, monkeypatch, capsys, _stub_proc):
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "http://mine")
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--", "pip", "install", "x"])
    assert rc == 2
    assert "PIP_EXTRA_INDEX_URL" in capsys.readouterr().err


def test_conflicting_pip_flag_errors(tmp_path, capsys, _stub_proc):
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--",
                   "pip", "install", "--no-index", "x"])
    assert rc == 2
    assert "--no-index" in capsys.readouterr().err


def test_conflicting_uv_flag_errors(tmp_path, capsys, _stub_proc):
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--",
                   "uv", "pip", "install", "--index-strategy=first-index", "x"])
    assert rc == 2
    assert "--index-strategy" in capsys.readouterr().err


def test_non_pip_command_flags_not_treated_as_conflict(tmp_path, _stub_proc):
    # --no-cache on a non-pip/uv command must not trip the CLI conflict check
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--", "docker", "build", "--no-cache", "."])
    assert rc == 0


def test_benign_env_var_not_contested(tmp_path, monkeypatch, _stub_proc):
    # PIP_TIMEOUT is injected but not contested — a pre-set value must not error
    monkeypatch.setenv("PIP_TIMEOUT", "30")
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--", "pip", "install", "x"])
    assert rc == 0


def test_recorded_problem_terminates_command_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    # A recorded problem makes the wrapper kill the command and exit non-zero, fast.
    monkeypatch.setattr(cli, "run_in_thread", lambda app: (_FakeServer(), 45678))
    seeded = Problems()
    seeded.record("foo", "boom")
    seeded.release()
    monkeypatch.setattr(cli, "Problems", lambda: seeded)

    start = time.time()
    rc = cli.main(["--cache-dir", str(tmp_path / "c"), "--", "sleep", "30"])
    assert rc == 1
    assert time.time() - start < 15          # terminated, not waited out
    assert "foo" in capsys.readouterr().err
