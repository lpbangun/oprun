"""The escalation clocks: acceptance budget, ``over_budget``, unit collision, parked ``nextAction``.

Frozen bar (Codevisor, session ``20260913_012503_7a62c5``, *FREEZE EXPLICIT ESCALATION*): acceptance
time, staleness, unit ownership and parked disposition are four different things, and one timeout
clock for all of them is what produces false environment faults, killed workers and commands that
cannot succeed. Concretely, and asserted below:

* a lane's **acceptance budget** is recorded at dispatch (``--test-timeout``, default 900s) and every
  acceptance path reads that number; a legacy lane that recorded none reads 900;
* ``--timeout`` is the **staleness** clock only — it never shrinks a command that has already
  started, and past the cutoff ``probe`` returns ``stalled``, never ``pending``;
* a command that outruns the lane's own recorded budget is ``over_budget``: it parks for review, it
  is never ``infra``, it spends no ``infra`` retry and it never touches the failure streak;
* ``dispatch`` refuses when ``oprun-<lane>`` is already active, prints
  ``systemctl --user stop oprun-<lane>`` and leaves the lane ready — no silent kill, no failure
  settlement for an occupied name;
* a lane the ledger already parked reports ``nextAction: none``.

No live systemd and no harness binary anywhere here: systemd and the launcher are stubbed where the
CLI path is exercised, so every case runs deterministically on any host.
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import advance  # noqa: E402
import launch  # noqa: E402
import oprun  # noqa: E402
import probe  # noqa: E402
from ledger import (  # noqa: E402
    BLOCKED,
    COMPLETED,
    DISPATCHED,
    FAILED,
    PENDING,
    READY,
    IllegalTransition,
    Ledger,
)

#: A green acceptance command, run through the same interpreter as the suite.
GREEN = [sys.executable, "-c", "raise SystemExit(0)"]
#: How long these tests may take before something is hanging rather than asserting.
BOUNDED_SECONDS = 10.0


def _sleep_cmd(seconds: float) -> list[str]:
    """A command that outlives any budget used here, so the budget is what ends the run."""
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _worktree(tmp_path: Path, name: str = "wt") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sidecar(worktree: Path, dispatch_id: str, *, lane_id: str = "alpha",
             status: str = "success") -> Path:
    """Write a lane's evidence file the way a worker would: at the exact canonical path."""
    directory = worktree / advance.SIDECAR_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "task_id": lane_id, "dispatch_id": dispatch_id,
               "status": status, "harness": "cursor-agent", "model": "cursor-grok-4.6",
               "exit_code": 0, "evidence": {"files": [], "log_path": "", "waiver": "legacy test fixture"}}
    target = directory / f"{advance.SIDECAR_PREFIX}{dispatch_id}{advance.SIDECAR_SUFFIX}"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return target


def _ledger_with_lane(tmp_path: Path, *, lane_id: str = "alpha",
                      test_cmd: list[str] | None = None,
                      test_timeout_s: float | None = None) -> tuple[Ledger, Path]:
    """A real ledger plus a real lane in a real worktree — no mocks in the ledger path."""
    ledger = Ledger(tmp_path / f"{lane_id}-state.json")
    worktree = _worktree(tmp_path, f"wt-{lane_id}")
    ledger.init_lane(lane_id, harness="cursor-agent", worktree=str(worktree),
                     test_cmd=list(GREEN if test_cmd is None else test_cmd),
                     model="cursor-grok-4.6", test_timeout_s=test_timeout_s)
    return ledger, worktree


def _lane_record(worktree: Path, **overrides) -> dict:
    """The lane dict an acceptance path is handed (probe reads nothing but this)."""
    lane = {
        "lane_id": "alpha",
        "status": DISPATCHED,
        "harness": "cursor-agent",
        "worktree": str(worktree),
        "dispatch_id": "alpha-d1",
        "attempt": 1,
        "test_cmd": list(GREEN),
        "model_requested": "cursor-grok-4.6",
        "history": [],
    }
    lane.update(overrides)
    return lane


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _mission(tmp_path: Path) -> dict:
    """A mission ledger created through the shipped ``init`` verb."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    ledger = repo / ".tmp" / "oprun" / "state.json"
    assert oprun.main(["init", str(repo), "--mission", "escalation-test"]) == 0
    return {"repo": repo, "ledger": ledger}


def _stub_launcher(monkeypatch: pytest.MonkeyPatch, *, unit_active: bool) -> list[tuple]:
    """Make ``oprun dispatch`` run end to end with no systemd and no harness binary.

    Returns the list of launches that actually reached the launcher, so a refusal can prove that
    nothing was started over a live unit.
    """
    calls: list[tuple] = []

    def fake_launch(unit: str, argv: list[str], *, workdir: Path, env: dict | None = None) -> dict:
        calls.append((unit, list(argv)))
        return {"unit": unit, "started": True, "detail": "stubbed launcher",
                "path": "/opt/lane-tools"}

    monkeypatch.setattr(oprun, "_resolve_binary", lambda _binary: "/bin/true")
    monkeypatch.setattr(launch, "launch", fake_launch)
    monkeypatch.setattr(launch, "unit_status", lambda unit: {
        "active": unit_active, "exec_main_status": None, "result": None, "control_group": None})
    return calls


def _dispatch_argv(ledger: Path, *extra: str, lane: str = "alpha") -> list[str]:
    return ["dispatch", lane, "--harness", "cursor-agent", "--model", "cursor-grok-4.6",
            "--test-cmd", f"{sys.executable} -c pass", "--prompt", "do the thing",
            *extra, "--ledger", str(ledger)]


# --- 1. one acceptance budget, read from the lane --------------------------------------------
def test_the_acceptance_budget_is_one_number_on_every_acceptance_path() -> None:
    """No second 900, and no runner-owned 300: one field name, one default, read from the lane."""
    assert probe.TEST_TIMEOUT_FIELD == advance.TEST_TIMEOUT_FIELD == oprun.TEST_TIMEOUT_FIELD
    assert probe.TEST_TIMEOUT_FIELD == "test_timeout_s"
    assert (probe.DEFAULT_TEST_TIMEOUT_S == advance.DEFAULT_TEST_TIMEOUT_S
            == oprun.DEFAULT_TEST_TIMEOUT_S == 900.0)
    # the controller's own clock IS the shipped default, derived rather than written twice
    assert probe.TEST_CMD_TIMEOUT_SECONDS == probe.DEFAULT_TEST_TIMEOUT_S
    assert not hasattr(advance, "TEST_TIMEOUT"), "the separate 300s runner ceiling must be gone"
    assert not hasattr(oprun, "TEST_TIMEOUT"), "one number, not a per-module constant"


def test_a_legacy_lane_with_no_test_timeout_s_reads_the_900s_default(tmp_path: Path) -> None:
    """A ledger written before the field existed keeps working: it reads 900 seconds."""
    lane = _lane_record(_worktree(tmp_path))          # no ``test_timeout_s`` key at all

    assert probe.recorded_test_timeout_s(lane) is None
    assert probe.acceptance_budget_s(lane) == 900.0 == probe.DEFAULT_TEST_TIMEOUT_S
    assert advance.recorded_test_timeout_s(lane) is None
    assert advance.acceptance_budget_s(lane) == 900.0 == advance.DEFAULT_TEST_TIMEOUT_S


def test_a_recorded_budget_is_read_verbatim_and_nonsense_is_not_a_budget(tmp_path: Path) -> None:
    lane = _lane_record(_worktree(tmp_path), test_timeout_s=1200)
    assert probe.recorded_test_timeout_s(lane) == 1200
    assert probe.acceptance_budget_s(lane) == 1200
    assert advance.acceptance_budget_s(lane) == 1200

    for nonsense in (None, 0, -5, "not-a-number", True, float("inf")):
        assert probe.recorded_test_timeout_s({"test_timeout_s": nonsense}) is None
        assert advance.recorded_test_timeout_s({"test_timeout_s": nonsense}) is None
        assert probe.acceptance_budget_s({"test_timeout_s": nonsense}) == 900.0
        assert advance.acceptance_budget_s({"test_timeout_s": nonsense}) == 900.0


def test_a_legacy_lane_is_judged_by_the_default_clock_and_says_so(tmp_path: Path) -> None:
    """The budget actually used is in the evidence, with whether the lane declared it."""
    worktree = _worktree(tmp_path)
    lane = _lane_record(worktree)
    _sidecar(worktree, "alpha-d1")

    result = probe.probe(lane, unit_active=False, timeout_exceeded=False)

    assert result["verdict"] == "done", result
    assert result["evidence"]["acceptance_budget_s"] == 900.0
    assert result["evidence"]["budget_recorded"] is False


# --- 2. over budget: the lane's own clock, never ``infra`` ------------------------------------
def test_a_command_that_outran_the_recorded_budget_is_over_budget_not_infra(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    lane = _lane_record(worktree, test_cmd=_sleep_cmd(30), test_timeout_s=0.4)
    _sidecar(worktree, "alpha-d1")

    result = probe.probe(lane, unit_active=False, timeout_exceeded=False)

    assert result["verdict"] == "over_budget", result
    assert result["verdict"] in probe.VERDICTS
    assert result["verdict"] != "infra"
    assert result["verdict"] != "failed"
    assert result["evidence"]["test_rc"] == probe.TEST_CMD_TIMEOUT_RC == 124
    assert result["evidence"]["acceptance_budget_s"] == 0.4
    assert result["evidence"]["budget_recorded"] is True
    assert "0.4" in result["evidence"]["over_budget_reason"]
    assert result["evidence"].get("infra_reason") is None, "over_budget is not the environment"


def test_the_ledger_parks_an_over_budget_outcome_without_moving_the_streak(tmp_path: Path) -> None:
    ledger, _worktree_path = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")

    outcome = ledger.settle("alpha", dispatch_id, False,
                            evidence={"verdict": "over_budget", "outcome": "over_budget"},
                            reason="command exceeded the recorded acceptance budget",
                            over_budget=True)

    lane = ledger.lane("alpha")
    assert outcome["accepted"] is True and outcome["over_budget_count"] == 1
    assert lane["status"] == BLOCKED, "parked for a human, and terminal for this workflow"
    assert lane["status"] != FAILED, "no verdict was reached, so it is not a failed lane"
    assert lane["consecutive_failures"] == 0, "the breaker counts red tests, and none was proven"
    assert lane["infra"] == [], "over_budget is a different clock from infra and is kept apart"
    assert lane["blocked_reason"].startswith("over_budget")
    assert "circuit breaker" not in lane["blocked_reason"]
    assert lane["over_budget"][0]["dispatch_id"] == dispatch_id
    assert lane["over_budget"][0]["evidence"]["verdict"] == "over_budget"
    assert dispatch_id not in lane["accepted"], "an over-budget outcome is not a settled success"
    assert "over_budget" in {entry["event"] for entry in lane["history"]}
    assert "test_timeout_s" in lane["blocked_reason"]
    assert "failure streak is untouched" in lane["blocked_reason"]

    # exactly-once still holds, and an over-budget outcome can never be an acceptance
    again = ledger.settle("alpha", dispatch_id, False, reason="again", over_budget=True)
    assert again["accepted"] is False and "not dispatched" in again["reason"]

    with pytest.raises(ValueError):
        ledger.settle("alpha", dispatch_id, True, over_budget=True)
    with pytest.raises(ValueError):
        ledger.settle("alpha", dispatch_id, False, infra=True, over_budget=True)


def test_advance_parks_an_over_budget_lane_without_a_streak_or_an_infra_retry(tmp_path: Path) -> None:
    """The unattended path: parked for review, no breaker strike, no in-place ``INFRA_RETRY``."""
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=_sleep_cmd(30), test_timeout_s=0.4)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    lines: list[str] = []

    started = time.monotonic()
    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)
    elapsed = time.monotonic() - started

    lane = ledger.lane("alpha")
    assert summary["accepted"] == [], "an over-budget run can never be an acceptance"
    assert summary["needs_review"] == ["alpha"]
    assert lane["status"] == BLOCKED
    assert lane["status"] != DISPATCHED
    assert lane["consecutive_failures"] == 0, "nothing was proven red, so the streak must not move"
    assert lane["infra"] == [], "an over-budget run records no infra outcome"
    assert [entry["event"] for entry in lane["history"]
            if entry["event"] in ("success", "failure")] == [], "no failure transition at all"
    assert len(lane["over_budget"]) == 1
    record = lane["over_budget"][0]
    assert record["dispatch_id"] == dispatch_id
    assert record["evidence"]["verdict"] == advance.OVER_BUDGET == "over_budget"
    assert record["evidence"]["outcome"] == "over_budget"
    assert record["evidence"]["test_exit_code"] == 124
    assert record["evidence"]["acceptance_budget_s"] == 0.4
    assert lane["blocked_reason"].startswith("over_budget")
    assert any("NEEDS_REVIEW over_budget" in line for line in lines), lines
    assert not any("INFRA_RETRY" in line for line in lines), "no infra retry may be spent"
    assert elapsed < BOUNDED_SECONDS, f"the park must be immediate (took {elapsed:.1f}s)"

    with pytest.raises(IllegalTransition):
        ledger.dispatch("alpha")          # a parked lane is not re-entered by the runner or by us


def test_settle_accept_refuses_an_over_budget_lane_and_names_the_human_path(
        tmp_path: Path, capsys) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=_sleep_cmd(30), test_timeout_s=0.4)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)

    rc = oprun.main(["settle", "alpha", "--accept", "--ledger", str(ledger.path)])
    captured = capsys.readouterr()

    assert rc == oprun.EXIT_ERROR
    assert "Traceback" not in captured.err
    assert "over_budget" in captured.err
    assert "test_timeout_s" in captured.err and "0.4s" in captured.err
    assert "--needs-review" in captured.err
    lane = ledger.lane("alpha")
    assert lane["status"] == DISPATCHED, "a refused --accept settles nothing"
    assert lane["consecutive_failures"] == 0
    assert lane["infra"] == [] and lane["over_budget"] == []


# --- 3. the runner's deadline is not the acceptance budget ------------------------------------
def test_the_runners_deadline_never_shrinks_a_command_that_has_already_started(
        tmp_path: Path) -> None:
    """``--timeout`` is staleness: a command that started runs to the budget the lane recorded."""
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=_sleep_cmd(1.5), test_timeout_s=30.0)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)

    summary = advance.advance(ledger.path, timeout=0.2, poll=0.05, emit=lambda _line: None)

    lane = ledger.lane("alpha")
    assert summary["accepted"] == ["alpha"], "the run outlives the runner's own deadline"
    assert lane["status"] == COMPLETED
    assert lane["evidence"]["test_exit_code"] == 0
    assert lane["consecutive_failures"] == 0
    # ...and the runner owns no deadline-shaped knob over the run at all
    assert "deadline" not in inspect.signature(advance._run_test).parameters


# --- 4. dispatch records the budget, and refuses an occupied unit -----------------------------
def test_dispatch_records_the_acceptance_budget_and_never_silently_resets_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _mission(tmp_path)
    _stub_launcher(monkeypatch, unit_active=False)
    # the shipped writer that puts a lane back in ``ready`` for a re-dispatch (an infra outcome)
    ledger = Ledger(fixture["ledger"], infra_limit=5)

    assert oprun.main(_dispatch_argv(fixture["ledger"])) == 0
    lane = _read(fixture["ledger"])["lanes"]["alpha"]
    assert lane["status"] == DISPATCHED
    assert lane["test_timeout_s"] == 900.0, "the shipped default is RECORDED, not merely implied"

    # the dispatch-only override replaces the recorded budget
    ledger.settle("alpha", lane["dispatch_id"], False, reason="controller could not run it",
                  infra=True)
    assert _read(fixture["ledger"])["lanes"]["alpha"]["status"] == READY
    assert oprun.main(_dispatch_argv(fixture["ledger"], "--test-timeout", "1800")) == 0
    assert _read(fixture["ledger"])["lanes"]["alpha"]["test_timeout_s"] == 1800.0

    # ...and a re-dispatch that does NOT restate it cannot silently reset it to the default
    lane = _read(fixture["ledger"])["lanes"]["alpha"]
    ledger.settle("alpha", lane["dispatch_id"], False, reason="again", infra=True)
    assert oprun.main(_dispatch_argv(fixture["ledger"])) == 0
    after = _read(fixture["ledger"])["lanes"]["alpha"]
    assert after["dispatch_id"] == "alpha-d3"
    assert after["test_timeout_s"] == 1800.0, "an implicit re-dispatch never resets the budget"


def test_a_non_positive_test_timeout_is_refused_before_anything_is_registered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    fixture = _mission(tmp_path)
    calls = _stub_launcher(monkeypatch, unit_active=False)

    rc = oprun.main(_dispatch_argv(fixture["ledger"], "--test-timeout", "0"))
    captured = capsys.readouterr()

    assert rc == oprun.EXIT_USAGE
    assert "--test-timeout" in captured.err
    assert calls == []
    assert _read(fixture["ledger"])["lanes"] == {}, "a refused dispatch registers nothing"


def test_dispatch_refuses_an_occupied_unit_and_prints_the_stop_command(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The frozen escape: refuse, name ``systemctl --user stop oprun-<lane>``, leave the lane ready."""
    fixture = _mission(tmp_path)
    calls = _stub_launcher(monkeypatch, unit_active=True)          # a worker is already running

    rc = oprun.main(_dispatch_argv(fixture["ledger"]))
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert rc != 0
    assert rc == oprun.EXIT_ILLEGAL
    assert "systemctl --user stop oprun-alpha" in output
    assert calls == [], "nothing may be launched over a live unit"

    lane = _read(fixture["ledger"])["lanes"]["alpha"]
    assert lane["status"] != DISPATCHED, "the live worker's lane must not be handed a new token"
    assert lane["status"] in (PENDING, READY), "the lane is left ready to be dispatched"
    assert lane["dispatch_id"] is None
    assert lane["attempt"] == 0
    assert lane["accepted"] == [] and lane["rejected"] == [], "a refusal settles nothing"
    assert "needs_review_reason" not in lane


def test_a_refused_dispatch_leaves_a_live_lanes_token_and_budget_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused re-dispatch touches nothing about a lane whose worker is still live."""
    fixture = _mission(tmp_path)
    _stub_launcher(monkeypatch, unit_active=False)
    assert oprun.main(_dispatch_argv(fixture["ledger"])) == 0
    before = _read(fixture["ledger"])["lanes"]["alpha"]
    assert before["dispatch_id"] == "alpha-d1" and before["status"] == DISPATCHED
    assert before["test_timeout_s"] == 900.0

    _stub_launcher(monkeypatch, unit_active=True)   # the d1 worker is still running
    rc = oprun.main(_dispatch_argv(fixture["ledger"], "--test-timeout", "1800"))

    assert rc == oprun.EXIT_ILLEGAL
    lane = _read(fixture["ledger"])["lanes"]["alpha"]
    assert lane == before, "a refused dispatch writes nothing at all to a live lane"
    assert lane["status"] == DISPATCHED, "the live worker's lane stays in flight"
    assert lane["dispatch_id"] == "alpha-d1", "its dispatch token is untouched"
    assert lane["attempt"] == 1


# --- 5. C8: past the cutoff, never ``pending``, liveness included -----------------------------
def test_probe_is_stalled_past_the_cutoff_even_while_the_unit_is_still_active(
        tmp_path: Path) -> None:
    """``pending`` is only valid BEFORE the cutoff; the reason carries the liveness probe saw."""
    worktree = _worktree(tmp_path)
    lane = _lane_record(worktree)                    # dispatched, no sidecar, no artifact ever

    before = probe.probe(lane, unit_active=True, timeout_exceeded=False)
    assert before["verdict"] == "pending", before
    assert before["evidence"]["liveness_note"] == "unit still active"

    after = probe.probe(lane, unit_active=True, timeout_exceeded=True)
    assert after["verdict"] == "stalled", after
    assert after["verdict"] != "pending"
    assert "still active" in after["reason"] and "did not wait" in after["reason"]
    assert after["evidence"]["unit_active"] is True
    assert after["evidence"]["timeout_exceeded"] is True
    assert after["evidence"]["liveness_note"] == "unit still active; probe did not wait"

    inactive = probe.probe(lane, unit_active=False, timeout_exceeded=True)
    assert inactive["verdict"] == "stalled"
    assert inactive["evidence"]["liveness_note"] == "unit is not active"


def test_probe_cli_reports_stalled_for_a_live_unit_and_never_waits_it_out(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    ledger.dispatch("alpha")                     # dispatched, and no sidecar will ever appear
    # the unit is alive: probe must still call it stalled past the cutoff, and say so
    monkeypatch.setattr(oprun, "_unit_active", lambda _lane_id, _lane: True)

    rc = oprun.main(["probe", "alpha", "--timeout", "0", "--ledger", str(ledger.path)])
    captured = capsys.readouterr()

    assert rc == oprun.EXIT_OK
    assert captured.out.strip() == "stalled"
    assert "still active" in captured.err and "did not wait" in captured.err
    assert worktree.is_dir()


# --- 6. a parked lane has no next action ------------------------------------------------------
def test_a_parked_lane_reports_next_action_none(tmp_path: Path, capsys) -> None:
    """``settle`` is restricted to in-flight lanes, so a parked lane cannot be offered one."""
    fixture = _mission(tmp_path)
    ledger = Ledger(fixture["ledger"])
    ledger.init_lane("alpha", harness="cursor-agent", worktree=str(fixture["repo"]),
                     test_cmd=list(GREEN))
    dispatch_id = ledger.dispatch("alpha")
    ledger.settle("alpha", dispatch_id, False, reason="flaky test",
                  evidence={"verdict": "needs_review", "reason": "flaky test"})
    assert ledger.lane("alpha")["status"] == FAILED

    assert oprun.main(["status", "--ledger", str(fixture["ledger"])]) == 0
    captured = capsys.readouterr()
    assert "nextAction: none" in captured.out
    assert "settle" not in captured.out.split("nextAction:")[-1]

    # a parked lane contributes NO action: the pending lane beside it still gets its dispatch
    ledger.init_lane("beta", harness="cursor-agent", worktree=str(fixture["repo"]),
                     test_cmd=list(GREEN))
    assert oprun.main(["status", "--json", "--ledger", str(fixture["ledger"])]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nextAction"] == "dispatch beta", payload["nextAction"]
    assert payload["lanes"]["alpha"]["status"] == FAILED


def test_next_action_still_names_a_blocked_lane_for_review(tmp_path: Path) -> None:
    """``blocked`` is a different disposition: it keeps its review pointer, it gains no settle."""
    assert oprun._next_action({"alpha": {"status": BLOCKED}}) == "review alpha (blocked)"
    assert oprun._next_action({"alpha": {"status": FAILED}}) == "none"
    assert oprun._next_action({"alpha": {"status": COMPLETED}}) == "none"
    assert oprun._next_action({}) == "none"
