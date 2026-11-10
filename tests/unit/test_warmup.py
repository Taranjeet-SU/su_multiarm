"""Startup warm-up state (``runtime/warmup.py``): not_run -> running -> done | failed, a run past its
bound reported ``timed_out`` (still not ready), a late finish ``done`` + ``late``, readiness problems
that name the error class but never its message (``/ready`` is public)."""

from __future__ import annotations

import threading

import pytest

from su_multi_arm.core.errors import BackendError
from su_multi_arm.runtime.warmup import WarmUp


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_not_run_then_done_with_the_report():
    clock = Clock()
    warm = WarmUp(10, clock=clock)
    assert warm.state() == {"status": "not_run"} and warm.problem() == "warm-up not_run"

    def work():
        assert warm.state()["status"] == "running" and warm.problem() == "warm-up running"
        clock.now += 2
        return {"units": 3}

    state = warm.run(work)
    assert state == {"status": "done", "ms": 2000, "units": 3} and warm.problem() is None


def test_a_failure_is_a_problem_naming_the_class_not_the_message():
    warm = WarmUp(10, clock=Clock())

    def work():
        raise BackendError("sqlite", "member", "unit at /private/path/state is gone")

    state = warm.run(work)
    assert state["status"] == "failed" and state["error"] == "BackendError" and state["adapter"] == "sqlite" and state["operation"] == "member"
    assert "private" not in repr(state)
    assert warm.problem() == "warm-up failed (BackendError in sqlite.member; see the server log)"
    with pytest.raises(RuntimeError, match="already started"):
        warm.run(dict)  # never re-run into success


def test_a_run_past_its_bound_is_timed_out_until_it_ends_late():
    clock = Clock()
    warm = WarmUp(5, clock=clock)
    entered, finish = threading.Event(), threading.Event()

    def work():
        entered.set()
        finish.wait(10)
        return {}

    thread = warm.start(work, name="test-warmup")
    assert entered.wait(5)
    assert warm.state()["status"] == "running"
    clock.now += 6
    state = warm.state()
    assert state["status"] == "timed_out" and state["seconds"] == 6.0
    assert warm.problem() == "warm-up timed out after 6.0 s (bound 5 s; still running)"
    finish.set()
    thread.join(5)
    assert warm.state()["status"] == "done" and warm.state()["late"] is True and warm.problem() is None


def test_start_reports_running_before_the_thread_runs():
    warm = WarmUp(5)
    gate = threading.Event()
    thread = warm.start(lambda: gate.wait(5) and {}, name="test-warmup")
    assert warm.state()["status"] == "running"
    gate.set()
    thread.join(5)
    assert warm.state()["status"] == "done"


def test_the_bound_must_be_positive():
    with pytest.raises(ValueError):
        WarmUp(0)
