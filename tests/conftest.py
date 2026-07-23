"""Shared test fixtures. No test hits the network or runs a real build."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


def make_wheel(
    dest_dir: Path,
    name: str = "foo",
    version: str = "1.2.3.dev4+gold",
    tag: str = "py3-none-any",
) -> Path:
    """Write a minimal but valid wheel and return its path.

    Used by the re-versioning tests: a real ``.dist-info`` with ``METADATA``,
    ``WHEEL`` and ``RECORD``, plus one package file.
    """
    di = f"{name}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        "Summary: test\n"
    ).encode("utf-8")
    wheel_meta = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        f"Tag: {tag}\n"
    ).encode("utf-8")
    files: dict[str, bytes] = {
        f"{name}/__init__.py": b"# package\n",
        f"{di}/METADATA": metadata,
        f"{di}/WHEEL": wheel_meta,
        f"{di}/RECORD": b"placeholder,,\n",
    }

    path = dest_dir / f"{name}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, data in files.items():
            z.writestr(arcname, data)
    return path


@pytest.fixture
def wheel_factory(tmp_path):
    def _make(**kwargs):
        return make_wheel(tmp_path, **kwargs)

    return _make
