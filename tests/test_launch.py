"""Tests for the detached launcher (``scripts/launch.py``) and the bounded advance runner
(``scripts/advance.py``).

Nothing here needs a live harness, and only one test needs a live systemd user session (it skips
when there is none). The detach story is asserted on the exact argv, and ``advance`` is driven
against a real :class:`ledger.Ledger` plus tmp worktrees, so every rule under test is the rule
that ships.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import advance
import launch
import oprun
from ledger import (
    BLOCKED,
    COMPLETED,
    DEFAULT_INFRA_LIMIT,
    DISPATCHED,
    FAILED,
    READY,
    IllegalTransition,
    Ledger,
)

#: A test command that passes, and one that fails, run through the same interpreter as the suite.
TEST_PASS = [sys.executable, "-c", "raise SystemExit(0)"]
TEST_FAIL = [sys.executable, "-c", "print('boom'); raise SystemExit(1)"]

#: A program that exists nowhere: the controller cannot START this command (issue #1's ENOENT).
TEST_ENOENT = ["oprun-no-such-program-xyz"]

#: A tool that exists in ONE directory and nowhere else on this host — the bare name resolves only
#: when the acceptance re-run runs under a PATH that contains that directory. That is exactly the
#: asymmetry ``unit_path`` removes: the worker's unit was pinned with such a PATH, the controller's
#: own PATH is a different variable.
BARE_TOOL_NAME = "oprun-path-parity-tool"

#: Prints the PATH the re-run actually handed the child, so the environment is observable output.
PRINT_PATH_TEST_CMD = [sys.executable, "-c", "import os; print(os.environ.get('PATH'))"]


# --- fixtures ----------------------------------------------------------------


def _ledger_with_lane(tmp_path: Path, lane_id: str = "alpha", *, test_cmd: list[str] | None = None,
                      failure_limit: int = 3,
                      infra_limit: int = DEFAULT_INFRA_LIMIT) -> tuple[Ledger, Path]:
    """A real ledger plus a real lane with a real worktree — no mocks anywhere."""
    ledger = Ledger(tmp_path / f"{lane_id}-state.json", failure_limit=failure_limit,
                    infra_limit=infra_limit)
    worktree = tmp_path / f"wt-{lane_id}"
    worktree.mkdir(parents=True, exist_ok=True)
    ledger.init_lane(lane_id, harness="cursor-agent", worktree=str(worktree),
                     test_cmd=list(TEST_PASS if test_cmd is None else test_cmd),
                     model="cursor-grok-4.6")
    return ledger, worktree


def _sidecar(worktree: Path, dispatch_id: str, *, status: str = "success",
             task_id: str | None = "alpha", filename: str | None = None,
             extra: dict | None = None) -> Path:
    """Write a lane's evidence file the way a worker would: atomically, at the exact path."""
    directory = worktree / advance.SIDECAR_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "dispatch_id": dispatch_id, "status": status,
               "harness": "cursor-agent", "model": "cursor-grok-4.6", "exit_code": 0}
    if task_id is not None:
        payload["task_id"] = task_id
    payload.update(extra or {})
    target = directory / (filename or f"{advance.SIDECAR_PREFIX}{dispatch_id}"
                                       f"{advance.SIDECAR_SUFFIX}")
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return target


def _systemd_user_available() -> bool:
    """Whether a live systemd user session is reachable (the only test that needs one)."""
    if not shutil.which("systemctl") or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        proc = subprocess.run(["systemctl", "--user", "is-system-running"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _wait_for_report(path: Path, *, seconds: float = 20.0) -> str:
    """Poll for a file a detached unit writes about ITSELF, and return its text (``\"\"`` on timeout).

    Bounded: the unit is detached, so there is no process to join and no exit status to read after
    ``--collect`` removed it — the unit's own output on disk is the only honest witness.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(0.1)
    return ""


# --- launch.py: the detach story, asserted on the argv -----------------------


def test_build_systemd_run_argv_is_a_detached_transient_user_unit(tmp_path: Path) -> None:
    workdir = tmp_path / "wt"
    argv = launch.build_systemd_run_argv("oprun-alpha", ["/bin/echo", "hi"], workdir)

    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert "--collect" in argv, "transient units must not accumulate"
    assert f"--working-directory={workdir}" in argv
    assert any(item.endswith("=oprun-alpha") or item == "oprun-alpha" for item in argv)
    assert f"--setenv=HOME={launch.REAL_HOME}" in argv

    separator = argv.index("--")
    assert argv[separator + 1:] == ["/bin/echo", "hi"], "the command must follow --"


def test_build_systemd_run_argv_pins_home_and_extra_env(tmp_path: Path) -> None:
    argv = launch.build_systemd_run_argv("oprun-alpha", ["/bin/true"], tmp_path,
                                         {"OPRUN_LANE": "alpha", "HOME": "/wrong"})
    assert f"--setenv=HOME={launch.REAL_HOME}" in argv
    assert "--setenv=OPRUN_LANE=alpha" in argv
    assert "--setenv=HOME=/wrong" not in argv, "HOME cannot be overridden by a caller"


def test_build_systemd_run_argv_carries_the_resolved_path_onto_the_unit(tmp_path: Path) -> None:
    """Issue #1: a systemd user unit gets no login shell, so PATH has to be carried onto it.

    A lane whose ``test_cmd`` names its tool by the bare name (``npx``, ``node``, ``npm``) died
    ENOENT inside the unit, which the controller read as rc 127 — a failed test. The unit now starts
    with the conductor's resolved PATH, so a bare name resolves there exactly as it does here.
    """
    workdir = tmp_path / "wt"
    argv = launch.build_systemd_run_argv("oprun-alpha", ["/bin/true"], workdir)

    pinned = [item for item in argv if item.startswith("--setenv=PATH=")]
    assert len(pinned) == 1, argv
    value = pinned[0].split("=", 2)[2]
    assert value, "an empty PATH is the ENOENT failure mode itself"

    entries = value.split(os.pathsep)
    conductor = [entry for entry in (os.environ.get("PATH") or "").split(os.pathsep) if entry]
    assert entries[:len(conductor)] == conductor, "the conductor's own entries keep precedence"
    for entry in launch.SYSTEM_PATH_ENTRIES:
        assert entry in entries, f"{entry} must be reachable inside the unit"
    assert shutil.which("sh", path=value) is not None, "a bare tool name must resolve under the pin"

    # ...and the HOME pin is still exactly what it was.
    assert f"--setenv=HOME={launch.REAL_HOME}" in argv
    assert "--setenv=HOME=/wrong" not in argv


def test_worker_path_never_returns_empty_and_honours_an_explicit_override(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/opt/lane-tools")
    assert launch.worker_path() == os.pathsep.join(("/opt/lane-tools", *launch.SYSTEM_PATH_ENTRIES))

    # A caller may pin a narrow PATH on purpose (a venv's bin, say): the override is used verbatim —
    # the one place a caller overrides the pin, unlike HOME where a sandboxed value is always a bug.
    assert launch.worker_path({"PATH": "/venv/bin"}) == "/venv/bin"
    assert "--setenv=PATH=/venv/bin" in launch.build_systemd_run_argv(
        "oprun-alpha", ["/bin/true"], Path("/tmp"), {"PATH": "/venv/bin"})

    monkeypatch.delenv("PATH", raising=False)
    assert launch.worker_path() == os.pathsep.join(launch.SYSTEM_PATH_ENTRIES)
    assert launch.worker_path({}) == os.pathsep.join(launch.SYSTEM_PATH_ENTRIES)


def test_build_systemd_run_argv_uses_no_shell_detach_shims(tmp_path: Path) -> None:
    argv = launch.build_systemd_run_argv("oprun-alpha", ["/bin/true"], tmp_path)
    joined = " ".join(argv)
    for token in ("nohup", "tmux", "screen", "herdr", "disown", "&"):
        assert token not in joined, f"{token!r} must never be the detach mechanism"

    # The gate greps the module itself, so assert the source carries none of them either.
    source = Path(launch.__file__).read_text(encoding="utf-8")
    assert re.search(r"nohup|tmux|herdr|screen ", source) is None


def test_build_systemd_run_argv_rejects_empty_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        launch.build_systemd_run_argv("", ["/bin/true"], tmp_path)
    with pytest.raises(ValueError):
        launch.build_systemd_run_argv("oprun-alpha", [], tmp_path)


def test_unit_status_reads_exact_keys_and_never_lies_about_an_unknown_unit() -> None:
    status = launch.unit_status("oprun-does-not-exist-xyz")
    assert set(status) == {"active", "exec_main_status", "result", "control_group"}
    assert status["active"] is False
    # systemd's default values for a unit that never existed are Result=success/ExecMainStatus=0;
    # echoing those would read as a successful run, so an unknown unit reports nothing.
    assert status["exec_main_status"] is None
    assert status["result"] is None
    assert status["control_group"] is None
    assert launch.unit_known("oprun-does-not-exist-xyz") is False
    assert launch.unit_finished("oprun-does-not-exist-xyz") is None, "absence is not death"
    launch.stop("oprun-does-not-exist-xyz")          # teardown must never raise


def test_launch_refuses_a_missing_workdir_without_spawning_anything(tmp_path: Path) -> None:
    result = launch.launch("oprun-alpha", ["/bin/true"], workdir=tmp_path / "nope")
    assert result["unit"] == "oprun-alpha"
    assert result["started"] is False
    assert "not found" in result["detail"]


@pytest.mark.skipif(not _systemd_user_available(), reason="no systemd user session on this host")
def test_launch_lands_a_real_unit_outside_the_session_scope(tmp_path: Path) -> None:
    """One live launch: the cgroup is the proof that the worker is detached, not this process."""
    workdir = tmp_path / "wt"
    workdir.mkdir()
    unit = launch.unit_name(f"selftest-{os.getpid()}")
    launch.stop(unit)
    try:
        result = launch.launch(unit, ["/bin/sleep", "30"], workdir=workdir)
        assert result["started"] is True, result["detail"]
        assert launch.unit_known(unit) is True
        status = launch.unit_status(unit)
        assert status["active"] is True
        assert "app.slice" in (status["control_group"] or ""), status
        assert "session-" not in (status["control_group"] or "")
    finally:
        launch.stop(unit)
    assert launch.unit_status(unit)["active"] is False


@pytest.mark.skipif(not _systemd_user_available() or shutil.which("npx") is None,
                    reason="needs a live systemd user session and a bare npx on PATH")
def test_a_bare_tool_name_resolves_inside_a_launched_unit(tmp_path: Path) -> None:
    """Live proof of issue #1's fix: the UNIT itself resolves ``npx`` by its bare name.

    The unit writes down what *it* resolved — ``command -v npx`` runs inside the unit, never here —
    and the very same command is launched twice: once with the pinned PATH, once with a deliberately
    poisoned one. Only the pin makes the bare name resolve, and the poisoned control is exactly the
    environment every systemd unit used to start in.
    """
    workdir = tmp_path / "wt"
    workdir.mkdir()
    script = ("resolved=$(command -v npx || echo NOT-FOUND); "
              "printf '%s\\n' \"$resolved\" > \"$OPRUN_UNIT_REPORT\"")
    expected = shutil.which("npx")
    assert expected is not None                     # the skipif above just proved it

    pinned_report = tmp_path / "unit-path-pinned.txt"
    pinned_unit = launch.unit_name(f"pathcheck-{os.getpid()}")
    launch.stop(pinned_unit)
    try:
        started = launch.launch(pinned_unit, ["/bin/sh", "-c", script], workdir=workdir,
                                env={"OPRUN_UNIT_REPORT": str(pinned_report)})
        assert started["started"] is True, started["detail"]
        assert started["path"] == launch.worker_path(), "the launch reports the PATH it pinned"
        assert _wait_for_report(pinned_report) == expected
    finally:
        launch.stop(pinned_unit)

    poisoned_report = tmp_path / "unit-path-poisoned.txt"
    poisoned_unit = launch.unit_name(f"pathcheck-bad-{os.getpid()}")
    launch.stop(poisoned_unit)
    try:
        started = launch.launch(poisoned_unit, ["/bin/sh", "-c", script], workdir=workdir,
                                env={"OPRUN_UNIT_REPORT": str(poisoned_report),
                                     "PATH": "/nonexistent"})
        assert started["started"] is True, started["detail"]
        assert _wait_for_report(poisoned_report) == "NOT-FOUND"
    finally:
        launch.stop(poisoned_unit)


# --- advance.py: acceptance is evidence, and the loop is bounded -------------


def test_advance_accepts_a_lane_with_a_valid_sidecar_and_a_passing_test(tmp_path: Path) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    lines: list[str] = []

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)

    assert summary["accepted"] == ["alpha"]
    assert summary["needs_review"] == []
    assert summary["final"] == {COMPLETED: 1}
    lane = ledger.lane("alpha")
    assert lane["status"] == COMPLETED
    assert lane["evidence"]["test_exit_code"] == 0
    assert lane["evidence"]["test_result"] == "pass"
    assert lane["evidence"]["dispatch_id"] == dispatch_id
    assert lane["evidence"]["sidecar"].endswith(f"result.{dispatch_id}.json")
    assert any("ACCEPTED" in line for line in lines)


def test_advance_parks_a_lane_whose_controller_test_fails(tmp_path: Path) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=TEST_FAIL)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    lines: list[str] = []

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)

    assert "alpha" in summary["needs_review"]
    assert "alpha" not in summary["accepted"]
    lane = ledger.lane("alpha")
    assert lane["status"] == FAILED
    assert lane["status"] != COMPLETED
    assert "test re-run failed" in lane["needs_review_reason"]
    assert any("NEEDS_REVIEW" in line for line in lines)


def test_advance_returns_within_a_bounded_wall_clock_with_no_evidence(tmp_path: Path) -> None:
    """A lane with no unit at all and no sidecar is a FAILED dispatch — and advance returns.

    This assertion used to read "no evidence means no settlement — ever", keeping the lane
    ``dispatched``. That was the defect: a lane whose unit never existed is not in flight, and
    leaving it dispatched is the v0.1 frozen-ledger shape (no unit running, no artifact coming).
    Absence of evidence is still never an *acceptance* — it is a failure, and the ledger says so.
    """
    lane_id = f"nosidecar-{uuid.uuid4().hex[:6]}"     # a unit that provably never existed
    ledger, _ = _ledger_with_lane(tmp_path, lane_id)
    ledger.dispatch(lane_id)
    lines: list[str] = []

    started = time.monotonic()
    summary = advance.advance(ledger.path, timeout=1, poll=0.05, emit=lines.append)
    elapsed = time.monotonic() - started

    assert elapsed < 6.0, f"advance must return when the timeout elapses (took {elapsed:.1f}s)"
    assert summary["accepted"] == []
    assert summary["stalled"] + summary["timed_out"] == [lane_id]
    lane = ledger.lane(lane_id)
    assert lane["status"] in (FAILED, BLOCKED), "no unit and no artifact is not in flight"
    assert lane["status"] != DISPATCHED
    assert lane["needs_review_reason"] == "no unit and no acceptable sidecar"
    assert lane["consecutive_failures"] == 1, "a failed dispatch counts toward the breaker"
    assert any("TIMED_OUT" in line or "STALLED" in line for line in lines)
    assert any("NEEDS_REVIEW" in line for line in lines)


def test_advance_stops_at_the_breaker_and_never_retries_a_parked_lane(tmp_path: Path) -> None:
    """An injected, repeated failure parks the lane at ``blocked``; advance does not touch it."""
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=TEST_FAIL, failure_limit=3)
    dispatch_id = ledger.dispatch("alpha")
    lines: list[str] = []

    for attempt in (1, 2, 3):
        _sidecar(worktree, dispatch_id)
        summary = advance.advance(ledger.path, timeout=2, poll=0.05, emit=lines.append)
        assert summary["accepted"] == [], "a failing lane is never accepted"
        assert "alpha" in summary["needs_review"]
        lane = ledger.lane("alpha")
        if attempt < 3:
            assert lane["status"] == FAILED
            assert lane["consecutive_failures"] == attempt
            dispatch_id = ledger.dispatch("alpha")     # the conductor's explicit re-dispatch
    parked = ledger.lane("alpha")
    assert parked["status"] == BLOCKED
    assert parked["consecutive_failures"] == 3
    assert parked["attempt"] == 3, "the breaker stopped the streak at failure_limit"
    assert parked["blocked_reason"].startswith("circuit breaker")

    # ...and it stays parked: no further advance run may retry or re-open it.
    summary = advance.advance(ledger.path, timeout=1, poll=0.05, emit=lines.append)
    assert summary == {"accepted": [], "needs_review": [], "stalled": [], "timed_out": [],
                       "final": {BLOCKED: 1}}
    assert ledger.lane("alpha")["attempt"] == 3
    with pytest.raises(IllegalTransition):
        ledger.dispatch("alpha")


# --- infra: the controller could not RUN the command (issue #1) ---------------


def test_ledger_infra_settlement_returns_the_lane_to_ready_and_never_touches_the_streak(
        tmp_path: Path) -> None:
    """``settle(infra=True)`` is the one settlement that is not an outcome of the lane."""
    ledger, _ = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")

    outcome = ledger.settle("alpha", dispatch_id, False, evidence={"verdict": "infra"},
                            reason="test command could not run", infra=True)

    lane = ledger.lane("alpha")
    assert outcome["accepted"] is True and outcome["infra_count"] == 1
    assert lane["status"] == READY, "ready: the ordinary dispatch gate re-enters it"
    assert lane["consecutive_failures"] == 0, "the breaker counts red tests, never an ENOENT"
    assert lane["infra"][0]["dispatch_id"] == dispatch_id
    assert lane["infra"][0]["evidence"]["verdict"] == "infra"
    assert "infra" in {entry["event"] for entry in lane["history"]}
    assert dispatch_id not in lane["accepted"], "an infra outcome is not a settled success"
    assert "needs_review_reason" not in lane, "an infra outcome is not a review park"

    # exactly-once still holds: the same token cannot settle the lane twice
    again = ledger.settle("alpha", dispatch_id, False, reason="again", infra=True)
    assert again["accepted"] is False and "not dispatched" in again["reason"]

    # ...and an infra outcome can never be an acceptance
    dispatch_id = ledger.dispatch("alpha")
    with pytest.raises(ValueError):
        ledger.settle("alpha", dispatch_id, True, infra=True)

    # the shipped cap is wired through the constructor (``advance`` reports it on the retry line)
    assert Ledger(tmp_path / "cap-state.json").infra_limit == DEFAULT_INFRA_LIMIT


def test_advance_records_an_enoent_test_cmd_as_infra_and_leaves_the_breaker_alone(
        tmp_path: Path) -> None:
    """Issue #1's second half: rc 127 is an environment fault, never a red test.

    The sidecar is valid and the lane's tests were never proven red — the program simply is not on
    this host — so the lane goes back to ``ready`` (re-dispatchable through the ordinary gate, which
    is what relaunches its worker) with its failure streak untouched, and the attempt is recorded in
    ``lane["infra"]`` so the fault is visible in the ledger itself, not only in stdout.
    """
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=TEST_ENOENT)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    lines: list[str] = []

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)

    lane = ledger.lane("alpha")
    assert summary["accepted"] == [], "an unrunnable command can never be an acceptance"
    assert summary["needs_review"] == [], "an environment fault is not a lane for review"
    assert summary["stalled"] == [] and summary["timed_out"] == []
    assert summary["final"] == {READY: 1}
    assert lane["status"] == READY
    assert lane["status"] != COMPLETED
    assert lane["status"] != DISPATCHED, "the dispatch is over: the lane is ready to be re-dispatched"
    assert lane["consecutive_failures"] == 0, "the breaker counts red tests, never an ENOENT"
    assert len(lane["infra"]) == 1
    record = lane["infra"][0]
    assert record["dispatch_id"] == dispatch_id
    assert TEST_ENOENT[0] in record["reason"]
    assert record["evidence"]["outcome"] == "infra"
    assert record["evidence"]["test_exit_code"] == 127
    assert any("INFRA_RETRY" in line for line in lines)
    assert not any("NEEDS_REVIEW" in line for line in lines)

    # ...and the lane really is re-dispatchable: the ordinary gate gives it a fresh token.
    assert ledger.dispatch("alpha") != dispatch_id


def test_advance_parks_after_the_infra_limit_with_the_breaker_still_untouched(
        tmp_path: Path) -> None:
    """A permanently unrunnable command must reach a human — without ever counting as a red test.

    Two infra outcomes (the ``infra_limit`` here) and the lane is parked ``blocked`` with a reason
    that names the ENVIRONMENT, not the circuit breaker: the lane's tests never ran, so its failure
    streak stays at zero even as it is parked for review.
    """
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=TEST_ENOENT, infra_limit=2)
    lines: list[str] = []

    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)
    first = ledger.lane("alpha")
    assert first["status"] == READY and len(first["infra"]) == 1
    assert first["consecutive_failures"] == 0

    dispatch_id = ledger.dispatch("alpha")        # the conductor's explicit re-dispatch
    _sidecar(worktree, dispatch_id)
    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)

    parked = ledger.lane("alpha")
    assert parked["status"] == BLOCKED
    assert len(parked["infra"]) == 2
    assert parked["consecutive_failures"] == 0, "the infra park never touches the failure streak"
    assert parked["blocked_reason"].startswith("infra")
    assert "circuit breaker" not in parked["blocked_reason"]
    assert "alpha" in summary["needs_review"]
    assert any("NEEDS_REVIEW infra" in line for line in lines)
    with pytest.raises(IllegalTransition):
        ledger.dispatch("alpha")


def test_a_red_controller_test_is_still_a_failure_and_still_counts(tmp_path: Path) -> None:
    """The other side of the split: a test that RAN and failed keeps exactly its old meaning."""
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=TEST_FAIL)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lambda _line: None)

    lane = ledger.lane("alpha")
    assert lane["status"] == FAILED
    assert "alpha" in summary["needs_review"]
    assert lane["consecutive_failures"] == 1, "a red test still moves the breaker"
    assert lane["infra"] == [], "a red test is not an infra outcome"
    assert "test re-run failed" in lane["needs_review_reason"]


# --- the PATH the acceptance re-run happens under ----------------------------
# ``launch.py`` pins a PATH onto every worker unit and dispatch records that reported value on the
# lane; the runner's own re-run must use it, or the worker's PATH and the controller's PATH are
# different variables by construction and the acceptance re-run can ENOENT a tool the worker used.


def _bare_tool(directory: Path, name: str = BARE_TOOL_NAME) -> Path:
    """An executable reachable by its bare name only from ``directory`` (or another PATH entry)."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def _record_unit_path(ledger: Ledger, lane_id: str, unit_path: str) -> None:
    """Record a launch PATH on the lane through the SHIPPED writer, exactly as dispatch does."""
    oprun._record_unit_path(ledger.path, lane_id, unit_path)
    assert ledger.lane(lane_id)[advance.UNIT_PATH_FIELD] == unit_path


def test_advance_reruns_the_test_cmd_under_the_lanes_recorded_path(tmp_path: Path) -> None:
    """The lane's tool resolves for the controller because the re-run uses the WORKER's PATH."""
    tool_dir = tmp_path / "lane-tools"
    _bare_tool(tool_dir)
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=[BARE_TOOL_NAME])
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    _record_unit_path(ledger, "alpha", str(tool_dir))
    lines: list[str] = []

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)

    lane = ledger.lane("alpha")
    assert summary["accepted"] == ["alpha"], lines
    assert lane["status"] == COMPLETED
    assert lane["evidence"]["test_exit_code"] == 0
    assert lane["evidence"]["unit_path"] == str(tool_dir)


def test_advance_without_the_recorded_path_cannot_start_the_same_test_cmd(tmp_path: Path) -> None:
    """The control, and the measured asymmetry: without the recording it is rc 127 — ``infra``.

    Not a red test: the command never ran, so the lane goes back to ``ready`` with its failure
    streak untouched (issue #1). Recording the PATH is what removes the asymmetry; inventing one
    would be a different fix, and this asserts the fallback stays the ambient environment.
    """
    tool_dir = tmp_path / "lane-tools"
    _bare_tool(tool_dir)
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=[BARE_TOOL_NAME])
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    lines: list[str] = []

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lines.append)

    lane = ledger.lane("alpha")
    assert summary["accepted"] == [], lines
    assert lane["status"] == READY, "an unrunnable command is an environment fault, not a lane"
    assert lane["consecutive_failures"] == 0
    assert lane["infra"][0]["evidence"]["test_exit_code"] == 127


def test_advance_passes_exactly_the_recorded_path_to_the_test_cmd(tmp_path: Path) -> None:
    """Verbatim, and nothing else: the re-run gets the recorded PATH with no additions."""
    recorded = "/opt/lane-tools:/usr/bin:/bin"
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=list(PRINT_PATH_TEST_CMD))
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)
    _record_unit_path(ledger, "alpha", recorded)

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lambda _line: None)

    lane = ledger.lane("alpha")
    assert summary["accepted"] == ["alpha"]
    assert lane["evidence"]["unit_path"] == recorded
    assert lane["evidence"]["test_output_tail"].strip() == recorded


def test_advance_without_a_recorded_path_reruns_with_the_ambient_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: a ledger written before ``unit_path`` existed keeps its old environment.

    ``env=None`` is what "inherit the ambient environment" means, so a tool reachable only through
    the ambient PATH still resolves for the re-run — asserted behaviourally (the tool runs), not by
    long-string equality of a PATH that the evidence tail may truncate.
    """
    tool_dir = tmp_path / "ambient-tools"
    _bare_tool(tool_dir)
    ambient = [entry for entry in (os.environ.get("PATH") or "").split(os.pathsep) if entry]
    monkeypatch.setenv("PATH", os.pathsep.join((str(tool_dir), *ambient)))

    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=[BARE_TOOL_NAME])
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)

    summary = advance.advance(ledger.path, timeout=5, poll=0.05, emit=lambda _line: None)

    lane = ledger.lane("alpha")
    assert summary["accepted"] == ["alpha"]
    assert lane["evidence"]["unit_path"] is None
    assert lane["evidence"]["test_exit_code"] == 0


def test_the_rerun_env_invents_nothing_for_an_absent_path() -> None:
    """``None`` (or blank) is "inherit", never a fabricated PATH: no defaults, no resolution."""
    assert advance._test_cmd_env(None) is None
    assert advance._test_cmd_env("") is None
    assert advance._test_cmd_env("   ") is None
    assert advance._test_cmd_env("/venv/bin") == {**os.environ, "PATH": "/venv/bin"}


def test_advance_never_accepts_a_sidecar_naming_a_superseded_dispatch(tmp_path: Path) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id, extra={"dispatch_id": "alpha-d0"})   # file for d1, claims d0

    summary = advance.advance(ledger.path, timeout=1, poll=0.05, emit=lambda _line: None)

    assert summary["accepted"] == []
    assert "alpha" in summary["needs_review"]
    lane = ledger.lane("alpha")
    assert lane["status"] == FAILED
    assert "stale dispatch" in lane["needs_review_reason"]


def test_advance_never_accepts_an_old_attempts_sidecar(tmp_path: Path) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    ledger.dispatch("alpha")                                            # current dispatch is d1
    _sidecar(worktree, "alpha-d0")                                      # only d0's evidence exists

    summary = advance.advance(ledger.path, timeout=1, poll=0.05, emit=lambda _line: None)

    assert summary["accepted"] == []
    assert "alpha" in summary["needs_review"]
    assert "stale sidecar" in ledger.lane("alpha")["needs_review_reason"]


def test_advance_never_accepts_a_sidecar_that_reports_failure(tmp_path: Path) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id, status="failed")

    summary = advance.advance(ledger.path, timeout=1, poll=0.05, emit=lambda _line: None)

    assert summary["accepted"] == []
    assert "not 'success'" in ledger.lane("alpha")["needs_review_reason"]


def test_advance_parks_a_sidecar_with_a_mismatched_task_id(tmp_path: Path) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id, task_id="beta")                     # evidence for another lane

    summary = advance.advance(ledger.path, timeout=1, poll=0.05, emit=lambda _line: None)

    assert summary["accepted"] == []
    assert "task_id" in ledger.lane("alpha")["needs_review_reason"]


def test_advance_returns_immediately_when_nothing_is_dispatched(tmp_path: Path) -> None:
    ledger, _ = _ledger_with_lane(tmp_path)
    started = time.monotonic()
    summary = advance.advance(ledger.path, timeout=30, poll=0.05, emit=lambda _line: None)
    assert time.monotonic() - started < 1.0, "an idle ledger must not be waited on"
    assert summary == {"accepted": [], "needs_review": [], "stalled": [], "timed_out": [],
                       "final": {"pending": 1}}


def test_advance_reports_stale_evidence_through_the_sidecar_reader(tmp_path: Path) -> None:
    """The reader itself: an exact-path hit wins, a foreign claim is reported, not ignored."""
    directory = tmp_path / advance.SIDECAR_DIRNAME
    path = _sidecar(tmp_path, "alpha-d1")
    found, payload, note = advance._find_sidecar(directory, "alpha-d1")
    assert (found, note) == (path, "")
    assert payload is not None and payload["dispatch_id"] == "alpha-d1"

    found, payload, note = advance._find_sidecar(directory, "alpha-d2")
    assert found is None and payload is None
    assert "stale sidecar" in note and "alpha-d1" in note


def test_lane_verdict_is_the_documented_local_fallback(tmp_path: Path) -> None:
    """``lane_verdict`` mirrors ``probe``'s contract, so a missing probe.py degrades cleanly."""
    ledger, worktree = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")
    lane = ledger.lane("alpha")

    pending = advance.lane_verdict(lane, lane_id="alpha")
    assert pending["case"] == "no_artifact"
    assert pending["verdict"] == "pending"

    stalled = advance.lane_verdict(lane, lane_id="alpha", unit_active=False)
    assert stalled["verdict"] == "stalled" and stalled["case"] == "no_artifact"

    timed_out = advance.lane_verdict(lane, lane_id="alpha", timeout_exceeded=True)
    assert timed_out["verdict"] == "needs_input"

    _sidecar(worktree, dispatch_id)
    done = advance.lane_verdict(lane, lane_id="alpha")
    assert done["verdict"] == "done" and done["case"] == "ok"
    witness = advance.witness_verdict(lane, lane_id="alpha")
    assert witness["verdict"] == "done"
    assert witness["source"].startswith("probe.py") or witness["source"].startswith("local:")


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_git_evidence_names_every_changed_path_exactly(tmp_path: Path) -> None:
    """Regression: a blanket stdout strip turned ' M work.txt' into 'ork.txt' — a hash of a file
    that does not exist, i.e. evidence that silently proved nothing."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(worktree), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    (worktree / "work.txt").write_text("base\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")
    head = git("rev-parse", "HEAD")
    (worktree / "work.txt").write_text("changed\n", encoding="utf-8")      # worktree-only change
    (worktree / "new.txt").write_text("added\n", encoding="utf-8")         # untracked file
    (worktree / ".oprun").mkdir()
    (worktree / ".oprun" / "result.x.json").write_text("{}\n", encoding="utf-8")

    evidence = advance.git_evidence(worktree)

    # The lane has NOT committed: HEAD is still the base commit, so nothing may name it as the
    # lane's commit (that base SHA dressed up as the lane's work is the v0.2 evidence defect).
    # The content hashes below are the only thing that identifies this work.
    assert evidence["commit"] is None
    assert evidence["commit"] != head
    assert evidence["uncommitted"] is True
    assert set(evidence["hashes"]) == {"work.txt", "new.txt", ".oprun/"}
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["hashes"]["work.txt"] or "")
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["hashes"]["new.txt"] or "")
    assert (evidence["hashes"][".oprun/"] or "").startswith("dir:")


def test_git_evidence_records_missing_git_instead_of_guessing(tmp_path: Path) -> None:
    evidence = advance.git_evidence(tmp_path)           # not a repository at all
    assert evidence["commit"] is None
    assert evidence["hashes"] == {}
    assert evidence["git_error"]


# --- the CLI -----------------------------------------------------------------


def test_cli_prints_lane_lines_and_a_json_summary(tmp_path: Path, capsys) -> None:
    ledger, worktree = _ledger_with_lane(tmp_path)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)

    code = advance.main(["--ledger", str(ledger.path), "--timeout", "2", "--poll", "0.05",
                         "--json"])

    assert code == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)                 # stdout stays machine-readable
    assert summary["accepted"] == ["alpha"]
    assert summary["final"] == {COMPLETED: 1}
    assert "ACCEPTED" in captured.err                  # per-lane progress goes to stderr


def test_cli_reports_a_missing_ledger_without_crashing(tmp_path: Path, capsys) -> None:
    code = advance.main(["--ledger", str(tmp_path / "nope.json"), "--timeout", "1"])
    assert code == 2
    assert "ledger not found" in capsys.readouterr().err


def test_cli_exits_zero_on_a_completed_run_even_when_a_lane_needs_review(
        tmp_path: Path, capsys) -> None:
    """Lane status lives in the ledger; a non-zero exit here would abort a caller's inspection."""
    ledger, worktree = _ledger_with_lane(tmp_path, test_cmd=TEST_FAIL)
    dispatch_id = ledger.dispatch("alpha")
    _sidecar(worktree, dispatch_id)

    code = advance.main(["--ledger", str(ledger.path), "--timeout", "1", "--poll", "0.05"])

    assert code == 0
    assert ledger.lane("alpha")["status"] == FAILED
    assert "NEEDS_REVIEW" in capsys.readouterr().out


def test_module_has_no_unbounded_loop_and_no_model() -> None:
    source = Path(advance.__file__).read_text(encoding="utf-8")
    assert "while True" not in source, "the runner must never wait forever"
    for forbidden in ("import hermes", "openai", "anthropic", "litellm", "delegate_task"):
        assert forbidden not in source
