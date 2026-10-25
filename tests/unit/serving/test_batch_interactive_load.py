"""InteractiveLoad: in-flight interactive requests per model."""

from __future__ import annotations

import pytest

from serving.batch.interactive_load import INTERACTIVE_LOAD, InteractiveLoad


def test_counts_while_tracked_and_per_model():
    load = InteractiveLoad()

    with load.track("a"):
        with load.track("a"), load.track("b"):
            assert load.in_flight("a") == 2
            assert load.in_flight("b") == 1
        assert load.in_flight("a") == 1
        assert load.in_flight("b") == 0

    assert load.in_flight("a") == 0
    assert load.in_flight("never-seen") == 0


def test_an_exception_still_stops_the_count():
    load = InteractiveLoad()

    with pytest.raises(RuntimeError), load.track("a"):
        assert load.in_flight("a") == 1
        raise RuntimeError("boom")

    assert load.in_flight("a") == 0


def test_an_unknown_tracker_can_move_to_the_concrete_model():
    load = InteractiveLoad()
    tracker = load.track(None)

    with tracker:
        assert load.in_flight("a") == 0
        tracker.retarget("a")
        assert load.in_flight("a") == 1

        tracker.retarget("b")
        assert load.in_flight("a") == 0
        assert load.in_flight("b") == 1

    assert load.in_flight("a") == 0
    assert load.in_flight("b") == 0


def test_an_exception_after_retargeting_stops_the_concrete_count():
    load = InteractiveLoad()

    with pytest.raises(RuntimeError), load.track(None) as tracker:
        tracker.retarget("a")
        assert load.in_flight("a") == 1
        raise RuntimeError("boom")

    assert load.in_flight("a") == 0


def test_never_negative():
    load = InteractiveLoad()
    outer = load.track("a")
    inner = load.track("a")
    outer.__enter__()
    inner.__enter__()
    load._in_flight.clear()  # an exit with nothing counted must not go below zero

    inner.__exit__(None, None, None)
    outer.__exit__(None, None, None)

    assert load.in_flight("a") == 0


def test_process_wide_instance():
    assert isinstance(INTERACTIVE_LOAD, InteractiveLoad)
