"""Builder: wheel re-versioning, resolution, cache reuse, and build dispatch."""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from packaging.utils import parse_wheel_filename

from git_wheels_index import builder as builder_mod
from git_wheels_index.builder import (
    BuildManager,
    Builder,
    HeadResolution,
    reversion_wheel,
)
from git_wheels_index.config import Config, SourceDirective
from git_wheels_index.exceptions import GitWheelsIndexError, RepoResolutionError


# --------------------------------------------------------------------------- #
# reversion_wheel
# --------------------------------------------------------------------------- #
def test_reversion_rewrites_filename_metadata_and_record(wheel_factory, tmp_path):
    src = wheel_factory(name="foo", version="1.2.3.dev4+gold")
    dest = tmp_path / "out"
    new = reversion_wheel(src, "foo", "1.2.3+gnew123", "py3-none-any", dest)

    assert new.name == "foo-1.2.3+gnew123-py3-none-any.whl"
    name, version, _, _ = parse_wheel_filename(new.name)
    assert str(version) == "1.2.3+gnew123"

    with zipfile.ZipFile(new) as z:
        names = z.namelist()
        assert "foo-1.2.3+gnew123.dist-info/METADATA" in names
        assert not any("1.2.3.dev4+gold" in n for n in names)   # old dist-info gone
        assert "foo/__init__.py" in names                       # payload preserved
        meta = z.read("foo-1.2.3+gnew123.dist-info/METADATA").decode()
        assert "Version: 1.2.3+gnew123" in meta
        assert "Version: 1.2.3.dev4+gold" not in meta
        record = z.read("foo-1.2.3+gnew123.dist-info/RECORD").decode()
        assert "foo-1.2.3+gnew123.dist-info/METADATA,sha256=" in record
        assert "foo-1.2.3+gnew123.dist-info/RECORD,," in record


def test_reversion_forces_configured_name_when_backend_differs(wheel_factory, tmp_path, caplog):
    # A repo override may point at a repo whose distribution was renamed upstream
    # (roman-numerals-py -> roman-numerals). The config wins: filename AND dist-info
    # must both use the configured name, or pip rejects the wheel as invalid.
    src = wheel_factory(name="roman_numerals", version="4.1.0")
    with caplog.at_level("INFO", logger="git_wheels_index"):
        new = reversion_wheel(src, "roman_numerals_py", "4.1.0+gadeca8e",
                              "py3-none-any", tmp_path / "out")

    assert new.name == "roman_numerals_py-4.1.0+gadeca8e-py3-none-any.whl"
    di = "roman_numerals_py-4.1.0+gadeca8e.dist-info"
    with zipfile.ZipFile(new) as z:
        names = z.namelist()
        assert f"{di}/METADATA" in names
        assert not any(n.startswith("roman_numerals-") for n in names)  # old name gone
        meta = z.read(f"{di}/METADATA").decode()
        assert "Name: roman_numerals_py" in meta and "Name: roman_numerals\n" not in meta
        assert "Version: 4.1.0+gadeca8e" in meta
        record = z.read(f"{di}/RECORD").decode()
        assert f"{di}/METADATA,sha256=" in record          # RECORD follows the rename
    assert "serving roman_numerals as roman_numerals_py" in caplog.text


def test_reversion_does_not_log_when_name_already_matches(wheel_factory, tmp_path, caplog):
    src = wheel_factory(name="foo", version="1.0")
    with caplog.at_level("INFO", logger="git_wheels_index"):
        reversion_wheel(src, "foo", "1.0+gabc", "py3-none-any", tmp_path / "o")
    assert "serving" not in caplog.text


def test_reversion_forces_tag(wheel_factory, tmp_path):
    # a pure wheel is force-re-tagged to the announced (platform) tag
    src = wheel_factory(name="ext", version="2.0.0", tag="py3-none-any")
    new = reversion_wheel(src, "ext", "2.0.0+gabc", "cp312-cp312-linux_x86_64", tmp_path / "o")
    assert new.name == "ext-2.0.0+gabc-cp312-cp312-linux_x86_64.whl"
    with zipfile.ZipFile(new) as z:
        wheel_meta = z.read("ext-2.0.0+gabc.dist-info/WHEEL").decode()
        assert "Tag: cp312-cp312-linux_x86_64" in wheel_meta
        assert "py3-none-any" not in wheel_meta


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _builder(config, tmp_path, **kw):
    return Builder(config, tmp_path / "cache", **kw)


_GIVEN_SHA = "e" * 40  # a given sha keeps resolve() network-free


def test_resolve_pypi_is_none(tmp_path):
    cfg = Config(sources={"foo": SourceDirective(type="pypi")})
    assert _builder(cfg, tmp_path).resolve("foo", _GIVEN_SHA) is None


def test_resolve_prebuild(tmp_path):
    d = SourceDirective(type="prebuild", repo="https://x/p", wheel="dist/*.whl")
    cfg = Config(sources={"p": d})
    res = _builder(cfg, tmp_path).resolve("p", _GIVEN_SHA)
    assert res.kind == "prebuild" and res.repo_url == "https://x/p" and res.directive is d


def test_resolve_git_override(tmp_path):
    cfg = Config(repo_overrides={"foo": "https://github.com/o/foo"})
    res = _builder(cfg, tmp_path).resolve("foo", _GIVEN_SHA)
    assert res.kind == "git" and res.repo_url == "https://github.com/o/foo"


def test_resolve_unresolvable_raises(tmp_path, monkeypatch):
    # no override and no PyPI repo -> RepoResolutionError (X2, aborts; never PyPI)
    import git_wheels_index.repo_resolution as rr
    monkeypatch.setattr(rr, "get_project_urls_from_pypi", lambda *a, **k: {})
    with pytest.raises(RepoResolutionError):
        _builder(Config(), tmp_path).resolve("mystery", _GIVEN_SHA)


def test_resolve_looks_up_sha_when_not_given(tmp_path, monkeypatch):
    monkeypatch.setattr(builder_mod, "resolve_sha", lambda url, ref=None: "d" * 40)
    cfg = Config(repo_overrides={"foo": "https://github.com/o/foo"})
    res = _builder(cfg, tmp_path).resolve("foo")
    assert res.kind == "git" and res.sha == "d" * 40 and res.project == "foo"


def test_resolve_uses_given_sha_without_lookup(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("resolve_sha must not be called when a sha is given")

    monkeypatch.setattr(builder_mod, "resolve_sha", boom)
    cfg = Config(repo_overrides={"foo": "https://github.com/o/foo"})
    res = _builder(cfg, tmp_path).resolve("foo", _GIVEN_SHA)
    assert res.sha == _GIVEN_SHA


# --------------------------------------------------------------------------- #
# build_for_sha: cache hit (the observable side of request-independence)
# --------------------------------------------------------------------------- #
def _seed_cache(b, wheel_factory, project="foo", version="1.0+gaaaaaaa", sha="a" * 40,
                name=None):
    """Put a real (valid) wheel into the SHA cache and return its path."""
    cache = b._wheel_cache / project / sha / b.wheel_tag
    cache.mkdir(parents=True, exist_ok=True)
    src = wheel_factory(name=name or project, version=version, tag=b.wheel_tag)
    dest = cache / f"{project}-{version}-{b.wheel_tag}.whl"
    src.replace(dest)
    return dest


def test_build_for_sha_returns_cached_wheel_without_building(tmp_path, wheel_factory):
    cfg = Config()
    b = _builder(cfg, tmp_path)
    b._tag = "cp311-cp311-linux_x86_64"          # avoid the real interpreter probe
    sha = "a" * 40
    _seed_cache(b, wheel_factory, sha=sha)       # keyed by tag now

    def boom(*a, **k):
        raise AssertionError("must not rebuild when a cached wheel exists")

    b._produce_natural_wheel = boom  # type: ignore[assignment]
    res = HeadResolution("foo", "git", "https://x/foo", None, sha, None)
    wheel = b.build_for_sha(res, sha)
    assert wheel.name == f"foo-1.0+gaaaaaaa-{b.wheel_tag}.whl"


def test_cached_wheel_returns_a_valid_entry(tmp_path, wheel_factory):
    b = _builder(Config(), tmp_path)
    b._tag = "cp311-cp311-linux_x86_64"
    dest = _seed_cache(b, wheel_factory)
    assert b.cached_wheel("foo", "a" * 40) == dest


def test_cached_wheel_ignores_wheel_built_under_a_different_name(tmp_path, wheel_factory, caplog):
    # The cache key can't capture how we stamp a wheel, so an entry from an older tool
    # version may carry the build backend's name (roman-numerals vs roman-numerals-py).
    # Serving it would trip the server's metadata self-check and abort the run.
    b = _builder(Config(), tmp_path)
    b._tag = "cp311-cp311-linux_x86_64"
    stale = _seed_cache(b, wheel_factory, project="foo", name="something_else")
    with caplog.at_level("INFO", logger="git_wheels_index"):
        assert b.cached_wheel("foo", "a" * 40) is None      # treated as a miss
    assert "stale cached wheel" in caplog.text
    assert not stale.exists()      # evicted, so later lookups don't re-read it
    assert b.cached_wheel("foo", "a" * 40) is None          # still a miss, silently


def test_cached_wheel_ignores_version_disagreeing_with_filename(tmp_path, wheel_factory):
    b = _builder(Config(), tmp_path)
    b._tag = "cp311-cp311-linux_x86_64"
    cache = b._wheel_cache / "foo" / ("a" * 40) / b.wheel_tag
    cache.mkdir(parents=True)
    src = wheel_factory(name="foo", version="9.9.9", tag=b.wheel_tag)
    src.replace(cache / f"foo-1.0+gaaaaaaa-{b.wheel_tag}.whl")   # filename lies
    assert b.cached_wheel("foo", "a" * 40) is None


def test_cached_wheel_ignores_corrupt_wheel(tmp_path):
    b = _builder(Config(), tmp_path)
    b._tag = "cp311-cp311-linux_x86_64"
    cache = b._wheel_cache / "foo" / ("a" * 40) / b.wheel_tag
    cache.mkdir(parents=True)
    (cache / f"foo-1.0+gaaaaaaa-{b.wheel_tag}.whl").write_bytes(b"not a zip")
    assert b.cached_wheel("foo", "a" * 40) is None    # must not raise


def test_wheel_cache_keyed_by_tag_no_cross_interpreter_hit(tmp_path, wheel_factory, monkeypatch):
    # same (project, sha) built for two interpreters must not cross-hit
    b = _builder(Config(), tmp_path)
    builds: list[str] = []

    def fake_produce(res, sha, out, key):
        builds.append(b.wheel_tag)
        return wheel_factory(name="foo", version="1.0")

    monkeypatch.setattr(b, "_produce_natural_wheel", fake_produce)
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None)

    b._tag = "cp311-cp311-linux_x86_64"
    w1 = b.build_for_sha(res, "a" * 40)
    b._tag = "cp312-cp312-linux_x86_64"
    w2 = b.build_for_sha(res, "a" * 40)

    assert builds == ["cp311-cp311-linux_x86_64", "cp312-cp312-linux_x86_64"]  # two builds
    assert w1.parent.name == "cp311-cp311-linux_x86_64"
    assert w2.parent.name == "cp312-cp312-linux_x86_64"


# --------------------------------------------------------------------------- #
# run-scoped checkout workspace
# --------------------------------------------------------------------------- #
def test_checkout_dir_is_run_scoped_and_close_removes_it(tmp_path):
    b = _builder(Config(), tmp_path)
    assert b._checkout_root is None                       # nothing created up front
    d = b._checkout_dir                                   # lazily materialized on first use
    assert d.exists() and d.name.startswith("gwi-checkouts-")
    assert d.parent == tmp_path / "cache"                 # under the cache dir
    b.close()
    assert not d.exists() and b._checkout_root is None    # gone at teardown


def test_close_without_any_build_is_noop(tmp_path):
    # a run that never built a recipe source made no checkout dir -> close must not raise
    _builder(Config(), tmp_path).close()


def test_close_kills_in_flight_build_before_removing_checkout(tmp_path):
    # A build still running when the wrapper aborts must be killed (not left to be
    # corrupted by the rmtree / leak orphans). Simulate a live build in the checkout.
    b = _builder(Config(), tmp_path)
    checkout = b._checkout_dir           # materialize the run temp dir
    proc = builder_mod.subprocess.Popen(["sleep", "60"], cwd=checkout, start_new_session=True)
    b._procs._procs.add(proc)            # as _stream would register it
    b.close()
    assert proc.wait(timeout=5) is not None   # terminated by close()
    assert not checkout.exists()              # workspace removed afterwards


# --------------------------------------------------------------------------- #
# _ProcRegistry / _stream process-group teardown
# --------------------------------------------------------------------------- #
def test_proc_registry_terminates_tracked_process_group():
    reg = builder_mod._ProcRegistry()
    proc = builder_mod.subprocess.Popen(["sleep", "60"], start_new_session=True)
    with reg.track(proc):
        assert proc.poll() is None
        reg.terminate_all()
    assert proc.poll() is not None       # killed; already reaped by terminate_all


def test_stream_registers_then_unregisters(tmp_path):
    reg = builder_mod._ProcRegistry()
    rc, _ = builder_mod._stream(["true"], "k", registry=reg)
    assert rc == 0
    assert reg._procs == set()           # deregistered once the process finishes


def test_kill_group_terminates_gracefully():
    # An over-budget probe may be installing a toolchain outside our cache dir, so it
    # must get SIGTERM (a chance to unwind) rather than an immediate SIGKILL.
    proc = builder_mod.subprocess.Popen(["sleep", "60"], start_new_session=True)
    sent = []
    real = builder_mod._ProcRegistry._signal

    def spy(p, sig):
        sent.append(sig)
        real(p, sig)

    builder_mod._ProcRegistry._signal = staticmethod(spy)
    try:
        builder_mod._ProcRegistry.kill_group(proc)
    finally:
        builder_mod._ProcRegistry._signal = staticmethod(real)
    assert sent[0] == builder_mod.signal.SIGTERM      # graceful first
    assert proc.poll() is not None                    # and it did die


def test_stream_is_quiet_after_teardown_but_still_captures(caplog):
    # Output from a process we are about to kill must not reach the log, or it buries
    # the real error — but the failure message still needs the captured text.
    reg = builder_mod._ProcRegistry()
    reg.quiet = True
    with caplog.at_level("INFO", logger="git_wheels_index"):
        rc, out = builder_mod._stream(["echo", "NOISE-LINE"], "pkg@dead", registry=reg)
    assert rc == 0
    assert "NOISE-LINE" in out          # captured for the failure message
    assert caplog.records == []         # but nothing logged


def test_terminate_all_goes_quiet():
    reg = builder_mod._ProcRegistry()
    assert reg.quiet is False
    reg.terminate_all()
    assert reg.quiet is True


def test_builder_silence_sets_registry_quiet(tmp_path):
    b = _builder(Config(), tmp_path)
    assert b._procs.quiet is False
    b.silence()
    assert b._procs.quiet is True


def test_git_build_uses_isolated_pip_cache(tmp_path, monkeypatch):
    b = _builder(Config(), tmp_path)
    captured = {}

    def fake_stream(cmd, key, *, cwd=None, shell=False, env=None, registry=None):
        captured["cmd"] = cmd
        captured["env"] = env
        wdir = Path(cmd[cmd.index("-w") + 1])
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "foo-1.0-py3-none-any.whl").write_bytes(b"x")
        return 0, ""

    monkeypatch.setattr(builder_mod, "_stream", fake_stream)
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None)
    b._produce_natural_wheel(res, "a" * 40, tmp_path / "out", "foo@aaaaaaa")

    cmd = captured["cmd"]
    assert "--cache-dir" in cmd
    assert cmd[cmd.index("--cache-dir") + 1].endswith("pip-cache")
    assert captured["env"]["PIP_CACHE_DIR"].endswith("pip-cache")
    assert captured["env"]["UV_CACHE_DIR"].endswith("pip-cache")


def test_git_commands_runs_prep_then_tool_builds(tmp_path, monkeypatch):
    b = _builder(Config(), tmp_path)
    calls = []

    def fake_stream(cmd, key, *, cwd=None, shell=False, env=None, registry=None):
        calls.append(cmd)
        if isinstance(cmd, list) and "wheel" in cmd:  # the pip wheel build
            wdir = Path(cmd[cmd.index("-w") + 1])
            wdir.mkdir(parents=True, exist_ok=True)
            (wdir / "foo-1.0-py3-none-any.whl").write_bytes(b"x")
        return 0, ""

    monkeypatch.setattr(builder_mod, "_stream", fake_stream)
    monkeypatch.setattr(
        b, "_clone", lambda repo, ref, sha, dest, key: dest.mkdir(parents=True, exist_ok=True)
    )
    directive = SourceDirective(type="git", commands=("make prep",))
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, directive)

    b._produce_natural_wheel(res, "a" * 40, tmp_path / "out", "foo@aaaaaaa")

    prep_i = calls.index("make prep")                              # prep ran (shell string)
    wheel_i = next(i for i, c in enumerate(calls) if isinstance(c, list) and "wheel" in c)
    assert prep_i < wheel_i                                        # prep BEFORE the build


def test_git_commands_failure_raises(tmp_path, monkeypatch):
    b = _builder(Config(), tmp_path)
    monkeypatch.setattr(builder_mod, "_stream", lambda *a, **k: (1, "boom"))
    monkeypatch.setattr(
        b, "_clone", lambda repo, ref, sha, dest, key: dest.mkdir(parents=True, exist_ok=True)
    )
    directive = SourceDirective(type="git", commands=("false",))
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, directive)
    with pytest.raises(builder_mod.BuildError):
        b._produce_natural_wheel(res, "a" * 40, tmp_path / "out", "foo@aaaaaaa")


def test_build_for_sha_builds_then_reversions(tmp_path, wheel_factory, monkeypatch):
    cfg = Config()
    b = _builder(cfg, tmp_path)
    natural = wheel_factory(name="foo", version="9.0.0.dev5+gold")
    monkeypatch.setattr(b, "_produce_natural_wheel", lambda res, sha, out, key: natural)
    sha = "a" * 40
    res = HeadResolution("foo", "git", "https://x/foo", None, sha, None)
    wheel = b.build_for_sha(res, sha)
    assert wheel.name.startswith("foo-9.0.0+gaaaaaaa-")   # announced version, forced tag
    assert wheel.exists()


class _FakeProbeProc:
    """Stands in for the probe's Popen: writes the --report file, then exits."""

    def __init__(self, cmd, returncode=0, report=None, output="", slow=False):
        self.pid = 4242
        self.returncode = returncode
        self._output = output
        self._slow = slow
        if report is not None:
            Path(cmd[cmd.index("--report") + 1]).write_text(json.dumps(report))

    def communicate(self, timeout=None):
        if self._slow and timeout is not None:      # overruns the probe budget
            raise builder_mod.subprocess.TimeoutExpired("pip", timeout)
        self._slow = False                           # the post-kill communicate() call
        return self._output, None

    def poll(self):
        return self.returncode


def _fake_popen(monkeypatch, **kwargs):
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        return _FakeProbeProc(cmd, **kwargs)

    monkeypatch.setattr(builder_mod.subprocess, "Popen", popen)
    return calls


def test_probe_reads_version_and_metadata_from_report(tmp_path, monkeypatch):
    b = _builder(Config(), tmp_path)
    _fake_popen(monkeypatch, report={"install": [{"metadata": {
        "metadata_version": "2.1",
        "name": "Foo",                       # ignored: Name is forced below
        "version": "9.0.0.dev5+gx",
        "requires_python": ">=3.8",
        "requires_dist": ["click >=8", 'pytest; extra == "test"'],
        "provides_extra": ["test"],
    }}]})
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None)
    version, meta = b.probe(res)
    assert version == "9.0.0+gaaaaaaa"           # base 9.0.0 + g + short sha
    text = meta.decode()
    assert "Name: foo" in text                    # forced to the normalized wheel name
    assert "Version: 9.0.0+gaaaaaaa" in text      # forced to the announced version
    assert "Requires-Python: >=3.8" in text
    assert 'Requires-Dist: pytest; extra == "test"' in text   # markers preserved verbatim
    assert "Provides-Extra: test" in text


def test_probe_ignores_installed(tmp_path, monkeypatch):
    # Without --ignore-installed pip reports an empty `install` list for a package
    # already present in the target env, and the probe fails on readable metadata.
    b = _builder(Config(), tmp_path)
    calls = _fake_popen(monkeypatch, report={"install": [{"metadata": {"version": "1.0"}}]})
    b.probe(HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None))
    assert "--ignore-installed" in calls[0]


def test_probe_caches_per_sha(tmp_path, monkeypatch):
    b = _builder(Config(), tmp_path)
    calls = _fake_popen(monkeypatch, report={"install": [{"metadata": {"version": "1.0"}}]})
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None)
    first = b.probe(res)
    assert b.probe(res) == first        # same object from cache
    assert len(calls) == 1              # probed only once


def test_probe_failure_raises(tmp_path, monkeypatch):
    b = _builder(Config(), tmp_path)
    _fake_popen(monkeypatch, returncode=1, output="boom")
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None)
    with pytest.raises(builder_mod.BuildError):
        b.probe(res)


def test_probe_over_budget_raises_probe_too_slow(tmp_path, monkeypatch):
    # An overrunning probe is a routing decision (build instead), NOT a failure:
    # ProbeTooSlow must not be a GitWheelsIndexError or it would abort the run.
    b = _builder(Config(), tmp_path)
    _fake_popen(monkeypatch, slow=True)
    killed = []
    monkeypatch.setattr(builder_mod._ProcRegistry, "kill_group",
                        classmethod(lambda cls, proc: killed.append(proc)))
    res = HeadResolution("foo", "git", "https://x/foo", None, "a" * 40, None)
    with pytest.raises(builder_mod.ProbeTooSlow):
        b.probe(res)
    assert len(killed) == 1                                   # killed group-wide
    assert not isinstance(builder_mod.ProbeTooSlow(), GitWheelsIndexError)


def test_reconstruct_metadata_forces_name_version_and_omits_missing():
    meta = builder_mod._reconstruct_metadata(
        {"metadata_version": "2.3", "name": "orig", "version": "1.0",
         "requires_dist": ["six >=1.5"]},
        "my_pkg", "2.0+gabc",
    ).decode()
    assert "Metadata-Version: 2.3" in meta
    assert "Name: my_pkg" in meta and "Name: orig" not in meta       # forced
    assert "Version: 2.0+gabc" in meta and "Version: 1.0\n" not in meta
    assert "Requires-Dist: six >=1.5" in meta
    assert "Requires-Python" not in meta and "Provides-Extra" not in meta  # absent -> omitted


def test_read_wheel_metadata_returns_raw_bytes(wheel_factory):
    w = wheel_factory(name="foo", version="1.0")
    meta = builder_mod.read_wheel_metadata(w)
    assert b"Name: foo" in meta and b"Version: 1.0" in meta


def test_from_recipe_classifies_sources():
    plain = HeadResolution("foo", "git", "u", None, "a" * 40, None)
    git_cmds = HeadResolution("foo", "git", "u", None, "a" * 40,
                              SourceDirective(type="git", commands=("make",)))
    prebuild = HeadResolution("foo", "prebuild", "u", None, "a" * 40,
                              SourceDirective(type="prebuild", repo="u", wheel="*.whl"))
    assert plain.from_recipe is False
    assert git_cmds.from_recipe is True
    assert prebuild.from_recipe is True


# --------------------------------------------------------------------------- #
# BuildManager: dedup + request independence
# --------------------------------------------------------------------------- #
class _CountingBuilder:
    def __init__(self, delay=0.0):
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._delay = delay

    def build_for_sha(self, resolution, sha):
        with self._lock:
            self.calls.append(sha)
        time.sleep(self._delay)
        return Path(f"{sha[:7]}.whl")


def _res(sha):
    return HeadResolution("foo", "git", "u", None, sha, None)


def test_manager_dedups_concurrent_same_key():
    b = _CountingBuilder(delay=0.3)
    mgr = BuildManager(b, max_workers=3)
    sha = "s" * 40
    results = []

    def call():
        results.append(mgr.build(_res(sha), sha))

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert b.calls == [sha]                       # one build shared by all callers
    assert len({id(r) for r in results}) == 1     # every caller got the same result


def test_manager_pops_job_after_completion():
    b = _CountingBuilder()
    mgr = BuildManager(b, max_workers=2)
    sha = "t" * 40
    mgr.build(_res(sha), sha)
    assert mgr._jobs == {}   # finished job removed -> a re-request re-enters (cache hit)


def test_manager_distinct_keys_build_independently():
    b = _CountingBuilder()
    mgr = BuildManager(b, max_workers=2)
    mgr.build(_res("a" * 40), "a" * 40)
    mgr.build(_res("b" * 40), "b" * 40)
    assert sorted(b.calls) == ["a" * 40, "b" * 40]
