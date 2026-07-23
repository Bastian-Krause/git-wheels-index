"""Server routes via the Flask test client (no real builds).

Error paths *stall* (block on ``Problems`` teardown) so pip is never handed a
fallback-able response. Tests that trigger an error call ``problems.release()``
first so the stalled handler returns immediately.
"""

from __future__ import annotations

from git_wheels_index.builder import HeadResolution, ProbeTooSlow
from git_wheels_index.config import SourceDirective
from git_wheels_index.exceptions import BuildError, RepoResolutionError
from git_wheels_index.problems import Problems
from git_wheels_index.server import _JSON_CT, create_app, run_in_thread

_SHA = "ab12cd34" * 5  # 40 hex
_TAG = "cp312-cp312-linux_x86_64"


def _git(kind="git", directive=None):
    return HeadResolution("foo", kind, "https://x/foo", None, _SHA, directive)


def _meta(name="foo", version="1.0+gab12cd3") -> bytes:
    return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()


class StubBuilder:
    wheel_tag = _TAG

    def __init__(self, resolution=None, resolve_exc=None, version="1.0+gab12cd3",
                 metadata=None, probe_exc=None, cached=None):
        self._resolution = resolution
        self._resolve_exc = resolve_exc
        self._version = version
        self._metadata = metadata if metadata is not None else _meta(version=version)
        self._probe_exc = probe_exc
        self._cached = cached
        self.probes = 0

    def resolve(self, project, sha=None):
        if self._resolve_exc:
            raise self._resolve_exc
        return self._resolution

    def cached_wheel(self, project, sha):
        return self._cached

    def probe(self, resolution):
        self.probes += 1
        if self._probe_exc:
            raise self._probe_exc
        return self._version, self._metadata


class StubManager:
    def __init__(self, wheel=None, exc=None):
        self._wheel = wheel
        self._exc = exc
        self.calls = []

    def build(self, resolution, sha, timeout=None):
        self.calls.append((resolution.project, sha))
        if self._exc:
            raise self._exc
        return self._wheel


def _client(builder, manager, problems=None):
    return create_app(builder, manager, problems or Problems()).test_client()


def test_root_ok():
    assert _client(StubBuilder(), StubManager()).get("/").status_code == 200


# --------------------------------------------------------------------------- #
# /simple/ — probe-and-list (no build for plain git), core-metadata advertised
# --------------------------------------------------------------------------- #
def test_simple_lists_git_without_building():
    mgr = StubManager()
    c = _client(StubBuilder(resolution=_git(), version="2.0+gdead12"), mgr)
    resp = c.get("/simple/foo/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    fname = f"foo-2.0+gdead12-{_TAG}.whl"
    assert fname in body
    assert f"/build/foo/{_SHA}/{fname}" in body
    assert 'data-core-metadata="true"' in body   # PEP 658 advertised
    assert mgr.calls == []  # git is listed from a probe — NOT built at /simple/


def test_simple_uses_cached_wheel_without_probing_or_building(wheel_factory):
    # An already-built wheel answers /simple/ outright: no probe, no build, no clone.
    wheel = wheel_factory(name="foo", version="7.0+gcached")
    mgr = StubManager()
    builder = StubBuilder(resolution=_git(), cached=wheel)
    resp = _client(builder, mgr).get("/simple/foo/")
    assert resp.status_code == 200
    assert wheel.name in resp.get_data(as_text=True)
    assert builder.probes == 0 and mgr.calls == []


def test_metadata_uses_cached_wheel_without_probing(wheel_factory):
    wheel = wheel_factory(name="foo", version="7.0+gcached")
    mgr = StubManager()
    builder = StubBuilder(resolution=_git(), cached=wheel)
    resp = _client(builder, mgr).get(f"/build/foo/{_SHA}/{wheel.name}.metadata")
    assert resp.status_code == 200
    assert b"Version: 7.0+gcached" in resp.get_data()
    assert builder.probes == 0 and mgr.calls == []


def test_simple_builds_when_probe_is_too_slow(wheel_factory):
    # ProbeTooSlow is a routing decision, not a failure: build instead, don't abort.
    wheel = wheel_factory(name="foo", version="5.0+gslow")
    mgr = StubManager(wheel=wheel)
    probs = Problems()
    builder = StubBuilder(resolution=_git(), probe_exc=ProbeTooSlow("too slow"))
    resp = _client(builder, mgr, probs).get("/simple/foo/")
    assert resp.status_code == 200
    assert wheel.name in resp.get_data(as_text=True)
    assert mgr.calls == [("foo", _SHA)]      # fell through to a build
    assert probs.snapshot() == {}            # and recorded no problem


def test_listing_advertises_requires_python():
    # An unsupported Python must let the client skip our wheel (like a version cap)
    # instead of failing the install — so Requires-Python has to reach the listing.
    meta = (b"Metadata-Version: 2.1\nName: foo\nVersion: 2.0+gdead12\n"
            b"Requires-Python: >=3.10,<3.15\n")
    builder = StubBuilder(resolution=_git(), version="2.0+gdead12", metadata=meta)
    c = _client(builder, StubManager())
    html = c.get("/simple/foo/").get_data(as_text=True)
    assert 'data-requires-python="&gt;=3.10,&lt;3.15"' in html      # escaped in HTML
    data = c.get("/simple/foo/", headers={"Accept": _JSON_CT}).get_json()
    assert data["files"][0]["requires-python"] == ">=3.10,<3.15"


def test_listing_requires_python_empty_when_metadata_has_none():
    c = _client(StubBuilder(resolution=_git(), metadata=_meta()), StubManager())
    html = c.get("/simple/foo/").get_data(as_text=True)
    assert "data-requires-python" not in html
    data = c.get("/simple/foo/", headers={"Accept": _JSON_CT}).get_json()
    assert data["files"][0]["requires-python"] == ""


def test_teardown_failures_are_not_logged(caplog):
    # After freeze() the failure is our own doing (we killed the build): no summary
    # entry AND no log line — dozens of these would bury the one real error.
    probs = Problems()
    probs.freeze()
    probs.release()
    c = _client(StubBuilder(resolve_exc=RepoResolutionError("killed")), StubManager(), probs)
    with caplog.at_level("WARNING", logger="git_wheels_index"):
        assert c.get("/simple/foo/").status_code == 503
    assert probs.snapshot() == {}
    assert caplog.records == []


def test_genuine_failure_is_logged(caplog):
    probs = Problems()
    probs.release()
    c = _client(StubBuilder(resolve_exc=RepoResolutionError("no repo")), StubManager(), probs)
    with caplog.at_level("WARNING", logger="git_wheels_index"):
        assert c.get("/simple/foo/").status_code == 503
    assert "no repo" in caplog.text


def test_simple_json_advertises_core_metadata():
    c = _client(StubBuilder(resolution=_git(), version="2.0+gdead12"), StubManager())
    resp = c.get("/simple/foo/", headers={"Accept": _JSON_CT})
    assert resp.status_code == 200 and resp.mimetype == _JSON_CT
    f = resp.get_json()["files"][0]
    assert f["filename"] == f"foo-2.0+gdead12-{_TAG}.whl"
    assert f["core-metadata"] is True


def test_simple_prebuild_builds(wheel_factory):
    wheel = wheel_factory(name="foo", version="3.0+gabc")
    mgr = StubManager(wheel=wheel)
    c = _client(StubBuilder(resolution=_git("prebuild")), mgr)
    resp = c.get("/simple/foo/")
    assert resp.status_code == 200
    assert wheel.name in resp.get_data(as_text=True)
    assert mgr.calls == [("foo", _SHA)]  # a recipe source has no probe -> builds here


def test_simple_git_with_commands_builds(wheel_factory):
    # git + pre-build commands is a recipe source: no probe, builds at /simple/
    wheel = wheel_factory(name="foo", version="3.0+gabc")
    mgr = StubManager(wheel=wheel)
    directive = SourceDirective(type="git", commands=("make prep",))
    c = _client(StubBuilder(resolution=_git("git", directive)), mgr)
    resp = c.get("/simple/foo/")
    assert resp.status_code == 200
    assert mgr.calls == [("foo", _SHA)]


def test_simple_404_when_pypi_typed():
    # resolve() returns None only for type="pypi" (E1) -> 404, no problem recorded
    probs = Problems()
    c = _client(StubBuilder(resolution=None), StubManager(), probs)
    assert c.get("/simple/somepkg/").status_code == 404
    assert probs.snapshot() == {}


def test_simple_records_and_stalls_on_resolve_error():
    probs = Problems()
    probs.release()  # so the stall returns immediately
    c = _client(StubBuilder(resolve_exc=RepoResolutionError("no repo")), StubManager(), probs)
    resp = c.get("/simple/foo/")
    assert resp.status_code == 503
    assert "foo" in probs.snapshot()


def test_simple_records_on_probe_error():
    probs = Problems()
    probs.release()
    c = _client(StubBuilder(resolution=_git(), probe_exc=BuildError("probe boom")), StubManager(), probs)
    assert c.get("/simple/foo/").status_code == 503
    assert "probe boom" in probs.snapshot()["foo"]


# --------------------------------------------------------------------------- #
# /build/…whl — build (SHA-cached) and serve
# --------------------------------------------------------------------------- #
def test_build_get_serves_wheel_bytes(tmp_path):
    wheel = tmp_path / f"foo-1.0+gab12cd3-{_TAG}.whl"
    wheel.write_bytes(b"REALWHEELBYTES")
    mgr = StubManager(wheel=wheel)
    c = _client(StubBuilder(resolution=_git()), mgr)
    resp = c.get(f"/build/foo/{_SHA}/{wheel.name}")
    assert resp.status_code == 200
    assert resp.get_data() == b"REALWHEELBYTES"
    assert resp.headers["Content-Length"] == str(len(b"REALWHEELBYTES"))
    assert mgr.calls == [("foo", _SHA)]


def test_build_head_returns_headers_only(tmp_path):
    wheel = tmp_path / f"foo-1.0+gab12cd3-{_TAG}.whl"
    wheel.write_bytes(b"REALWHEELBYTES")
    c = _client(StubBuilder(resolution=_git()), StubManager(wheel=wheel))
    resp = c.head(f"/build/foo/{_SHA}/{wheel.name}")
    assert resp.status_code == 200
    assert resp.get_data() == b""
    assert resp.headers["Content-Length"] == str(len(b"REALWHEELBYTES"))


def test_build_records_on_build_error():
    probs = Problems()
    probs.release()
    c = _client(StubBuilder(resolution=_git()), StubManager(exc=BuildError("compile failed")), probs)
    resp = c.get(f"/build/foo/{_SHA}/foo-1.0-{_TAG}.whl")
    assert resp.status_code == 503
    assert "compile failed" in probs.snapshot()["foo"]


# --------------------------------------------------------------------------- #
# /build/…whl.metadata — PEP 658 core metadata
# --------------------------------------------------------------------------- #
def test_metadata_get_serves_probe_metadata_without_building():
    mgr = StubManager()
    meta = _meta(version="2.0+gdead12")
    c = _client(StubBuilder(resolution=_git(), version="2.0+gdead12", metadata=meta), mgr)
    fname = f"foo-2.0+gdead12-{_TAG}.whl"
    resp = c.get(f"/build/foo/{_SHA}/{fname}.metadata")
    assert resp.status_code == 200
    assert resp.get_data() == meta
    assert resp.headers["Content-Length"] == str(len(meta))
    assert mgr.calls == []  # plain-git metadata comes from the probe — NO build


def test_metadata_head_returns_headers_only():
    meta = _meta(version="2.0+gdead12")
    c = _client(StubBuilder(resolution=_git(), version="2.0+gdead12", metadata=meta), StubManager())
    fname = f"foo-2.0+gdead12-{_TAG}.whl"
    resp = c.head(f"/build/foo/{_SHA}/{fname}.metadata")
    assert resp.status_code == 200
    assert resp.get_data() == b""
    assert resp.headers["Content-Length"] == str(len(meta))


def test_metadata_recipe_reads_from_built_wheel(wheel_factory):
    # prebuild / git+commands: metadata is read from the built (reversioned) wheel
    wheel = wheel_factory(name="foo", version="3.0+gabc")
    mgr = StubManager(wheel=wheel)
    c = _client(StubBuilder(resolution=_git("prebuild")), mgr)
    resp = c.get(f"/build/foo/{_SHA}/{wheel.name}.metadata")
    assert resp.status_code == 200
    assert b"Version: 3.0+gabc" in resp.get_data()
    assert mgr.calls == [("foo", _SHA)]  # recipe builds (cache hit) to read metadata


def test_metadata_records_on_probe_error():
    probs = Problems()
    probs.release()
    c = _client(StubBuilder(resolution=_git(), probe_exc=BuildError("probe boom")), StubManager(), probs)
    fname = f"foo-1.0+gab12cd3-{_TAG}.whl"
    assert c.get(f"/build/foo/{_SHA}/{fname}.metadata").status_code == 503
    assert "probe boom" in probs.snapshot()["foo"]


def test_metadata_mismatch_stalls_and_aborts():
    # metadata whose Version != the advertised filename would make the client silently
    # use PyPI; the self-check turns that into a stall+abort instead.
    probs = Problems()
    probs.release()
    c = _client(StubBuilder(resolution=_git(), version="2.0+gdead12", metadata=_meta(version="9.9.9")),
                StubManager(), probs)
    fname = f"foo-2.0+gdead12-{_TAG}.whl"
    resp = c.get(f"/build/foo/{_SHA}/{fname}.metadata")
    assert resp.status_code == 503
    assert "foo" in probs.snapshot()


def test_success_responses_are_not_cacheable(tmp_path):
    wheel = tmp_path / f"foo-1.0+gab12cd3-{_TAG}.whl"
    wheel.write_bytes(b"x")
    c = _client(StubBuilder(resolution=_git(), version="1.0+gab12cd3"), StubManager(wheel=wheel))
    for path in ("/", "/simple/foo/", f"/build/foo/{_SHA}/{wheel.name}",
                 f"/build/foo/{_SHA}/{wheel.name}.metadata"):
        resp = c.get(path)
        assert "no-store" in resp.headers.get("Cache-Control", "")


def test_run_in_thread_binds_free_port_and_shuts_down():
    app = create_app(StubBuilder(resolution=None), StubManager(), Problems())
    server, port = run_in_thread(app)
    try:
        assert isinstance(port, int) and port > 0
    finally:
        server.shutdown()
