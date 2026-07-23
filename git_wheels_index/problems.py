"""Shared failure channel between the index (server threads) and the wrapper.

The index is responsible for a git dev wheel for *every* package; the only ways
pip/uv may legitimately reach PyPI are a version cap excluding our (served) wheel
or an explicit ``type="pypi"``. Any other outcome is an error that must **not** be
answered with a fallback-able HTTP response — because pip treats any non-200 from
an extra index as "skip it, use PyPI", and no status code makes it abort.

So on an error a request handler ``record``s the problem and then ``stall``s
(blocks on ``teardown``), holding pip on the open connection. The wrapper polls
``snapshot`` every tick; on the first problem it terminates pip/uv *while it is
still stalled* (so it never received a fallback-able response), then ``release``s
the stalled handlers and tears the server down.
"""

from __future__ import annotations

import threading


class Problems:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, str] = {}
        self._frozen = False
        self._teardown = threading.Event()

    def record(self, project: str, reason: str) -> bool:
        """Record the first failure reason for a project; ``True`` if it was accepted.

        Rejected once :meth:`freeze` has been called: builds the wrapper kills while
        tearing the run down are our own doing, not genuine problems — the caller uses
        the return value to stay quiet about them (they'd bury the real error).
        """
        with self._lock:
            if self._frozen:
                return False
            self._items.setdefault(project, reason)
            return True

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._items)

    def freeze(self) -> None:
        """Stop recording new problems — the wrapper is tearing down and about to
        kill in-flight builds, whose induced failures must not enter the summary."""
        with self._lock:
            self._frozen = True

    def stall(self) -> None:
        """Hold the request until the wrapper tears down (never answer pip)."""
        self._teardown.wait()

    def release(self) -> None:
        """Unblock stalled handlers — called by the wrapper *after* pip is dead."""
        self._teardown.set()
