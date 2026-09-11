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
from ledger import BLOCKED, COMPLETED, DISPATCHED, FAILED, IllegalTransition, Ledger

#: A test command that passes, and one that fails, run through the same interpreter as the suite.
TEST_PASS = [sys.executable, "-c", "raise SystemExit(0)"]
TEST_FAIL = [sys.executable, "-c", "print('boom'); raise SystemExit(1)"]


# --- fixtures ----------------------------------------------------------------


def _ledger_with_lane(tmp_path: Path, lane_id: str = "alpha", *, test_cmd: list[str] | None = None,
                      failure_limit: int = 3) -> tuple[Ledger, Path]:
    """A real ledger plus a real lane with a real worktree — no mocks anywhere."""
    ledger = Ledger(tmp_path / f"{lane_id}-state.json", failure_limit=failure_limit)
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
    # No evidence means no settlement — ever. Inventing a failure would poison the breaker.
    assert ledger.lane(lane_id)["status"] == DISPATCHED
    assert any("TIMED_OUT" in line or "STALLED" in line for line in lines)


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

    evidence = advance._git_evidence(worktree)

    assert evidence["commit"] == head
    assert evidence["uncommitted"] is True
    assert set(evidence["hashes"]) == {"work.txt", "new.txt", ".oprun/"}
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["hashes"]["work.txt"] or "")
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["hashes"]["new.txt"] or "")
    assert (evidence["hashes"][".oprun/"] or "").startswith("dir:")


def test_git_evidence_records_missing_git_instead_of_guessing(tmp_path: Path) -> None:
    evidence = advance._git_evidence(tmp_path)          # not a repository at all
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
