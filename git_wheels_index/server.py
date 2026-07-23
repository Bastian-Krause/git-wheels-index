"""The passive package index (a Flask app).

The index is responsible for a git dev wheel for **every** package. pip/uv may reach
PyPI in only two cases: a version cap excludes our (successfully served) wheel, or the
package is ``type="pypi"`` (a `404`). Any other outcome is an error — and because pip
treats *any* non-200 from an extra index as "skip it, use PyPI", an error must **not**
be answered at all: the handler records a :class:`~git_wheels_index.problems.Problems`
entry and **stalls** (holds the request open) until the wrapper terminates pip/uv.

Every listed file advertises PEP 658 **core metadata**, so pip/uv read a package's
``Requires-Dist`` from a cheap ``.metadata`` sidecar instead of downloading (building)
the wheel just to inspect it. A plain-git wheel is therefore built only when the client
actually selects it — candidates the resolver evaluates then discards are never built.

Routes:
* ``GET /simple/<project>/`` — resolve the repo and list one file
  ``foo-<base+gsha>-<tag>.whl`` (marked ``core-metadata``). An already-built wheel answers
  with no work at all; otherwise plain git gets its version from a cheap **probe**, and a
  **recipe** source (``prebuild`` / ``git``+``commands``) — or a package whose probe
  overruns its budget — builds here. See :func:`_describe`. ``type="pypi"`` → `404`.
* ``GET|HEAD /build/<project>/<sha>/<file>`` — build (once, SHA-cached) and serve the wheel.
* ``GET|HEAD /build/<project>/<sha>/<file>.metadata`` — the wheel's core metadata: for plain
  git from the probe (no build), for a recipe source read from the built wheel (cache hit).
"""

from __future__ import annotations

import logging
import threading

from flask import Flask, Response, abort, jsonify, request
from markupsafe import escape
from packaging.utils import canonicalize_name
from werkzeug.serving import BaseWSGIServer, make_server

from .builder import BuildManager, Builder, ProbeTooSlow, _wheel_name, read_wheel_metadata
from .exceptions import GitWheelsIndexError
from .problems import Problems

logger = logging.getLogger("git_wheels_index")

_JSON_CT = "application/vnd.pypi.simple.v1+json"


def run_in_thread(app: Flask, host: str = "127.0.0.1") -> tuple[BaseWSGIServer, int]:
    """Serve ``app`` on an OS-picked free port in a background daemon thread.

    ``daemon_threads`` so a *stalled* request handler (holding pip on an error) never
    blocks process exit — the wrapper kills pip, then the whole process exits.
    """
    server = make_server(host, 0, app, threaded=True)
    server.daemon_threads = True
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, name="gwi-index", daemon=True)
    thread.start()
    return server, port


def create_app(builder: Builder, manager: BuildManager, problems: Problems) -> Flask:
    app = Flask(__name__)

    @app.after_request
    def _no_store(resp: Response) -> Response:
        # dev-branch HEAD builds — never let a client cache them.
        resp.headers["Cache-Control"] = "no-store, no-cache, max-age=0"
        return resp

    @app.get("/")
    def index() -> Response:
        return Response("git-wheels-index: OK\n", mimetype="text/plain")

    @app.get("/simple/")
    def simple_root() -> Response:
        return Response(
            "<!DOCTYPE html><html><body><!-- git-wheels-index: built on demand -->"
            "</body></html>",
            mimetype="text/html",
        )

    @app.get("/simple/<project>/")
    def simple(project: str) -> Response:
        canonical = canonicalize_name(project)
        try:
            resolution = builder.resolve(project)          # X1 / X2 / X3
        except GitWheelsIndexError as exc:
            return _fail(problems, canonical, str(exc))
        except Exception as exc:
            return _fail(problems, canonical, f"internal error: {exc}", trace=True)

        if resolution is None:
            abort(404)  # E1: type="pypi" — the only intended "use PyPI" response

        try:
            filename, meta = _describe(builder, manager, resolution, canonical)  # X4
        except GitWheelsIndexError as exc:
            return _fail(problems, canonical, str(exc))
        except TimeoutError:
            return _fail(problems, canonical, "build timed out")           # X6
        except Exception as exc:
            return _fail(problems, canonical, f"internal error: {exc}", trace=True)    # X7

        file_url = f"/build/{canonical}/{resolution.sha}/{filename}"
        requires_python = _requires_python(meta)
        if _wants_json():
            return _json_listing(canonical, filename, file_url, requires_python)
        return _html_listing(canonical, filename, file_url, requires_python)

    @app.get("/build/<project>/<sha>/<path:filename>")
    def build_get(project: str, sha: str, filename: str) -> Response:
        if filename.endswith(".metadata"):
            return _serve_metadata(builder, manager, problems, project, sha, filename, head=False)
        return _serve_build(builder, manager, problems, project, sha, head=False)

    # Flask routes GET; register HEAD explicitly so pip's HEAD probe is answered.
    @app.route("/build/<project>/<sha>/<path:filename>", methods=["HEAD"])
    def build_head(project: str, sha: str, filename: str) -> Response:
        if filename.endswith(".metadata"):
            return _serve_metadata(builder, manager, problems, project, sha, filename, head=True)
        return _serve_build(builder, manager, problems, project, sha, head=True)

    return app


def _fail(problems: Problems, project: str, reason: str, *, trace: bool = False) -> Response:
    """Record the failure and **stall** — never hand pip a fallback-able response.

    Once teardown has begun the failure is our own doing (we killed the build), so it is
    neither recorded nor logged: dozens of such messages, each carrying the killed
    process's output or a traceback, would otherwise bury the one real error.
    """
    if problems.record(project, reason):
        logger.warning("git-wheels-index: %s: %s", project, reason, exc_info=trace)
    problems.stall()  # blocks until the wrapper has terminated pip/uv
    return Response("git-wheels-index build error\n", status=503)


def _serve_build(
    builder: Builder, manager: BuildManager, problems: Problems,
    project: str, sha: str, *, head: bool,
) -> Response:
    canonical = canonicalize_name(project)
    try:
        resolution = builder.resolve(project, sha)
    except GitWheelsIndexError as exc:
        return _fail(problems, canonical, str(exc))
    except Exception as exc:
        return _fail(problems, canonical, f"internal error: {exc}", trace=True)

    if resolution is None:
        abort(404)  # E1

    try:
        wheel = manager.build(resolution, sha)
    except GitWheelsIndexError as exc:
        return _fail(problems, canonical, str(exc))          # X5
    except TimeoutError:
        return _fail(problems, canonical, "build timed out")  # X6
    except Exception as exc:
        return _fail(problems, canonical, f"internal error: {exc}", trace=True)  # X7

    try:
        data = b"" if head else wheel.read_bytes()
        size = wheel.stat().st_size
    except OSError as exc:
        return _fail(problems, canonical, f"built wheel unreadable: {exc}")  # X7

    resp = Response(data, mimetype="application/octet-stream")
    resp.headers["Content-Length"] = str(size)
    resp.headers["Content-Disposition"] = f"attachment; filename={wheel.name}"
    return resp


def _describe(
    builder: Builder, manager: BuildManager, resolution, canonical: str,
) -> tuple[str, bytes]:
    """The advertised wheel filename and its core metadata.

    Three sources, cheapest first: an **already-built wheel** (no probe, no build — what
    makes a re-run, or a ``uv pip compile`` followed by an install, nearly free); a
    **metadata probe** for plain git; otherwise a **build** — for recipe sources, and for
    packages whose probe overran its budget (:class:`ProbeTooSlow`), whose metadata is
    not cheap to obtain (grpcio cythonizes during ``egg_info``). Building never costs
    more than the abandoned probe, since it does that same work anyway.
    """
    wheel = builder.cached_wheel(canonical, resolution.sha)
    if wheel is None and not resolution.from_recipe:
        try:
            version, meta = builder.probe(resolution)
            return f"{_wheel_name(canonical)}-{version}-{builder.wheel_tag}.whl", meta
        except ProbeTooSlow as exc:
            logger.info("%s: %s — building the wheel instead", canonical, exc)
    if wheel is None:
        wheel = manager.build(resolution, resolution.sha)
    return wheel.name, read_wheel_metadata(wheel)


def _serve_metadata(
    builder: Builder, manager: BuildManager, problems: Problems,
    project: str, sha: str, filename: str, *, head: bool,
) -> Response:
    """Serve a wheel's PEP 658 core metadata (see :func:`_describe` for the source)."""
    canonical = canonicalize_name(project)
    try:
        resolution = builder.resolve(project, sha)
    except GitWheelsIndexError as exc:
        return _fail(problems, canonical, str(exc))
    except Exception as exc:
        return _fail(problems, canonical, f"internal error: {exc}", trace=True)

    if resolution is None:
        abort(404)  # E1

    try:
        _, meta = _describe(builder, manager, resolution, canonical)
    except GitWheelsIndexError as exc:
        return _fail(problems, canonical, str(exc))          # X4 / X5
    except TimeoutError:
        return _fail(problems, canonical, "build timed out")  # X6
    except Exception as exc:
        return _fail(problems, canonical, f"internal error: {exc}", trace=True)  # X7

    # Self-check: the metadata's Name/Version must match the advertised wheel. A
    # parseable-but-mismatched .metadata (a 200 the stall can't catch) makes the client
    # silently fall back to PyPI, so a mismatch here is our bug — abort loudly instead.
    want_version = _wheel_version(filename[: -len(".metadata")])
    if not _metadata_matches(meta, canonical, want_version):
        return _fail(problems, canonical,
                     f"internal error: served metadata does not match {filename}")

    resp = Response(b"" if head else meta, mimetype="text/plain")
    resp.headers["Content-Length"] = str(len(meta))
    return resp


def _wheel_version(wheel_filename: str) -> str:
    """The version field of a ``{name}-{version}-{tags…}.whl`` name (neither contains ``-``)."""
    stem = wheel_filename[: -len(".whl")] if wheel_filename.endswith(".whl") else wheel_filename
    parts = stem.split("-")
    return parts[1] if len(parts) >= 2 else ""


def _metadata_matches(meta: bytes, canonical: str, want_version: str) -> bool:
    name = version = None
    for line in meta.decode("utf-8", "replace").splitlines():
        low = line.lower()
        if name is None and low.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif version is None and low.startswith("version:"):
            version = line.split(":", 1)[1].strip()
    return (name is not None and canonicalize_name(name) == canonical
            and version == want_version)


def _wants_json() -> bool:
    return _JSON_CT in request.headers.get("Accept", "")


def _requires_python(meta: bytes) -> str:
    """The wheel's ``Requires-Python``, advertised so the client can skip us itself.

    A dev branch that doesn't support the running interpreter is the *same class* as a
    version cap: the user's constraint excludes our wheel, and pip/uv should quietly use
    PyPI's release. Advertising an empty value would instead claim the wheel fits any
    interpreter and turn the mismatch into a hard install error.
    """
    for line in meta.decode("utf-8", "replace").splitlines():
        if not line.strip():
            break                      # end of headers
        if line[:16].lower() == "requires-python:":
            return line.split(":", 1)[1].strip()
    return ""


def _json_listing(project: str, filename: str, file_url: str, requires_python: str) -> Response:
    # core-metadata: the wheel's metadata is served at <url>.metadata (PEP 658/691), so the
    # client resolves without downloading (building) the wheel.
    payload = {
        "meta": {"api-version": "1.0"},
        "name": project,
        "files": [{"filename": filename, "url": file_url, "hashes": {},
                   "requires-python": requires_python, "core-metadata": True}],
    }
    resp = jsonify(payload)
    resp.mimetype = _JSON_CT
    return resp


def _html_listing(project: str, filename: str, file_url: str, requires_python: str) -> Response:
    attr = f' data-requires-python="{escape(requires_python)}"' if requires_python else ""
    body = (
        "<!DOCTYPE html><html><head>"
        '<meta name="pypi:repository-version" content="1.0">'
        f"<title>Links for {project}</title></head><body>"
        f"<h1>Links for {project}</h1>"
        f'<a href="{file_url}" data-core-metadata="true"{attr}>{filename}</a><br>'
        "</body></html>"
    )
    return Response(body, mimetype="text/html")
