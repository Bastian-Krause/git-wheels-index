"""Build a wheel from a package's git development branch, on demand.

* :func:`reversion_wheel` — rewrite a freshly built wheel's version to the
  announced ``<base>+g<sha>`` (see :func:`git_wheels_index.metadata.announced_version`)
  so it is a *final* release that wins by default yet is still excluded by a real
  upper bound.
* :class:`Builder` — resolve a project's git HEAD, build (via ``pip wheel`` for a
  normal package or a config ``prebuild`` recipe for exotic ones), reversion, and
  cache the result on disk keyed by commit SHA. Build subprocess output is streamed
  to the log, prefixed with the build key.
* :class:`BuildManager` — runs builds on a background thread pool, deduplicated per
  ``(project, sha)``. Because a build runs on a pool thread rather than the request
  thread, **an abandoned HTTP request never cancels a build**: it runs to completion
  and lands in the SHA cache for the next request to pick up.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from .config import Config, SourceDirective
from .exceptions import BuildError
from .metadata import announced_version, git_wheel_spec
from .repo_resolution import resolve_repo_url, resolve_sha

logger = logging.getLogger("git_wheels_index")

# A probe is meant to be *cheap* (PEP 517 metadata, no compile). Overrunning this
# budget means the package's metadata isn't cheap (grpcio cythonizes during
# `egg_info`), so we stop probing it and build instead — see `Builder.probe`.
_PROBE_BUDGET = 60
# Bound so a hung build becomes a loud error instead of blocking until the client's
# HTTP timeout fires (after which pip would skip us and use PyPI). Must stay below
# the injected client timeout (cli._HTTP_TIMEOUT).
_BUILD_TIMEOUT = 5400


class ProbeTooSlow(Exception):
    """The metadata probe overran its budget — build the wheel instead.

    Deliberately *not* a :class:`GitWheelsIndexError`: this is a routing decision,
    not a failure, so it must never reach the never-fall-back error path.
    """


# --------------------------------------------------------------------------- #
# in-flight build subprocesses (so an abort can kill them before the checkout is
# removed — otherwise the rmtree corrupts a still-running build and the killed
# build leaks orphan children, e.g. bazel/protoc/java)
# --------------------------------------------------------------------------- #
class _ProcRegistry:
    """Tracks live build subprocesses and kills their process groups on teardown."""

    def __init__(self) -> None:
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.quiet = False   # set at teardown: stop echoing output we are about to kill

    @contextmanager
    def track(self, proc: subprocess.Popen):
        with self._lock:
            self._procs.add(proc)
        try:
            yield
        finally:
            with self._lock:
                self._procs.discard(proc)

    def terminate_all(self) -> None:
        """SIGTERM (then SIGKILL) every tracked process *group* and wait for exit.

        Signalling the group — each build runs in its own session (see
        :func:`_stream`) — reaps the whole tree, so no orphan compiler/daemon
        children survive. Waiting before returning guarantees nothing is still
        writing when the caller removes the checkout.
        """
        self.quiet = True    # nothing we are about to kill should keep logging
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        self._kill(procs)

    @classmethod
    def kill_group(cls, proc: subprocess.Popen) -> None:
        """Stop one process' whole group (an overrunning metadata probe).

        Graceful — SIGTERM first — because the probe may be *installing* something
        outside our cache dir (maturin bootstraps a Rust toolchain into
        ``~/.cache/puccinialin``); SIGKILLing that leaves a half-written toolchain
        that fails the build we are about to start.
        """
        cls._kill([proc])

    @classmethod
    def _kill(cls, procs: list[subprocess.Popen]) -> None:
        """SIGTERM every process group, then SIGKILL whatever outlives the grace."""
        for proc in procs:
            cls._signal(proc, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                cls._signal(proc, signal.SIGKILL)
        for proc in procs:  # reap anything we had to SIGKILL
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# --------------------------------------------------------------------------- #
# streaming subprocess helper
# --------------------------------------------------------------------------- #
def _stream(
    cmd, key: str, *, cwd: Path | None = None, shell: bool = False,
    env: dict[str, str] | None = None, registry: "_ProcRegistry | None" = None,
) -> tuple[int, str]:
    """Run a build subprocess, streaming combined output to the log per-``key``.

    Each line is logged at INFO prefixed with the build key (``project@shortsha``)
    so concurrent builds stay readable — until ``registry`` goes quiet at teardown,
    after which lines are still captured but no longer echoed (they belong to a
    process we are killing, and would bury the real error). ``env`` is overlaid on
    the current environment (used to point builds at an isolated pip/uv cache). The
    child runs in its own session/process group so ``registry`` (if given) can kill
    the whole tree on teardown. Returns ``(returncode, captured_output)``.
    """
    quiet = (lambda: registry is not None and registry.quiet)
    display = cmd if shell else " ".join(cmd)
    if not quiet():
        logger.info("[%s] $ %s", key, display)
    run_env = {**os.environ, **env} if env else None
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, env=run_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        start_new_session=True,
    )
    captured: list[str] = []
    assert proc.stdout is not None
    with (registry.track(proc) if registry is not None else nullcontext()):
        for line in proc.stdout:
            if not quiet():
                logger.info("[%s] %s", key, line.rstrip("\n"))
            captured.append(line)
        proc.wait()
    return proc.returncode, "".join(captured)


def _repo_key(repo: str, ref: str | None) -> str:
    """A filesystem-safe checkout dir name keyed by repo (+ref).

    Keying by repo — not package name — lets several prebuild sources on the same
    monorepo (grpcio-reflection / grpcio-channelz) share one checkout.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", repo.partition("#")[0]).strip("_")
    if ref:
        slug += "__" + re.sub(r"[^A-Za-z0-9._-]+", "_", ref).strip("_")
    return slug or "checkout"


def _tail(text: str, n: int = 25) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


def _wheel_name(project: str) -> str:
    """The name component of a wheel filename (normalized, non-alnum runs -> ``_``)."""
    return canonicalize_name(project).replace("-", "_")


# --------------------------------------------------------------------------- #
# wheel re-versioning (surgery)
# --------------------------------------------------------------------------- #
def _record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{path},sha256={digest.decode('ascii')},{len(data)}"


def reversion_wheel(src: Path, name: str, version: str, tag: str, dest_dir: Path) -> Path:
    """Rewrite ``src`` to the announced ``name``/``version``/``tag`` in ``dest_dir``.

    Everything identifying the distribution comes from the *configuration*, not from
    the build backend: the filename becomes ``{name}-{version}-{tag}.whl``, and the
    ``*.dist-info`` directory, ``METADATA`` ``Name:``/``Version:`` and the ``WHEEL``
    ``Tag:`` are rewritten to match (``RECORD`` is regenerated last). A backend may
    well build a *differently named* distribution — a repo override can point at a
    repo whose package was renamed upstream — and pip rejects a wheel whose
    ``.dist-info`` disagrees with its filename, so the two must be forced together.
    Returns the new path.
    """
    dest = dest_dir / f"{name}-{version}-{tag}.whl"

    with zipfile.ZipFile(src) as zin:
        di = next((n for n in zin.namelist() if n.endswith(".dist-info/METADATA")), None)
        if di is None:
            raise BuildError(f"{src.name} has no .dist-info/METADATA")
        old_di = di[: -len("/METADATA")]                    # foo-1.2.dev3.dist-info
        backend_name = old_di[: -len(".dist-info")].rpartition("-")[0]
        if canonicalize_name(backend_name) != canonicalize_name(name):
            # The config asked for this package from this repo, so serve it under that
            # name — but say so: if an override points at the wrong repo, this line is
            # the thread to pull when the import later fails.
            logger.info("serving %s as %s (built distribution has a different name)",
                        backend_name, name)
        new_di = f"{name}-{version}.dist-info"

        entries: list[tuple[str, bytes]] = []
        for info in zin.infolist():
            n = info.filename
            if n == f"{old_di}/RECORD":
                continue  # regenerated below
            data = zin.read(n)
            if n.startswith(old_di + "/"):
                n = new_di + n[len(old_di):]
            if n == f"{new_di}/METADATA":
                data = _rewrite_version(data, version)
                data = _rewrite_name(data, name)
            elif n == f"{new_di}/WHEEL":
                data = _rewrite_wheel_tag(data, tag)
            entries.append((n, data))

    record = "".join(f"{_record_line(n, d)}\n" for n, d in entries)
    record += f"{new_di}/RECORD,,\n"
    entries.append((f"{new_di}/RECORD", record.encode("utf-8")))

    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, data in entries:
            zout.writestr(n, data)
    return dest


def _rewrite_wheel_tag(wheel_meta: bytes, tag: str) -> bytes:
    """Replace all ``Tag:`` lines in a ``WHEEL`` file with a single forced tag."""
    out, done = [], False
    for line in wheel_meta.split(b"\n"):
        if line[:4].lower() == b"tag:":
            if not done:
                out.append(f"Tag: {tag}".encode("utf-8"))
                done = True
        else:
            out.append(line)
    if not done:
        out.append(f"Tag: {tag}".encode("utf-8"))
    return b"\n".join(out)


def _rewrite_version(metadata: bytes, version: str) -> bytes:
    return _rewrite_header(metadata, "Version", version)


def _rewrite_name(metadata: bytes, name: str) -> bytes:
    return _rewrite_header(metadata, "Name", name)


def _rewrite_header(metadata: bytes, header: str, value: str) -> bytes:
    """Replace the first ``header:`` line in an RFC822 ``METADATA`` blob."""
    prefix = f"{header.lower()}:".encode("ascii")
    out = []
    replaced = False
    for line in metadata.split(b"\n"):
        if not replaced and line[: len(prefix)].lower() == prefix:
            out.append(f"{header}: {value}".encode("utf-8"))
            replaced = True
        else:
            out.append(line)
    return b"\n".join(out)


def _wheel_is_current(wheel: Path, project: str) -> bool:
    """Is this cached wheel still one we would serve for ``project``?

    The cache key — ``<project>/<sha>/<wheel_tag>`` — says nothing about how the wheel
    was *stamped*, so an entry built by an older version of this tool can disagree with
    what we produce today (its ``METADATA`` may carry the build backend's name rather
    than the configured one). Serving it trips the server's own metadata self-check and
    aborts the run, so verify a hit instead of trusting it: the metadata's ``Name`` must
    be this project and its ``Version`` must match the wheel's filename. An unreadable
    or truncated wheel is likewise not current.
    """
    try:
        meta = read_wheel_metadata(wheel).decode("utf-8", "replace")
    except (BuildError, zipfile.BadZipFile, OSError):
        return False
    name = version = None
    for line in meta.splitlines():
        if not line.strip():
            break                      # end of headers
        low = line.lower()
        if name is None and low.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif version is None and low.startswith("version:"):
            version = line.split(":", 1)[1].strip()
    if name is None or canonicalize_name(name) != canonicalize_name(project):
        return False
    # `{name}-{version}-{tags…}.whl` — neither field contains a "-"
    parts = wheel.name[: -len(".whl")].split("-")
    return len(parts) >= 2 and version == parts[1]


def read_wheel_metadata(wheel: Path) -> bytes:
    """The raw RFC822 ``METADATA`` bytes of a built wheel (for the ``.metadata`` route)."""
    with zipfile.ZipFile(wheel) as z:
        di = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
        if di is None:
            raise BuildError(f"{wheel.name} has no .dist-info/METADATA")
        return z.read(di)


def _metadata_version(wheel: Path) -> str:
    for line in read_wheel_metadata(wheel).decode("utf-8", "replace").splitlines():
        if line[:8].lower() == "version:":
            return line.split(":", 1)[1].strip()
    raise BuildError(f"no Version header in {wheel.name}")


def _reconstruct_metadata(report_md: dict, name: str, version: str) -> bytes:
    """A minimal RFC822 ``METADATA`` from a ``pip --report`` metadata object.

    Only the fields the resolver needs are emitted — version, python/dist requirements,
    extras — because the client uses ``.metadata`` solely to *resolve*; the real wheel
    carries the full metadata at install time. ``Name`` and ``Version`` are **forced** to
    our announced values: a ``.metadata`` whose name/version don't match the advertised
    wheel makes pip/uv silently fall back to PyPI, so they must agree by construction.
    ``requires_dist`` from the report preserves every ``; extra == …`` / marker verbatim.
    """
    lines = [
        f"Metadata-Version: {report_md.get('metadata_version', '2.1')}",
        f"Name: {name}",
        f"Version: {version}",
    ]
    if summary := report_md.get("summary"):
        lines.append(f"Summary: {summary.splitlines()[0]}")
    if req_python := report_md.get("requires_python"):
        lines.append(f"Requires-Python: {req_python}")
    for extra in report_md.get("provides_extra") or []:
        lines.append(f"Provides-Extra: {extra}")
    for req in report_md.get("requires_dist") or []:
        lines.append(f"Requires-Dist: {req}")
    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# resolution + build
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeadResolution:
    project: str            # canonical name
    kind: str               # "git" | "prebuild"
    repo_url: str           # may carry a #subdirectory= fragment (git kind)
    ref: str | None
    sha: str
    directive: SourceDirective | None

    @property
    def from_recipe(self) -> bool:
        """True if the wheel comes from a recipe (``prebuild``, or ``git`` with pre-build
        ``commands``) rather than a plain ``pip wheel``. Recipe sources have no cheap
        metadata probe, so they build at ``/simple/`` and their metadata is read from the
        built wheel; plain git is probed (no build) and built lazily at ``/build/``.
        """
        return self.kind == "prebuild" or bool(self.directive and self.directive.commands)


class Builder:
    """Resolve, build, reversion and cache a package's git wheel."""

    def __init__(
        self,
        config: Config,
        cache_dir: Path,
        *,
        python: str | None = None,
    ) -> None:
        self._config = config
        self._cache_dir = cache_dir
        self._wheel_cache = cache_dir / "wheels"
        # recipe checkouts are a per-run build workspace, not a cache: created under
        # _checkout_dir on first use and dropped by close() at teardown (see property).
        self._checkout_root: Path | None = None
        # isolate pip/uv build caches under our cache dir so building the graph
        # never pollutes the operator's global ~/.cache/pip (this is a test tool)
        self._pip_cache = (cache_dir / "pip-cache").resolve()
        self._python = python or sys.executable
        self._checkout_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._procs = _ProcRegistry()                 # live build subprocesses
        self._probes: dict[str, tuple[str, bytes]] = {}   # sha -> (version, metadata)
        self._probes_guard = threading.Lock()
        self._tag: str | None = None                 # computed lazily on first use

    def _cache_env(self) -> dict[str, str]:
        c = str(self._pip_cache)
        return {"PIP_CACHE_DIR": c, "UV_CACHE_DIR": c}

    # -- run-scoped checkout workspace ------------------------------------- #
    @property
    def _checkout_dir(self) -> Path:
        """Scratch root for recipe checkouts, created on first use and removed by
        :meth:`close` at teardown — a recipe clone is a build workspace, not a cache,
        so it never outlives the run. The dir is unique per process, so concurrent
        wrappers sharing a cache dir don't collide and every run starts from fresh clones.
        """
        with self._locks_guard:
            if self._checkout_root is None:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._checkout_root = Path(
                    tempfile.mkdtemp(prefix="gwi-checkouts-", dir=self._cache_dir)
                )
            return self._checkout_root

    def silence(self) -> None:
        """Stop echoing in-flight build output — the run is aborting.

        Called as soon as the wrapper decides to abort (not at teardown): builds keep
        streaming while the command is being terminated, and a killed `git checkout`
        alone can emit a line per file, burying the failure we are about to report.
        Output is still captured, so failure messages are unaffected.
        """
        self._procs.quiet = True

    def close(self) -> None:
        """Stop in-flight builds, then remove the run-scoped checkout workspace.

        Builds are killed (process-group-wide) **before** the rmtree, so an
        abandoned build can't be corrupted by files vanishing under it (spurious
        "No such file or directory" failures) and can't leak orphan children.
        """
        self._procs.terminate_all()
        with self._locks_guard:
            root, self._checkout_root = self._checkout_root, None
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    def _compute_tag(self) -> str:
        """The target interpreter's primary wheel tag, e.g. ``cp312-cp312-linux_x86_64``.

        Advertised for every wheel; :func:`reversion_wheel` forces it onto the built
        wheel (a no-op for a matching compiled wheel; harmlessly narrows a pure one).
        """
        code = (
            "import sysconfig,sys;"
            "v=f'{sys.version_info.major}{sys.version_info.minor}';"
            "p=sysconfig.get_platform().replace('-','_').replace('.','_');"
            "print(f'cp{v}-cp{v}-{p}')"
        )
        proc = subprocess.run([self._python, "-c", code], capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise BuildError(f"could not determine wheel tag for {self._python}: {proc.stderr}")
        return proc.stdout.strip()

    @property
    def wheel_tag(self) -> str:
        if self._tag is None:
            self._tag = self._compute_tag()
        return self._tag

    # -- resolution -------------------------------------------------------- #
    def resolve(self, project: str, sha: str | None = None) -> HeadResolution | None:
        """Resolve a project's git build target.

        Returns ``None`` **only** for ``type="pypi"`` (the one intended "use PyPI"
        case — the server answers ``404``). Otherwise it raises: a repo that can't be
        determined → :class:`RepoResolutionError` (X1/X2), a repo whose HEAD sha can't
        be looked up → :class:`BuildError` (X3). With ``sha`` (from a ``/build`` URL)
        the result pins to that commit and skips the ``ls-remote`` lookup.
        """
        directive = self._config.source_for(project)
        if directive is not None and directive.type == "pypi":
            return None
        if directive is not None and directive.type == "prebuild":
            kind, repo_url = "prebuild", directive.repo or ""
        elif directive is not None and directive.url:
            kind, repo_url = "git", directive.url
        else:
            kind, repo_url = "git", resolve_repo_url(self._config, project)  # raises X1/X2
        ref = directive.ref if directive else None
        if sha is None:
            sha = resolve_sha(repo_url, ref)
            if sha is None:
                raise BuildError(f"could not resolve HEAD commit for {project} ({repo_url})")
        return HeadResolution(canonicalize_name(project), kind, repo_url, ref, sha, directive)

    # -- probe (the lazy /simple/ path) ------------------------------------ #
    def probe(self, resolution: HeadResolution) -> tuple[str, bytes]:
        """The announced version **and** resolution metadata for a git source, no build.

        Runs ``pip install --dry-run --report`` (PEP 517 metadata only — no compile for
        backends that support it) and returns ``(<base>+g<sha>, metadata_bytes)`` where
        ``metadata_bytes`` is a minimal RFC822 ``METADATA`` with ``Name``/``Version``
        forced to the announced values (so it matches the served wheel). Cached per sha.
        Raises :class:`BuildError` on failure (X4), or :class:`ProbeTooSlow` when the
        probe overruns ``_PROBE_BUDGET`` — the caller then builds instead. Not for a
        recipe source — the recipe *is* the build (see :attr:`HeadResolution.from_recipe`).
        """
        sha = resolution.sha
        with self._probes_guard:
            if sha in self._probes:
                return self._probes[sha]

        key = f"{resolution.project}@{sha[:7]}"
        spec = git_wheel_spec(resolution.repo_url, resolution.ref, sha)
        with tempfile.TemporaryDirectory(prefix="gwi-probe-") as tmp:
            report = Path(tmp) / "report.json"
            # --ignore-installed: without it pip reports an empty `install` list when the
            # package is already present in the target environment, and the probe would
            # fail on metadata we could have read perfectly well.
            # --ignore-requires-python: whether the dev branch supports this interpreter
            # is the *client's* call — we read the metadata, advertise its Requires-Python
            # in the listing, and pip/uv skip our wheel (falling back to PyPI, like a
            # version cap). Without this the probe itself would fail and abort the run.
            cmd = [self._python, "-m", "pip", "install", spec, "--dry-run", "--no-deps",
                   "--ignore-installed", "--ignore-requires-python",
                   "--report", str(report), "--quiet", "--cache-dir", str(self._pip_cache)]
            # Own session + registry so an overrunning probe can be killed *group-wide*
            # (it spawns git and possibly a compiler; subprocess.run's timeout would
            # leave those grandchildren behind).
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={**os.environ, **self._cache_env()}, start_new_session=True,
            )
            with self._procs.track(proc):
                try:
                    out, _ = proc.communicate(timeout=_PROBE_BUDGET)
                except subprocess.TimeoutExpired:
                    _ProcRegistry.kill_group(proc)
                    proc.communicate()
                    raise ProbeTooSlow(
                        f"metadata probe for {key} exceeded {_PROBE_BUDGET}s"
                    )
            if proc.returncode != 0:
                raise BuildError(
                    f"version probe for {key} failed (exit {proc.returncode}):\n{_tail(out)}"
                )
            try:
                report_md = json.loads(report.read_text())["install"][0]["metadata"]
                raw = report_md["version"]
            except (OSError, ValueError, LookupError) as exc:
                raise BuildError(f"version probe for {key}: could not read metadata ({exc})")

        ann = announced_version(raw, sha)
        meta = _reconstruct_metadata(report_md, _wheel_name(resolution.project), ann)
        with self._probes_guard:
            self._probes[sha] = (ann, meta)
        return ann, meta

    # -- build ------------------------------------------------------------- #
    def cached_wheel(self, project: str, sha: str) -> Path | None:
        """An already-built wheel for ``(project, sha)``, or ``None``.

        Keyed by the wheel tag too: the forced tag depends on ``--python``, so a hit
        must never hand back a wheel built for a different interpreter/platform. A hit
        means ``/simple/`` can answer with no probe *and* no build (the metadata comes
        from the wheel), which is what makes a re-run — or a compile-then-install —
        cheap.
        """
        cache = self._wheel_cache / project / sha / self.wheel_tag
        for wheel in sorted(cache.glob("*.whl")):
            if _wheel_is_current(wheel, project):
                return wheel
            # Built by an older version of this tool (the key can't capture how we
            # stamp a wheel), or truncated. Evict it: it can never be served, and
            # leaving it would re-open the zip on every lookup.
            logger.info("discarding stale cached wheel %s — rebuilding", wheel.name)
            wheel.unlink(missing_ok=True)
        return None

    def build_for_sha(self, resolution: HeadResolution, sha: str) -> Path:
        """Build (or return the cached) wheel for ``resolution`` pinned at ``sha``."""
        project = resolution.project
        existing = self.cached_wheel(project, sha)
        if existing is not None:
            logger.debug("[%s@%s] cache hit: %s", project, sha[:7], existing.name)
            return existing
        cache = self._wheel_cache / project / sha / self.wheel_tag

        key = f"{project}@{sha[:7]}"
        with tempfile.TemporaryDirectory(prefix="gwi-build-") as tmp:
            natural = self._produce_natural_wheel(resolution, sha, Path(tmp), key)
            ann = announced_version(_metadata_version(natural), sha)
            final = reversion_wheel(natural, _wheel_name(project), ann, self.wheel_tag, cache)
        logger.info("[%s] built %s", key, final.name)
        return final

    def _produce_natural_wheel(
        self, resolution: HeadResolution, sha: str, out_dir: Path, key: str
    ) -> Path:
        directive = resolution.directive
        if resolution.kind == "prebuild":
            assert directive is not None
            return self._prebuild(directive, sha, out_dir, key)
        if directive is not None and directive.commands:
            # git source with a pre-build hook: clone, run the commands, then let
            # the tool build the wheel from the prepared checkout.
            return self._git_build_with_commands(resolution, sha, out_dir, key)
        # plain git: pip clones + builds in one shot
        spec = git_wheel_spec(resolution.repo_url, resolution.ref, sha)
        return self._pip_wheel([spec], out_dir, key, label=spec)

    def _git_build_with_commands(
        self, resolution: HeadResolution, sha: str, out_dir: Path, key: str
    ) -> Path:
        directive = resolution.directive
        assert directive is not None
        # subdir from the directive, else a #subdirectory= fragment on the repo URL
        subdir = directive.subdirectory
        if subdir is None:
            frag = resolution.repo_url.partition("#")[2]
            subdir = next(
                (p[len("subdirectory="):] for p in frag.split("&")
                 if p.startswith("subdirectory=")),
                None,
            )
        repo_key = _repo_key(resolution.repo_url, directive.ref)
        checkout = self._checkout_dir / repo_key
        with self._checkout_lock(repo_key):
            self._clone(resolution.repo_url, directive.ref, sha, checkout, key)
            cwd = checkout / subdir if subdir else checkout
            for command in directive.commands:
                rc, out = _stream(command, key, cwd=cwd, shell=True,
                                  env=self._cache_env(), registry=self._procs)
                if rc != 0:
                    raise BuildError(
                        f"pre-build command for {key} failed (exit {rc}): {command}\n{_tail(out)}"
                    )
            return self._pip_wheel([str(cwd)], out_dir, key, label=key)

    def _pip_wheel(self, spec: list[str], out_dir: Path, key: str, *, label: str) -> Path:
        rc, out = _stream(
            # --ignore-requires-python for the same reason as the probe: we build and
            # advertise the wheel, and let the client decide whether its Requires-Python
            # fits (a mismatch is a PyPI fallback, not our error).
            [self._python, "-m", "pip", "wheel", "--no-deps", "--ignore-requires-python",
             "--cache-dir", str(self._pip_cache), *spec, "-w", str(out_dir)],
            key, env=self._cache_env(), registry=self._procs,
        )
        if rc != 0:
            raise BuildError(f"pip wheel failed for {label} (exit {rc}):\n{_tail(out)}")
        built = sorted(out_dir.glob("*.whl"), key=lambda p: p.stat().st_mtime)
        if not built:
            raise BuildError(f"pip wheel produced no wheel for {label}")
        return built[-1]

    def _prebuild(
        self, directive: SourceDirective, sha: str, out_dir: Path, key: str
    ) -> Path:
        assert directive.repo and directive.wheel
        repo_key = _repo_key(directive.repo, directive.ref)
        checkout = self._checkout_dir / repo_key
        with self._checkout_lock(repo_key):
            self._clone(directive.repo, directive.ref, sha, checkout, key)
            cwd = checkout / directive.subdirectory if directive.subdirectory else checkout
            for command in directive.commands:
                rc, out = _stream(command, key, cwd=cwd, shell=True,
                                  env=self._cache_env(), registry=self._procs)
                if rc != 0:
                    raise BuildError(
                        f"prebuild command for {key} failed (exit {rc}): {command}\n{_tail(out)}"
                    )
            produced = sorted(checkout.glob(directive.wheel))
            if not produced:
                raise BuildError(
                    f"prebuild for {key}: no wheel matched {directive.wheel!r} under {checkout}"
                )
            built = max(produced, key=lambda p: p.stat().st_mtime)
            dest = out_dir / built.name
            shutil.copyfile(built, dest)
            return dest

    def _clone(self, repo: str, ref: str | None, sha: str, dest: Path, key: str) -> None:
        bare = repo.partition("#")[0]
        reg = self._procs
        if not (dest / ".git").is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Recurse submodules like pip's VCS path does — a checkout missing them is
            # simply incomplete, and we build from it. --also-filter-submodules keeps
            # them blobless too (without it grpc's boringssl/protobuf/abseil come down
            # at full history).
            rc, out = _stream(
                ["git", "clone", "--filter=blob:none", "--also-filter-submodules",
                 "--recurse-submodules", bare, str(dest)],
                key, registry=reg,
            )
            if rc != 0:
                raise BuildError(f"git clone of {bare} failed (exit {rc}):\n{_tail(out)}")
        target = sha or ref
        if target:
            rc, out = _stream(["git", "-C", str(dest), "checkout", "--detach", target], key, registry=reg)
            if rc != 0:
                # a blobless clone may lack the commit if HEAD moved; fetch then retry
                _stream(["git", "-C", str(dest), "fetch", "--filter=blob:none", "origin", target], key, registry=reg)
                rc, out = _stream(["git", "-C", str(dest), "checkout", "--detach", target], key, registry=reg)
                if rc != 0:
                    raise BuildError(f"git checkout {target} failed for {bare}:\n{_tail(out)}")
        # The clone recursed for the *default* HEAD, so re-sync submodules to whatever
        # the checked-out commit points at. A no-op for repos without submodules.
        rc, out = _stream(
            ["git", "-C", str(dest), "submodule", "update", "--init", "--recursive"],
            key, registry=reg,
        )
        if rc != 0:
            raise BuildError(f"git submodule update failed for {bare} (exit {rc}):\n{_tail(out)}")

    def _checkout_lock(self, repo_key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._checkout_locks.get(repo_key)
            if lock is None:
                lock = threading.Lock()
                self._checkout_locks[repo_key] = lock
            return lock


# --------------------------------------------------------------------------- #
# request-independent build dispatch
# --------------------------------------------------------------------------- #
class BuildManager:
    """Deduplicate and background builds so an abandoned request never cancels one.

    A build for a given ``(project, sha)`` runs on a shared thread pool. Callers
    attach to the in-flight :class:`~concurrent.futures.Future`; the pool thread
    runs to completion regardless of whether any caller is still waiting, writing
    the wheel into the SHA cache for the next request. ``max_workers`` bounds
    concurrent compiles.
    """

    def __init__(self, builder: Builder, *, max_workers: int = 4) -> None:
        self._builder = builder
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="gwi-build")
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], Future] = {}

    def build(self, resolution: HeadResolution, sha: str, timeout: float = _BUILD_TIMEOUT) -> Path:
        key = (resolution.project, sha)
        with self._lock:
            fut = self._jobs.get(key)
            if fut is None:
                fut = self._pool.submit(self._run, resolution, sha, key)
                self._jobs[key] = fut
        # Wait on the pool-run build. If this caller goes away, the build keeps
        # running on the pool thread and its result lands in the SHA cache. A
        # ``TimeoutError`` here (hung build) is turned into a problem by the caller.
        return fut.result(timeout=timeout)

    def _run(self, resolution: HeadResolution, sha: str, key: tuple[str, str]) -> Path:
        try:
            return self._builder.build_for_sha(resolution, sha)
        finally:
            # Drop the in-flight entry once done; a later request for the same key
            # gets a cache hit (or, if still running, attaches to this future).
            with self._lock:
                self._jobs.pop(key, None)

    def shutdown(self) -> None:  # pragma: no cover - lifecycle helper
        self._pool.shutdown(wait=False)
