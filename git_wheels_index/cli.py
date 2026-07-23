"""Command-line entry point: ``with-git-wheels-index -- COMMAND …``.

The tool runs as a **wrapper**: it starts the index on a free local port, runs the
given command with the index injected into its environment (so ``pip``/``uv``
resolve the graph from git dev branches), tears the index down when the command
exits, and exits with the command's exit code. It owns the index's lifetime and
port itself and writes nothing to disk beyond the SHA-keyed build cache.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .builder import BuildManager, Builder
from .config import DEFAULT_CACHE_DIR, load_config
from .exceptions import GitWheelsIndexError
from .problems import Problems
from .server import create_app, run_in_thread

logger = logging.getLogger("git_wheels_index")

# A socket timeout, and we hold the connection open while building — so it must exceed
# the longest build (builder._BUILD_TIMEOUT), or pip gives up first and uses PyPI.
_HTTP_TIMEOUT = 7200
_BUILD_WORKERS = 4            # concurrent wheel builds (the build semaphore)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="with-git-wheels-index",
        usage="%(prog)s [options] -- COMMAND [ARGS ...]",
        description="Run COMMAND with a git-wheels index injected, so pip/uv resolve "
        "the dependency graph from git development branches.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="explicit TOML config file")
    parser.add_argument(
        "--python", default=None,
        help="interpreter to build wheels for (path or X.Y); default = python3 on PATH. "
        "Must match the interpreter COMMAND installs into.",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=Path(DEFAULT_CACHE_DIR),
        help="checkout + wheel cache root (default ./.git-wheels-index)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v/-vv raise log verbosity (build output is always streamed)",
    )
    return parser


def _configure_logging(verbosity: int) -> None:
    level = logging.DEBUG if verbosity >= 2 else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)


def _resolve_python(python: str | None) -> str:
    # Default to python3 on PATH — NOT sys.executable, which under a pipx install is
    # the tool's own isolated interpreter, not the one COMMAND installs into.
    if not python:
        return shutil.which("python3") or sys.executable
    if Path(python).exists():
        return python
    return shutil.which(python) or shutil.which(f"python{python}") or python


def _is_pip_or_uv(command: list[str]) -> bool:
    if not command:
        return False
    exe = Path(command[0]).name
    return exe in {"pip", "pip3", "uv"} or (exe.startswith("python") and "pip" in command)


def _has_flag(command: list[str], opt: str) -> bool:
    """True if ``opt`` appears in ``command`` as ``opt`` or ``opt=value``."""
    return any(tok == opt or tok.startswith(opt + "=") for tok in command)


def _index_env(port: int, command: list[str]) -> tuple[dict[str, str], list[str]]:
    """Build the env to inject into the wrapped command, plus any competing options.

    ``manage`` sets one option *and* checks the same option for competition — the env
    var it would overwrite, and the pip/uv CLI flags that mean the same thing — so a new
    managed option can't be added without its conflict check coming along. ``inject``
    sets a benign var we don't contest (ours simply applies, no index/trust/cache
    ambiguity).
    """
    base = f"http://127.0.0.1:{port}/simple/"
    timeout = str(_HTTP_TIMEOUT)
    is_client = _is_pip_or_uv(command)
    env: dict[str, str] = {}
    conflicts: list[str] = []

    def manage(env_var: str, cli_opts: tuple[str, ...], value: str) -> None:
        if env_var in os.environ:
            conflicts.append(f"${env_var} is already set in the environment")
        if is_client and (hit := next((o for o in cli_opts if _has_flag(command, o)), None)):
            conflicts.append(f"the command passes {hit}")
        env[env_var] = value

    def inject(env_var: str, value: str) -> None:
        env[env_var] = value

    index_opts = ("-i", "--index-url", "--extra-index-url", "--no-index")
    manage("PIP_EXTRA_INDEX_URL", index_opts, base)
    manage("UV_EXTRA_INDEX_URL", (*index_opts, "--index", "--default-index"), base)
    manage("PIP_TRUSTED_HOST", ("--trusted-host",), "127.0.0.1")
    # allow plain-http localhost for uv (env-var name has varied across uv versions)
    manage("UV_INSECURE_HOST", ("--allow-insecure-host",), "127.0.0.1")
    # uv's default first-index strategy would never consult PyPI for a package we serve,
    # breaking the capped→PyPI fallback; unsafe-best-match considers both.
    manage("UV_INDEX_STRATEGY", ("--index-strategy",), "unsafe-best-match")
    manage("PIP_NO_CACHE_DIR", ("--no-cache-dir", "--cache-dir"), "1")
    manage("UV_NO_CACHE", ("--no-cache", "--cache-dir"), "1")

    # benign — set, but not contested (ours simply applies)
    inject("PIP_TIMEOUT", timeout)
    inject("UV_HTTP_TIMEOUT", timeout)
    # pip's self-version-check would otherwise query the index for `pip` and build it
    # from git for nothing (it only compares versions, never installs).
    inject("PIP_DISABLE_PIP_VERSION_CHECK", "1")

    # a shared flag (e.g. --no-index maps to both pip and uv index) reports once
    return env, list(dict.fromkeys(conflicts))


def _report_problems(problems: dict[str, str]) -> None:
    print(f"\nerror: git-wheels-index stopped the command — {len(problems)} package(s) could "
          "not be served from git (pip/uv was terminated before it could use PyPI):",
          file=sys.stderr)
    for name, reason in sorted(problems.items()):
        first = reason.strip().splitlines()[0] if reason.strip() else "failed"
        print(f"  - {name}: {first}", file=sys.stderr)


def _run(args: argparse.Namespace, command: list[str]) -> int:
    python = _resolve_python(args.python)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(args.config)  # ConfigError handled in main()
    problems = Problems()
    builder = Builder(config, args.cache_dir, python=python)
    manager = BuildManager(builder, max_workers=_BUILD_WORKERS)
    server, port = run_in_thread(create_app(builder, manager, problems))
    try:
        index_env, conflicts = _index_env(port, command)
        if conflicts:
            for c in conflicts:
                print(f"error: {c}", file=sys.stderr)
            print("error: the wrapper manages index selection, trusted/insecure host "
                  "and caching for the command; unset the above and let it manage them.",
                  file=sys.stderr)
            return 2
        logger.info("index serving at http://127.0.0.1:%d/  →  running: %s",
                    port, " ".join(command))
        try:
            proc = subprocess.Popen(command, env={**os.environ, **index_env})
        except FileNotFoundError:
            print(f"error: command not found: {command[0]}", file=sys.stderr)
            return 127
        try:
            # Poll the shared problems channel: the moment the index records a
            # failure it also stalls that request, so the command is stuck — we
            # terminate it here before it can fall back to PyPI.
            while proc.poll() is None:
                if problems.snapshot():
                    # Abort decided. Go quiet *now*, not at teardown: in-flight builds
                    # would otherwise keep streaming while the command is terminating
                    # (a killed `git checkout` alone can emit a line per file) and bury
                    # the failure we are about to report.
                    problems.freeze()
                    builder.silence()
                    break
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        recorded = problems.snapshot()
        if recorded:
            _report_problems(recorded)
            return 1
        return proc.returncode
    finally:
        problems.freeze()    # builds killed below are our own doing — keep them out of the summary
        builder.silence()    # …and keep their streamed output out of the log
        problems.release()   # unblock any stalled handlers (the command is already dead)
        server.shutdown()
        manager.shutdown()
        builder.close()      # kill in-flight builds, then drop the run-scoped checkouts


def _split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``[opts] -- cmd…`` on the first ``--``."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    our_args, command = _split_command(argv)

    parser = build_parser()
    args = parser.parse_args(our_args)   # handles --version / --help
    _configure_logging(args.verbose)

    if not command:
        parser.error("no command to run — put it after `--`, e.g. "
                     "`with-git-wheels-index -- pip install .`")

    try:
        return _run(args, command)
    except GitWheelsIndexError as exc:  # ConfigError, BuildError, … all land here
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
