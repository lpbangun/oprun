"""C1 acceptance/rejection cases for the deterministic witness (``scripts/probe.py``).

Every case here is a way a lane could be wrongly declared finished, or wrongly parked forever. The
rule under test is the one in the frozen benchmark:

    done  ⟺  sidecar exists
          ∧  sidecar.task_id + dispatch_id == the ledger's CURRENT dispatch
          ∧  sidecar.status == "success"
          ∧  the controller re-runs the lane's own test command and it exits 0
          ∧  every evidence path resolves

Nothing in this file mocks the acceptance test: ``test_cmd`` really executes, and the ledger
cases use the shipped ``ledger.py`` rather than a stub.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import probe
from ledger import BLOCKED, DISPATCHED, FAILED, Ledger

GREEN_TEST_CMD = [sys.executable, "-c", "raise SystemExit(0)"]
RED_TEST_CMD = [sys.executable, "-c", "raise SystemExit(3)"]

CLAUDE_CANARY_403 = (
    '{"is_error":true,"subtype":"success","api_error_status":403,"type":"result",'
    '"terminal_reason":"api_error","result":"Failed to authenticate. API Error: 403 Access to '
    'model denied."}'
)


# --- helpers -----------------------------------------------------------------
def make_worktree(tmp_path: Path, name: str = "wt") -> Path:
    worktree = tmp_path / name
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def make_lane(tmp_path: Path, *, lane_id: str = "lane-a", dispatch_id: str = "lane-a-d1",
              test_cmd: list[str] | None = None, status: str = DISPATCHED,
              worktree: Path | None = None, harness: str = "cursor-agent") -> dict:
    worktree = worktree if worktree is not None else make_worktree(tmp_path)
    return {
        "lane_id": lane_id,
        "status": status,
        "harness": harness,
        "worktree": str(worktree),
        "dispatch_id": dispatch_id,
        "attempt": 1,
        "test_cmd": list(GREEN_TEST_CMD if test_cmd is None else test_cmd),
        "model_requested": "cursor-grok-4.6",
        "history": [],
    }


def write_sidecar(worktree: Path, dispatch_id: str, *, sidecar_dispatch_id: str | None = None,
                  task_id: str = "lane-a", status: str = "success",
                  files: tuple[str, ...] = (), log_path: str = "",
                  raw: str | None = None) -> Path:
    """Write the lane's sidecar at the canonical path — the same shape the canary workers wrote."""
    base = worktree / ".oprun"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"result.{dispatch_id}.json"
    document = {
        "schema_version": 1,
        "task_id": task_id,
        "dispatch_id": sidecar_dispatch_id if sidecar_dispatch_id is not None else dispatch_id,
        "harness": "cursor-agent 2026.09.10-fd3934a",
        "model": "Cursor Grok 4.6",
        "status": status,
        "exit_code": 0 if status == "success" else 1,
        "evidence": {
            "branch": "lane-a",
            "commit": None,
            "files": list(files),
            "log_path": log_path,
        },
        "summary": "implemented the slice",
        "finished_at": "2026-09-11T17:24:31Z",
    }
    path.write_text(raw if raw is not None else json.dumps(document, indent=2), encoding="utf-8")
    return path


def run_probe(lane: dict, *, unit_active: bool = False, timeout_exceeded: bool = False) -> dict:
    return probe.probe(lane, unit_active=unit_active, timeout_exceeded=timeout_exceeded)


# --- vocabulary --------------------------------------------------------------
def test_verdict_vocabulary_is_frozen() -> None:
    assert probe.VERDICTS == ("done", "pending", "needs_input", "stalled", "failed")


def test_every_verdict_returned_is_in_the_vocabulary(tmp_path: Path) -> None:
    lane = make_lane(tmp_path)
    results = [
        run_probe(lane, unit_active=True),
        run_probe(lane, unit_active=False),
        run_probe(lane, unit_active=True, timeout_exceeded=True),
    ]
    write_sidecar(Path(lane["worktree"]), "lane-a-d1", status="failed")
    results.append(run_probe(lane))
    for result in results:
        assert set(result) == {"verdict", "reason", "evidence"}
        assert result["verdict"] in probe.VERDICTS
        assert isinstance(result["reason"], str) and result["reason"]
        assert isinstance(result["evidence"], dict)


# --- the positive case -------------------------------------------------------
def test_inactive_unit_with_valid_sidecar_and_green_tests_is_done(tmp_path: Path) -> None:
    """A dead unit with a valid sidecar CAN be done — systemd lifetime is not lane state."""
    worktree = make_worktree(tmp_path)
    (worktree / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", files=("stats.py",))

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] == "done", result


def test_done_survives_an_empty_log_path(tmp_path: Path) -> None:
    """``log_path: ""`` means "no log", not "a missing file" (canary sidecars ship exactly this)."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", files=(), log_path="")
    assert run_probe(lane)["verdict"] == "done"


def test_done_ignores_systemd_lifetime(tmp_path: Path) -> None:
    """A live unit with a complete artifact is still done: probe never asks systemd for status."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1")
    assert run_probe(lane, unit_active=True)["verdict"] == "done"


# --- absence of an artifact --------------------------------------------------
def test_no_sidecar_at_all_is_never_done(tmp_path: Path) -> None:
    lane = make_lane(tmp_path)
    assert run_probe(lane, unit_active=True)["verdict"] != "done"
    assert run_probe(lane, unit_active=False)["verdict"] != "done"


def test_active_unit_with_no_sidecar_is_not_done(tmp_path: Path) -> None:
    """The exact trap: a running systemd unit is not a completed lane."""
    lane = make_lane(tmp_path)
    result = run_probe(lane, unit_active=True)
    assert result["verdict"] == "pending"
    assert result["verdict"] != "done"


def test_dead_unit_with_no_sidecar_needs_input(tmp_path: Path) -> None:
    """Nothing is running and nothing landed: escalate rather than wait forever."""
    lane = make_lane(tmp_path)
    result = run_probe(lane, unit_active=False)
    assert result["verdict"] == "needs_input"


def test_timeout_with_no_artifact_is_not_pending_forever(tmp_path: Path) -> None:
    lane = make_lane(tmp_path)
    for unit_active in (True, False):
        result = run_probe(lane, unit_active=unit_active, timeout_exceeded=True)
        assert result["verdict"] in {"stalled", "needs_input"}, result
        assert result["verdict"] != "pending"
        assert result["verdict"] != "done"


def test_timeout_with_a_stale_artifact_is_not_pending(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, dispatch_id="lane-a-d2")
    write_sidecar(worktree, "lane-a-d1")          # only the superseded attempt landed
    result = run_probe(lane, unit_active=True, timeout_exceeded=True)
    assert result["verdict"] in {"stalled", "needs_input"}


def test_never_dispatched_lane_is_not_done(tmp_path: Path) -> None:
    lane = make_lane(tmp_path, dispatch_id=None)
    assert run_probe(lane, unit_active=False)["verdict"] != "done"


# --- fencing: stale and foreign claims --------------------------------------
def test_stale_dispatch_id_in_sidecar_is_not_done(tmp_path: Path) -> None:
    """The file exists, but it speaks for a dispatch that has been superseded."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, dispatch_id="lane-a-d2")
    write_sidecar(worktree, "lane-a-d2", sidecar_dispatch_id="lane-a-d1")

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] != "done"
    assert "stale" in result["reason"]


def test_superseded_sidecar_file_is_not_done(tmp_path: Path) -> None:
    """A real second attempt: only the first attempt's file is on disk."""
    ledger = Ledger(tmp_path / "state.json")
    worktree = make_worktree(tmp_path)
    ledger.init_lane("lane-a", harness="cursor-agent", worktree=str(worktree),
                     test_cmd=GREEN_TEST_CMD)
    first = ledger.dispatch("lane-a")
    write_sidecar(worktree, first)
    ledger.settle("lane-a", first, ok=False, reason="injected failure")
    second = ledger.dispatch("lane-a")
    assert second != first

    lane = dict(ledger.lane("lane-a"))
    lane["lane_id"] = "lane-a"
    result = run_probe(lane, unit_active=False)
    assert result["verdict"] != "done"


def test_sibling_lane_dispatch_id_is_not_done(tmp_path: Path) -> None:
    """Another lane's token in this lane's sidecar is foreign, never a completion."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, lane_id="lane-a", dispatch_id="lane-a-d1")
    write_sidecar(worktree, "lane-a-d1", sidecar_dispatch_id="lane-b-d1", task_id="lane-b")

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] != "done"
    assert result["verdict"] == "needs_input"


def test_sibling_task_id_alone_is_not_done(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, lane_id="lane-a", dispatch_id="lane-a-d1")
    write_sidecar(worktree, "lane-a-d1", task_id="lane-b")

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] != "done"
    assert "foreign" in result["reason"]


# --- the worker's claim vs the controller's re-run ---------------------------
def test_sidecar_success_but_failing_test_cmd_is_needs_input(tmp_path: Path) -> None:
    """A worker's word is not acceptance: the controller re-runs the lane's own test command."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, test_cmd=RED_TEST_CMD)
    write_sidecar(worktree, "lane-a-d1")

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] == "needs_input"
    assert result["verdict"] != "done"
    assert result["evidence"]["test_rc"] == 3


def test_sidecar_failed_but_tests_green_is_needs_input(tmp_path: Path) -> None:
    """The honest worker that reported failure is never rounded up to done."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, test_cmd=GREEN_TEST_CMD)
    write_sidecar(worktree, "lane-a-d1", status="failed")

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] == "needs_input"
    assert result["verdict"] != "done"


def test_lane_without_test_cmd_is_never_done(tmp_path: Path) -> None:
    """No acceptance command means no acceptance — evidence alone cannot settle a lane."""
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree, test_cmd=[])
    write_sidecar(worktree, "lane-a-d1")
    assert run_probe(lane)["verdict"] == "needs_input"


def test_unreadable_sidecar_is_needs_input(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", raw="{not json at all")
    assert run_probe(lane)["verdict"] == "needs_input"


# --- evidence paths ----------------------------------------------------------
def test_missing_evidence_file_is_needs_input(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", files=("stats.py",))

    result = run_probe(lane, unit_active=False)
    assert result["verdict"] == "needs_input"
    assert result["verdict"] != "done"
    assert "stats.py" in result["evidence"]["missing_evidence_paths"]


def test_missing_log_path_is_needs_input(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", log_path="logs/lane-a.log")
    assert run_probe(lane)["verdict"] == "needs_input"


def test_absolute_evidence_path_must_exist(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", files=(str(tmp_path / "nowhere" / "x.py"),))
    assert run_probe(lane)["verdict"] == "needs_input"


def test_present_evidence_files_and_log_are_accepted(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    (worktree / "stats.py").write_text("mean = 1\n")
    (worktree / "logs").mkdir()
    (worktree / "logs" / "lane-a.log").write_text("ok\n")
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1", files=("stats.py", ".oprun"), log_path="logs/lane-a.log")
    assert run_probe(lane)["verdict"] == "done"


# --- the ledger's own word ---------------------------------------------------
def test_failed_lane_in_the_ledger_is_failed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state.json")
    worktree = make_worktree(tmp_path)
    ledger.init_lane("lane-a", harness="cursor-agent", worktree=str(worktree),
                     test_cmd=GREEN_TEST_CMD)
    dispatch_id = ledger.dispatch("lane-a")
    ledger.settle("lane-a", dispatch_id, ok=False, reason="injected failure")
    lane = dict(ledger.lane("lane-a"))
    assert lane["status"] == FAILED

    result = probe.probe(lane, unit_active=False, timeout_exceeded=True)
    assert result["verdict"] == "failed"


def test_blocked_lane_in_the_ledger_is_failed(tmp_path: Path) -> None:
    """Three consecutive failures circuit-break the lane; probe reports that, not pending."""
    ledger = Ledger(tmp_path / "state.json")
    worktree = make_worktree(tmp_path)
    ledger.init_lane("lane-a", harness="cursor-agent", worktree=str(worktree),
                     test_cmd=GREEN_TEST_CMD)
    for _ in range(3):
        dispatch_id = ledger.dispatch("lane-a")
        ledger.settle("lane-a", dispatch_id, ok=False, reason="injected failure")
    lane = dict(ledger.lane("lane-a"))
    assert lane["status"] == BLOCKED

    result = probe.probe(lane, unit_active=True, timeout_exceeded=False)
    assert result["verdict"] == "failed"


def test_completed_lane_with_valid_sidecar_is_done(tmp_path: Path) -> None:
    """The settled happy path, through the real ledger's own dispatch id."""
    ledger = Ledger(tmp_path / "state.json")
    worktree = make_worktree(tmp_path)
    (worktree / "stats.py").write_text("mean = 1\n")
    ledger.init_lane("lane-a", harness="cursor-agent", worktree=str(worktree),
                     test_cmd=GREEN_TEST_CMD)
    dispatch_id = ledger.dispatch("lane-a")
    write_sidecar(worktree, dispatch_id, files=("stats.py",))
    lane = dict(ledger.lane("lane-a"))
    lane["lane_id"] = "lane-a"

    result = probe.probe(lane, unit_active=False, timeout_exceeded=False)
    assert result["verdict"] == "done", result


# --- determinism -------------------------------------------------------------
def test_probe_is_deterministic(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    write_sidecar(worktree, "lane-a-d1")
    for unit_active, timeout_exceeded in ((False, False), (True, False), (True, True)):
        first = run_probe(lane, unit_active=unit_active, timeout_exceeded=timeout_exceeded)
        second = run_probe(lane, unit_active=unit_active, timeout_exceeded=timeout_exceeded)
        assert first == second


def test_sidecar_dir_argument_points_at_the_sidecar_directory(tmp_path: Path) -> None:
    worktree = make_worktree(tmp_path)
    lane = make_lane(tmp_path, worktree=worktree)
    elsewhere = tmp_path / "sidecars"
    elsewhere.mkdir()
    (elsewhere / "result.lane-a-d1.json").write_text(json.dumps({
        "task_id": "lane-a", "dispatch_id": "lane-a-d1", "status": "success",
        "evidence": {"files": [], "log_path": ""},
    }))
    assert probe.probe(lane, unit_active=False, timeout_exceeded=False,
                       sidecar_dir=elsewhere)["verdict"] == "done"


# --- systemd lifetime --------------------------------------------------------
def test_unit_state_reports_absent_unit_as_inactive() -> None:
    assert probe.unit_state("oprun-probe-test-does-not-exist") is False


def test_unit_state_returns_a_bool_for_a_real_unit() -> None:
    assert isinstance(probe.unit_state("dbus.service"), bool)


# --- terminal events ---------------------------------------------------------
def test_canary_subtype_trap_is_not_a_success() -> None:
    """claude 403 shape: ``subtype:"success"`` + ``is_error:true`` + exit 1 (measured 2026-09-11)."""
    result = probe.parse_terminal_event("claude", CLAUDE_CANARY_403)
    assert result["present"] is True
    assert result["ok"] is False
    assert "is_error" in result["detail"]


def test_subtype_success_with_is_error_never_ok_for_cursor_agent() -> None:
    stdout = '{"type":"result","subtype":"success","is_error":true}'
    assert probe.parse_terminal_event("cursor-agent", stdout)["ok"] is False


def test_honest_success_event_is_ok() -> None:
    stdout = ('{"type":"result","subtype":"success","is_error":false,"duration_ms":4753,'
              '"result":"PROBE_OK"}')
    result = probe.parse_terminal_event("cursor-agent", stdout)
    assert result["present"] is True
    assert result["ok"] is True


def test_codex_turn_completed_is_ok() -> None:
    stdout = "\n".join([
        "Reading additional input from stdin...",
        '{"type":"thread.started","thread_id":"01a0917e"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"PROBE_OK"}}',
        '{"type":"turn.completed","usage":{"input_tokens":16378}}',
    ])
    result = probe.parse_terminal_event("codex", stdout)
    assert result["present"] is True
    assert result["ok"] is True


def test_codex_failed_turn_is_not_ok() -> None:
    stdout = '{"type":"turn.started"}\n{"type":"turn.failed","error":{"message":"boom"}}'
    result = probe.parse_terminal_event("codex", stdout)
    assert result["ok"] is False


def test_codex_turn_completed_with_an_error_event_is_not_ok() -> None:
    stdout = '{"type":"error","message":"stream error"}\n{"type":"turn.completed"}'
    assert probe.parse_terminal_event("codex", stdout)["ok"] is False


def test_opencode_without_step_finish_is_not_ok() -> None:
    """opencode is known to drop ``step_finish`` (#26855); absence is never a pass."""
    stdout = '{"type":"text","part":{"text":"PROBE_OK"}}'
    result = probe.parse_terminal_event("opencode", stdout)
    assert result["present"] is False
    assert result["ok"] is False


def test_opencode_step_finish_is_ok() -> None:
    stdout = '{"type":"text","part":{"text":"PROBE_OK"}}\n{"type":"step_finish","part":{}}'
    result = probe.parse_terminal_event("opencode", stdout)
    assert result["present"] is True
    assert result["ok"] is True


def test_hermes_session_line_is_the_terminal_witness() -> None:
    ok = probe.parse_terminal_event("hermes", "session_id: 4f2ac1de-9c1b\nresult written\n")
    assert ok["present"] is True
    assert ok["ok"] is True

    noisy = probe.parse_terminal_event("hermes", "Session: 4f2ac1de-9c1b\nerror: no credentials\n")
    assert noisy["ok"] is False


def test_pi_plain_text_is_not_silently_ok() -> None:
    """pi's witness is its OS exit code; stdout text alone is not a terminal event."""
    result = probe.parse_terminal_event("pi", "")
    assert result["present"] is False and result["ok"] is False
    assert "exit code" in result["detail"]


def test_unmapped_harness_raises_key_error() -> None:
    with pytest.raises(KeyError):
        probe.parse_terminal_event("herdr", '{"type":"result","subtype":"success"}')


# --- CLI ---------------------------------------------------------------------
def _dispatch_lane_with_sidecar(tmp_path: Path, *, test_cmd: list[str] | None = None) -> tuple[
        Path, Path]:
    ledger_path = tmp_path / "state.json"
    ledger = Ledger(ledger_path)
    worktree = make_worktree(tmp_path)
    (worktree / "stats.py").write_text("mean = 1\n")
    ledger.init_lane("lane-a", harness="cursor-agent", worktree=str(worktree),
                     test_cmd=GREEN_TEST_CMD if test_cmd is None else test_cmd)
    dispatch_id = ledger.dispatch("lane-a")
    write_sidecar(worktree, dispatch_id, files=("stats.py",))
    return ledger_path, worktree


def test_main_prints_the_verdict_token_and_exits_zero(tmp_path: Path, capsys) -> None:
    ledger_path, _ = _dispatch_lane_with_sidecar(tmp_path)
    code = probe.main(["--ledger", str(ledger_path), "lane-a"])
    assert code == 0
    assert capsys.readouterr().out.strip() == "done"


def test_main_json_document(tmp_path: Path, capsys) -> None:
    ledger_path, _ = _dispatch_lane_with_sidecar(tmp_path)
    code = probe.main(["--ledger", str(ledger_path), "lane-a", "--json"])
    assert code == 0
    document = json.loads(capsys.readouterr().out.strip())
    assert document["verdict"] == "done"
    assert document["evidence"]["sidecar_present"] is True


def test_main_exits_one_for_a_failed_lane(tmp_path: Path, capsys) -> None:
    ledger_path, _ = _dispatch_lane_with_sidecar(tmp_path)
    ledger = Ledger(ledger_path)
    lane = ledger.lane("lane-a")
    ledger.settle("lane-a", lane["dispatch_id"], ok=False, reason="injected failure")
    code = probe.main(["--ledger", str(ledger_path), "lane-a"])
    assert code == 1
    assert capsys.readouterr().out.strip() == "failed"


def test_main_unknown_lane_is_a_usage_error(tmp_path: Path, capsys) -> None:
    ledger_path = tmp_path / "state.json"
    Ledger(ledger_path)
    code = probe.main(["--ledger", str(ledger_path), "nope"])
    assert code == 2


def test_main_does_not_wait_forever_without_an_artifact(tmp_path: Path, capsys) -> None:
    """Past the timeout a lane with no artifact must not stay pending."""
    ledger_path = tmp_path / "state.json"
    ledger = Ledger(ledger_path)
    worktree = make_worktree(tmp_path)
    ledger.init_lane("lane-a", harness="cursor-agent", worktree=str(worktree),
                     test_cmd=GREEN_TEST_CMD)
    ledger.dispatch("lane-a")
    code = probe.main(["--ledger", str(ledger_path), "lane-a", "--timeout", "0"])
    assert code == 0
    assert capsys.readouterr().out.strip() in {"stalled", "needs_input"}
