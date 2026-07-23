"""Allow ``python -m git_wheels_index``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
