"""Tests for ``advance.py``'s deadline rule: what a lane with NO acceptable sidecar becomes.

The controller found this defect with an independent adversarial test: ``advance()`` timed out on
a lane that had no sidecar, correctly *reported* it in its ``timed_out`` list, and then never
called ``led.settle()`` — so the ledger sat at ``dispatched`` forever while no unit was running
under it and no artifact was ever going to arrive. That is the v0.1 failure shape (a ledger frozen
in a non-terminal state), and it misses the frozen criterion that every lane ends ACCEPTED or
parked ``blocked``/``needs_review``.

The rule these tests freeze is decided by the unit's **lifetime**, and by nothing else:

    active    -> leave ``dispatched``, report in ``timed_out``, NEVER settle
                 (a genuinely in-flight worker may still write its evidence)
    finished  -> settle(False, "unit exited with no acceptable sidecar") -> FAILED
    absent    -> settle(False, "no unit and no acceptable sidecar")      -> FAILED

and three consecutive failures of either settling kind park the lane at BLOCKED through the
ledger's circuit breaker, which ``advance`` never re-dispatchs past.

No live systemd is required: ``advance._unit_active`` and ``advance._unit_finished`` are
monkeypatched to simulate each lifetime, so every test drives the shipped decision path on any
host (including the "no unit record at all" answer ``None``, which is the exact shape the defect
was reported with).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import advance
from ledger import BLOCKED, COMPLETED, DISPATCHED, FAILED, IllegalTransition, Ledger

#: The lane's own test command, run through the same interpreter as the suite.
TEST_PASS = [sys.executable, "-c", "raise SystemExit(0)"]

#: How long a single ``advance`` call may take in these tests before we call it unbounded.
BOUNDED_SECONDS = 6.0


# --- fixtures ----------------------------------------------------------------


def _lane(tmp_path: Path, lane_id: str = "L3", *,
          failure_limit: int = 3) -> tuple[Ledger, Path]:
    """A real ledger plus a real lane with a real worktree — no mocks in the ledger path."""
    ledger = Ledger(tmp_path / "state.json", failure_limit=failure_limit)
    worktree = tmp_path / f"wt-{lane_id}"
    worktree.mkdir(parents=True, exist_ok=True)
    ledger.init_lane(lane_id, harness="cursor-agent", worktree=str(worktree),
                     test_cmd=list(TEST_PASS), model="cursor-grok-4.6")
    return ledger, worktree


def _lifetime(monkeypatch: pytest.MonkeyPatch, *, active: bool | None,
              finished: bool | None) -> None:
    """Pin what systemd would answer about the lane's unit; ``None`` is its "no record" answer."""
    monkeypatch.setattr(advance, "_unit_active", lambda _lane_id: active)
    monkeypatch.setattr(advance, "_unit_finished", lambda _lane_id: finished)


def _sidecar(worktree: Path, dispatch_id: str, *, lane_id: str = "L3",
             status: str = "success") -> Path:
    """Write a worker's evidence file the way a worker would: atomically, at the exact path."""
    directory = worktree / advance.SIDECAR_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "task_id": lane_id, "dispatch_id": dispatch_id,
               "status": status, "harness": "cursor-agent", "model": "cursor-grok-4.6",
               "exit_code": 0, "evidence": {"files": [], "waiver": "fixture has no artifact files"}}
    target = directory / f"{advance.SIDECAR_PREFIX}{dispatch_id}{advance.SIDECAR_SUFFIX}"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return target


def _advance(ledger: Ledger, *, timeout: float = 0.5, poll: float = 0.05,
             lines: list[str] | None = None) -> tuple[dict, float]:
    """Run ``advance`` and time it, so boundedness is asserted in the same breath as the verdict."""
    started = time.monotonic()
    summary = advance.advance(ledger.path, timeout=timeout, poll=poll,
                              emit=lines.append if lines is not None else (lambda _line: None))
    return summary, time.monotonic() - started


# --- 1. the regression: an ABSENT unit with no sidecar is never left dispatched ---------------


def test_absent_unit_and_no_sidecar_is_never_left_dispatched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported defect: no unit at all, no sidecar — the lane must not stay in limbo."""
    ledger, _worktree = _lane(tmp_path)
    dispatch_id = ledger.dispatch("L3")
    _lifetime(monkeypatch, active=None, finished=None)          # nothing is running, ever
    lines: list[str] = []

    summary, elapsed = _advance(ledger, lines=lines)

    lane = ledger.lane("L3")
    assert lane["status"] != DISPATCHED, "a lane with no unit and no artifact is not in flight"
    assert lane["status"] in (FAILED, BLOCKED)
    assert lane["needs_review_reason"] == "no unit and no acceptable sidecar"
    assert lane["consecutive_failures"] == 1, "the failure streak must be recorded on the lane"
    assert COMPLETED not in {entry["to"] for entry in lane["history"]}, "never an acceptance"
    assert all(entry["event"] != "success" for entry in lane["history"])
    assert lane["evidence"]["dispatch_id"] == dispatch_id
    assert lane["evidence"]["rejected_because"] == "no unit and no acceptable sidecar"
    assert summary["accepted"] == []
    assert "L3" in summary["needs_review"]
    assert summary["timed_out"] == ["L3"]
    assert summary["final"] in ({FAILED: 1}, {BLOCKED: 1})
    assert any("NEEDS_REVIEW" in line for line in lines)
    assert elapsed < BOUNDED_SECONDS, f"advance must return (took {elapsed:.1f}s)"


def test_known_unit_with_no_exit_status_and_no_sidecar_settles_in_loop(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit record exists but nothing is running under it: same verdict, decided in the loop."""
    ledger, _worktree = _lane(tmp_path)
    ledger.dispatch("L3")
    _lifetime(monkeypatch, active=False, finished=False)

    summary, elapsed = _advance(ledger)

    lane = ledger.lane("L3")
    assert lane["status"] in (FAILED, BLOCKED)
    assert lane["needs_review_reason"] == "no unit and no acceptable sidecar"
    assert "L3" in summary["needs_review"]
    assert elapsed < BOUNDED_SECONDS


# --- 2. a FINISHED unit with no sidecar parks the lane, naming the missing sidecar -----------


def test_finished_unit_with_no_sidecar_parks_with_a_reason_naming_the_sidecar(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit exited (systemd has the exit) and left nothing behind: that is a failed dispatch."""
    ledger, _worktree = _lane(tmp_path)
    ledger.dispatch("L3")
    _lifetime(monkeypatch, active=False, finished=True)

    summary, elapsed = _advance(ledger)

    lane = ledger.lane("L3")
    assert lane["status"] in (FAILED, BLOCKED)
    assert lane["status"] != DISPATCHED
    assert "sidecar" in lane["needs_review_reason"]
    assert lane["needs_review_reason"] == "unit exited with no acceptable sidecar"
    assert lane["consecutive_failures"] == 1
    assert "L3" in summary["needs_review"]
    assert elapsed < BOUNDED_SECONDS


def test_finished_unit_is_settled_at_the_deadline_and_reported_stalled(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the deadline the exit is what decides: the lane is parked, and reported as stalled."""
    ledger, _worktree = _lane(tmp_path)
    ledger.dispatch("L3")
    _lifetime(monkeypatch, active=None, finished=True)           # observed only at the hard stop
    lines: list[str] = []

    summary, elapsed = _advance(ledger, timeout=0.4, lines=lines)

    lane = ledger.lane("L3")
    assert lane["status"] == FAILED
    assert lane["needs_review_reason"] == "unit exited with no acceptable sidecar"
    assert summary["stalled"] == ["L3"], "an exited unit is the stalled case"
    assert "L3" in summary["needs_review"]
    assert any("STALLED" in line for line in lines)
    assert elapsed < BOUNDED_SECONDS


# --- 3. the over-correction guard: an ACTIVE unit is never failed ----------------------------


def test_active_unit_with_no_sidecar_is_left_dispatched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live worker may still write its sidecar, so nothing may be settled about it."""
    ledger, _worktree = _lane(tmp_path)
    dispatch_id = ledger.dispatch("L3")
    _lifetime(monkeypatch, active=True, finished=False)
    lines: list[str] = []

    summary, elapsed = _advance(ledger, lines=lines)

    lane = ledger.lane("L3")
    assert lane["status"] == DISPATCHED, "a running worker must never be failed"
    assert lane["dispatch_id"] == dispatch_id
    assert lane["consecutive_failures"] == 0, "no invented failure may touch the breaker streak"
    assert lane["accepted"] == []
    assert lane["rejected"] == [], "not even a refused settlement may be recorded"
    assert "needs_review_reason" not in lane
    assert summary["needs_review"] == []
    assert summary["timed_out"] == ["L3"]
    assert summary["final"] == {DISPATCHED: 1}
    assert any("TIMED_OUT" in line for line in lines)
    assert elapsed < BOUNDED_SECONDS


def test_a_lane_left_in_flight_is_decided_once_its_unit_is_gone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kept-``dispatched`` outcome is not sticky: a later call decides the lane."""
    ledger, _worktree = _lane(tmp_path)
    ledger.dispatch("L3")
    _lifetime(monkeypatch, active=True, finished=False)

    first, _ = _advance(ledger, timeout=0.3)
    assert first["final"] == {DISPATCHED: 1}
    assert ledger.lane("L3")["status"] == DISPATCHED

    _lifetime(monkeypatch, active=False, finished=True)          # the worker has now exited
    second, _ = _advance(ledger, timeout=0.3)
    assert second["final"] == {FAILED: 1}
    assert ledger.lane("L3")["status"] == FAILED


# --- 4. repeat no-evidence timeouts reach BLOCKED, and advance never re-dispatches -----------


def test_three_no_evidence_timeouts_park_the_lane_at_blocked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three consecutive failures trip the ledger's breaker; the lane stays parked afterwards."""
    ledger, _worktree = _lane(tmp_path, failure_limit=3)
    _lifetime(monkeypatch, active=None, finished=None)
    lines: list[str] = []

    for attempt in (1, 2, 3):
        dispatch_id = ledger.dispatch("L3")               # the conductor's explicit re-dispatch
        summary, elapsed = _advance(ledger, timeout=0.3, lines=lines)
        lane = ledger.lane("L3")
        assert lane["status"] != DISPATCHED, "no-evidence timeout must never leave limbo"
        assert lane["consecutive_failures"] == attempt
        assert lane["attempt"] == attempt
        assert dispatch_id == f"L3-d{attempt}"
        assert "L3" in summary["needs_review"]
        assert elapsed < BOUNDED_SECONDS

    parked = ledger.lane("L3")
    assert parked["status"] == BLOCKED, "the breaker must stop the streak at failure_limit"
    assert parked["blocked_reason"].startswith("circuit breaker")
    # every settlement of this lane was a failure — the streak is what the breaker counted
    assert [entry["event"] for entry in parked["history"]
            if entry["event"] in ("success", "failure")] == ["failure"] * 3

    # ...and a parked lane is untouched: advance neither retries nor re-opens it.
    summary, elapsed = _advance(ledger, timeout=0.3)
    assert summary == {"accepted": [], "needs_review": [], "stalled": [], "timed_out": [],
                       "final": {BLOCKED: 1}}
    assert ledger.lane("L3")["attempt"] == 3, "advance must never dispatch on its own"
    with pytest.raises(IllegalTransition):
        ledger.dispatch("L3")
    assert elapsed < BOUNDED_SECONDS


# --- 5. bounded in every case, and evidence still wins over a dead unit ----------------------


@pytest.mark.parametrize("active,finished", [(True, False), (False, True), (False, False),
                                             (None, True), (None, None)])
def test_advance_returns_bounded_for_every_unit_lifetime(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool | None,
        finished: bool | None) -> None:
    """Whatever the lifetime says, the call returns a complete summary and does not hang."""
    ledger, _worktree = _lane(tmp_path)
    ledger.dispatch("L3")
    _lifetime(monkeypatch, active=active, finished=finished)

    summary, elapsed = _advance(ledger, timeout=0.3)

    assert set(summary) == {"accepted", "needs_review", "stalled", "timed_out", "final"}
    assert elapsed < BOUNDED_SECONDS, f"advance must be bounded (took {elapsed:.1f}s)"
    assert sum(summary["final"].values()) == 1


def test_a_dead_unit_with_a_valid_sidecar_is_still_accepted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not over-correct: acceptance is evidence, and a dead unit cannot veto it."""
    ledger, worktree = _lane(tmp_path)
    dispatch_id = ledger.dispatch("L3")
    _sidecar(worktree, dispatch_id)
    _lifetime(monkeypatch, active=None, finished=False)          # the unit is long gone

    summary, elapsed = _advance(ledger)

    lane = ledger.lane("L3")
    assert lane["status"] != FAILED
    assert len(lane["accepted"]) == 1
    assert summary["accepted"] == ["L3"]
    assert summary["needs_review"] == []
    assert elapsed < BOUNDED_SECONDS


# --- the classification itself ---------------------------------------------------------------


@pytest.mark.parametrize("active,finished,expected", [
    (True, False, "active"),
    (True, True, "active"),         # active wins: a live unit is never re-classified
    (False, True, "finished"),
    (None, True, "finished"),       # an exit systemd recorded wins over a missing record
    (False, False, "absent"),
    (None, False, "absent"),
    (None, None, "absent"),         # the reported defect's shape
])
def test_unit_lifetime_is_the_three_way_split(
        monkeypatch: pytest.MonkeyPatch, active: bool | None, finished: bool | None,
        expected: str) -> None:
    _lifetime(monkeypatch, active=active, finished=finished)
    assert advance._unit_lifetime("L3") == expected


def test_settling_an_active_unit_is_a_programming_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_settle_no_evidence`` refuses the one input that would poison the breaker streak."""
    ledger, _worktree = _lane(tmp_path)
    ledger.dispatch("L3")
    with pytest.raises(ValueError):
        advance._settle_no_evidence(ledger, "L3", "L3-d1", lifetime="active", verdict=None,
                                    witness=None, emit=lambda _line: None)
    assert ledger.lane("L3")["status"] == DISPATCHED
