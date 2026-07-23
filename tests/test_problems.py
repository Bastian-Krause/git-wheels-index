"""The Problems failure channel: record/snapshot, stall/release, and the
teardown freeze (killed-build noise must not pollute the summary)."""

from __future__ import annotations

from git_wheels_index.problems import Problems


def test_record_and_snapshot_keeps_first_reason():
    p = Problems()
    p.record("foo", "first")
    p.record("foo", "second")     # later reasons for the same project are noise
    assert p.snapshot() == {"foo": "first"}


def test_record_is_frozen_after_freeze():
    # once frozen, builds the wrapper kills during teardown must not be recorded
    p = Problems()
    assert p.record("real", "genuine failure") is True
    p.freeze()
    assert p.record("collateral", "killed during teardown") is False
    assert p.snapshot() == {"real": "genuine failure"}


def test_release_does_not_freeze_recording():
    # release only unblocks stalls; a handler recording its problem then must still land
    p = Problems()
    p.release()
    p.record("foo", "boom")
    assert p.snapshot() == {"foo": "boom"}


def test_release_unblocks_stall():
    p = Problems()
    p.release()
    p.stall()                     # returns immediately (teardown already set)
