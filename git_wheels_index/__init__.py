"""git-wheels-index: a passive package index that serves git dev branches as wheels.

Instead of walking and installing a dependency graph itself, this tool stands up a
small PyPI-style index. For every package it can build from git, the index
advertises a single synthetic wheel built from that project's development branch;
``pip``/``uv`` then resolve normally against the *real* version constraints in the
graph and fetch our git wheel when it is not excluded by an upper bound (falling
back to PyPI's released wheels when it is).

The public entry point is :func:`git_wheels_index.cli.main`.
"""

__version__ = "0.1.0"
