> [!NOTE]
> Vibe-coded with Claude Opus 4.8.

# git-wheels-index

pip/uv wrapper adding on demand built **development wheels** (from each package's git default branch) to the used package index.


> [!CAUTION]
> Do not use in production. Releases exist for a reason. Expect fallout.

# Intended use case
When combined with a proper test suite, git-wheels-index is useful to spot upcoming incompatible changes in your dependencies early. It is also a good way to check dependency compatibility for new Python versions.

## How it works

The `with-git-wheels-index` wrapper adds a passive, PyPI-style package **index** that serves each project's **git development
branch** as a wheel. Makes uv/pip pull a
freshly built **git** wheel for every package a version constraint doesn't forbid.

For each package the index advertises a single synthetic wheel whose version is the package's current
base version plus the commit (e.g. `9.0.0+gabc1234`) making it win over the latest release on PyPI (when uncapped).

## Design decisions 

- Implemented as a wrapper:
  - Prevents incompatible pip/uv options from being used.
  - Build errors are fatal.
  - Single log stream: pip/uv output combined with build logs.
- Build backends come from PyPI.
- Wheels are interpreter- and platform-specific. Cross-interpreter serving is out of scope.
- Built wheels are cached per commit, and a cached wheel is served without any further work.
  Packages whose metadata is not cheap to read (grpcio cythonizes while generating it) are detected
  and built once instead — so the `uv pip compile` preview also warms the cache for the install.
- Exotic builds need a recipe: packages needing unpublished build tools or a
  codegen step (protoc, Cython, ...) need a per-package `prebuild` entry.

## Workflow 

```console
$ pipx install git+https://github.com/Bastian-Krause/git -wheels-index

# optional: preview dependency graph (useful for fast iteration to create the required configuration, see below)
$ echo "some-package" | with-git-wheels-index -- uv pip compile -

# actual development wheel installation
$ with-git-wheels-index -- pip install some-package
# or
$ with-git-wheels-index -- uv pip install some-package
```

## Usage

Everything after `--` is the command to run with the development package index active. Options:

| Option | Meaning |
|---|---|
| `--config PATH` | TOML config (repo overrides + per-package sources); optional |
| `--python X.Y` \| PATH | interpreter to build wheels for (default `python3` on PATH) — must match what the command installs into |
| `--cache-dir DIR` | wheel cache root, keyed by commit + interpreter (default `./.git-wheels-index`) |
| `-v` / `-vv` | raise log verbosity (build output is always streamed) |

## Configuration (optional)

TOML at `--config PATH`, else `./git-wheels-index.toml` or
`~/.config/git-wheels-index/config.toml`. Use it to correct a package's source repo
(`[repo_overrides]`) or set a per-package source:

- `git` — build from a pinned ref (`url`, `ref`), and/or run pre-build `commands` in the
  checkout before the tool builds the wheel (codegen, cythonize, install headers, ...);
- `pypi` — use release wheel from PyPI;
- `prebuild` — clone + your `commands` **produce** the wheel (globbed via `wheel`), for
  packages whose build itself is non-standard (bazel, protoc, …).

Recipe checkouts (`prebuild` / `git` + `commands`) are cloned **fresh into a temp dir each run** and
removed when the command exits — so a build never inherits a previous run's tree.

See [`config.example.toml`](config.example.toml).
