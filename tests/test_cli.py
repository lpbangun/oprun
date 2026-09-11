"""The CLI surface: verbs, exit codes, ledger round-trips, and the refusals that matter.

No live harness and no live systemd: every case below either exercises a pure path or stops at a
ledger refusal, so the suite is deterministic on any host. The approval envelope — which *does*
perform real git — lives in ``tests/test_approvals.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "oprun.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import probe as probe_mod  # noqa: E402
from ledger import Ledger  # noqa: E402

VERBS = ("init", "dispatch", "probe", "settle", "status", "approve")
APPROVAL_KEYS = ("commit", "push", "merge", "deploy", "publish", "destructive")

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "oprun test",
    "GIT_AUTHOR_EMAIL": "oprun@test.local",
    "GIT_COMMITTER_NAME": "oprun test",
    "GIT_COMMITTER_EMAIL": "oprun@test.local",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


# --- helpers -----------------------------------------------------------------
def run_cli(*args: str, cwd: Path | None = None,
            stdin: int = subprocess.DEVNULL) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          cwd=str(cwd) if cwd else None, stdin=stdin, timeout=180)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=GIT_ENV)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def read_ledger(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_repo(tmp_path: Path) -> dict:
    """A mission repo. Git only where the case needs it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"tmp": tmp_path, "repo": repo, "ledger": repo / ".tmp" / "oprun" / "state.json"}


def init_mission(tmp_path: Path, *extra: str) -> dict:
    fixture = make_repo(tmp_path)
    proc = run_cli("init", str(fixture["repo"]), "--mission", "cli-test", *extra)
    assert proc.returncode == 0, proc.stderr
    return fixture


# --- the six verbs -----------------------------------------------------------
def test_help_lists_all_six_verbs() -> None:
    proc = run_cli("--help")
    assert proc.returncode == 0, proc.stderr
    for verb in VERBS:
        assert verb in proc.stdout, f"--help does not list {verb!r}"


@pytest.mark.parametrize("verb", VERBS)
def test_every_verb_has_its_own_help(verb: str) -> None:
    proc = run_cli(verb, "--help")
    assert proc.returncode == 0, proc.stderr
    assert verb in proc.stdout


def test_unknown_verb_is_non_zero_and_prints_no_traceback() -> None:
    proc = run_cli("frobnicate")
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert proc.stderr.strip()


def test_missing_verb_is_non_zero_and_prints_no_traceback() -> None:
    proc = run_cli()
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr


def test_unknown_lane_is_non_zero_and_prints_no_traceback(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    for args in (("probe", "ghost"), ("settle", "ghost", "--accept"),
                 ("settle", "ghost", "--needs-review", "nope")):
        proc = run_cli(*args, "--ledger", str(fixture["ledger"]))
        assert proc.returncode != 0, args
        assert "Traceback" not in proc.stderr, args
        assert proc.stderr.strip(), args


# --- init --------------------------------------------------------------------
def test_init_creates_a_parseable_ledger(tmp_path: Path) -> None:
    fixture = make_repo(tmp_path)
    assert not fixture["ledger"].exists()

    proc = run_cli("init", str(fixture["repo"]), "--mission", "cli-test",
                   "--approve", "commit,push", "--max-parallel", "2", "--failure-limit", "5")
    assert proc.returncode == 0, proc.stderr
    assert fixture["ledger"].is_file()

    doc = read_ledger(fixture["ledger"])
    assert doc["mission"] == "cli-test"
    assert doc["lanes"] == {}
    assert doc["max_parallel"] == 2 and doc["failure_limit"] == 5
    assert set(doc["approvals"]) == set(APPROVAL_KEYS)
    assert doc["approvals"]["commit"]["granted"] is True
    assert doc["approvals"]["destructive"]["granted"] is False

    # the mission dir + mission.md, and nothing launched
    assert (fixture["ledger"].parent / "mission.md").is_file()
    assert run_cli("status", "--ledger", str(fixture["ledger"])).returncode == 0


def test_init_writes_nothing_outside_the_mission_dir(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    stray = sorted(p.name for p in fixture["repo"].iterdir())
    assert stray == [".tmp"], f"init created unexpected entries: {stray}"


def test_a_second_init_is_idempotent_and_keeps_lanes(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    led = Ledger(fixture["ledger"])
    led.init_lane("keep-me", harness="cursor-agent", worktree=str(fixture["repo"]),
                  test_cmd=["true"])

    again = run_cli("init", str(fixture["repo"]), "--mission", "renamed")
    assert again.returncode == 0, again.stderr
    doc = read_ledger(fixture["ledger"])
    assert doc["mission"] == "renamed"
    assert "keep-me" in doc["lanes"], "a second init must not drop lanes"
    assert doc["lanes"]["keep-me"]["harness"] == "cursor-agent"


def test_init_does_not_require_a_git_repo(tmp_path: Path) -> None:
    fixture = make_repo(tmp_path)          # no `git init` anywhere
    assert run_cli("init", str(fixture["repo"]), "--mission", "m").returncode == 0


def test_verbs_refuse_a_missing_ledger(tmp_path: Path) -> None:
    fixture = make_repo(tmp_path)          # never initialised
    for args in (("status",), ("probe", "x"), ("approve", "--grant", "push"),
                 ("settle", "x", "--accept")):
        proc = run_cli(*args, "--ledger", str(fixture["ledger"]))
        assert proc.returncode != 0, args
        assert "Traceback" not in proc.stderr, args


def test_ledger_flag_works_on_either_side_of_the_verb(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    before = run_cli("--ledger", str(fixture["ledger"]), "status", "--json")
    after = run_cli("status", "--json", "--ledger", str(fixture["ledger"]))
    assert before.returncode == 0 and after.returncode == 0
    assert json.loads(before.stdout)["lanes"] == json.loads(after.stdout)["lanes"]


def test_default_ledger_is_found_from_the_repo_root(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    proc = run_cli("status", cwd=fixture["repo"])      # no --ledger at all
    assert proc.returncode == 0, proc.stderr
    assert "cli-test" in proc.stdout


# --- dispatch ----------------------------------------------------------------
def test_dispatch_refuses_a_lane_whose_dependency_is_not_complete(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    led = Ledger(fixture["ledger"])
    led.init_lane("parent", harness="cursor-agent", worktree=str(fixture["repo"]),
                  test_cmd=["true"])

    proc = run_cli("dispatch", "child", "--harness", "cursor-agent", "--test-cmd", "true",
                   "--prompt", "do the thing", "--depends-on", "parent",
                   "--ledger", str(fixture["ledger"]))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "parent" in (proc.stdout + proc.stderr)
    # refused by the ledger, so nothing reached a launch
    assert read_ledger(fixture["ledger"])["lanes"]["child"]["status"] == "pending"


def test_dispatch_refuses_an_unknown_harness(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    proc = run_cli("dispatch", "x", "--harness", "not-a-harness", "--test-cmd", "true",
                   "--prompt", "y", "--ledger", str(fixture["ledger"]))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "not-a-harness" in proc.stderr


def test_dispatch_refuses_claude_without_a_model_pin(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    proc = run_cli("dispatch", "x", "--harness", "claude", "--test-cmd", "true",
                   "--prompt", "y", "--ledger", str(fixture["ledger"]))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "model" in proc.stderr.lower()
    assert read_ledger(fixture["ledger"])["lanes"] == {}, "a refused dispatch registers nothing"


def test_dispatch_requires_a_test_command_and_a_task(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    no_test = run_cli("dispatch", "x", "--harness", "cursor-agent", "--prompt", "y",
                      "--ledger", str(fixture["ledger"]))
    assert no_test.returncode != 0 and "Traceback" not in no_test.stderr

    no_task = run_cli("dispatch", "x", "--harness", "cursor-agent", "--test-cmd", "true",
                      "--ledger", str(fixture["ledger"]))
    assert no_task.returncode != 0 and "Traceback" not in no_task.stderr


def test_dispatch_brief_rendering_names_this_dispatch_sidecar(tmp_path: Path) -> None:
    """The worker preamble must carry the sidecar path for the dispatch that is about to run."""
    import oprun

    text = oprun.render_worker_brief(lane="alpha", dispatch_id="alpha-d3",
                                     worktree=Path("/w/alpha"), test_cmd=["pytest", "-q"],
                                     task="do the thing")
    assert "alpha-d3" in text
    assert str(Path("/w/alpha/.oprun/result.alpha-d3.json")) in text
    assert "do the thing" in text
    assert "not the conductor" in text


def test_worker_argv_uses_the_registry_recipe_and_returns_the_brief(tmp_path: Path) -> None:
    import oprun

    brief = tmp_path / "brief.md"
    brief.write_text("TASK", encoding="utf-8")
    argv = oprun.build_worker_argv("cursor-agent", model=None, brief_path=brief,
                                   brief_text="TASK")
    assert argv[0] == "cursor-agent"
    assert "-p" in argv and "--yolo" in argv and "--trust" in argv
    assert argv[-1] == "TASK"

    hermes = oprun.build_worker_argv("hermes", model=None, brief_path=brief, brief_text="TASK")
    assert "--query-file" in hermes
    assert str(brief) in hermes
    assert hermes[-1] == str(brief), "hermes takes its brief by file, not as a prompt"

    with pytest.raises(KeyError):
        oprun.build_worker_argv("claude", model=None, brief_path=brief, brief_text="TASK")


def test_harness_aliases_resolve_to_the_registry_id(tmp_path: Path) -> None:
    import oprun

    assert oprun.canonical_harness("cursor") == "cursor-agent"
    assert oprun.canonical_harness("cursor-agent") == "cursor-agent"


# --- probe -------------------------------------------------------------------
def test_probe_prints_exactly_one_verdict_word_for_a_no_sidecar_lane(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    worktree = fixture["tmp"] / "wt"
    worktree.mkdir()
    lane = f"cli-probe-{uuid.uuid4().hex[:8]}"
    Ledger(fixture["ledger"]).init_lane(lane, harness="cursor-agent", worktree=str(worktree),
                                       test_cmd=["true"])

    proc = run_cli("probe", lane, "--ledger", str(fixture["ledger"]))
    assert proc.returncode == 0, proc.stderr
    words = proc.stdout.split()
    assert len(words) == 1, f"probe stdout must be one word, got {proc.stdout!r}"
    assert words[0] in probe_mod.VERDICTS, words[0]


def test_probe_json_carries_the_full_document(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    worktree = fixture["tmp"] / "wt"
    worktree.mkdir()
    lane = f"cli-probe-json-{uuid.uuid4().hex[:8]}"
    Ledger(fixture["ledger"]).init_lane(lane, harness="cursor-agent", worktree=str(worktree),
                                       test_cmd=["true"])

    proc = run_cli("probe", lane, "--json", "--ledger", str(fixture["ledger"]))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["verdict"] in probe_mod.VERDICTS
    assert isinstance(result["reason"], str) and result["reason"]
    assert isinstance(result["evidence"], dict)
    assert result["evidence"]["sidecar_present"] is False


def test_probe_uses_the_probe_module_not_a_local_copy(tmp_path: Path) -> None:
    """`probe` must call `probe.probe()` — the shipped witness — with no systemd lane state."""
    fixture = init_mission(tmp_path)
    worktree = fixture["tmp"] / "wt"
    worktree.mkdir()
    lane = f"cli-probe-witness-{uuid.uuid4().hex[:8]}"
    Ledger(fixture["ledger"]).init_lane(lane, harness="cursor-agent", worktree=str(worktree),
                                       test_cmd=["true"])

    import oprun

    captured = {}
    real = oprun.probe_mod.probe

    def spy(lane_record, *, unit_active, timeout_exceeded, sidecar_dir=None):
        captured["unit_active"] = unit_active
        captured["timeout_exceeded"] = timeout_exceeded
        return real(lane_record, unit_active=unit_active, timeout_exceeded=timeout_exceeded,
                    sidecar_dir=sidecar_dir)

    oprun.probe_mod.probe = spy
    try:
        rc = oprun.main(["probe", lane, "--json", "--ledger", str(fixture["ledger"])])
    finally:
        oprun.probe_mod.probe = real
    assert rc == 0
    assert captured == {"unit_active": False, "timeout_exceeded": False}


# --- status ------------------------------------------------------------------
def test_status_json_round_trips_against_the_ledger_file(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path, "--approve", "push")
    led = Ledger(fixture["ledger"])
    for lane in ("one", "two"):
        led.init_lane(lane, harness="cursor-agent", worktree=str(fixture["repo"]),
                      test_cmd=["true"])

    proc = run_cli("status", "--json", "--ledger", str(fixture["ledger"]))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    document = read_ledger(fixture["ledger"])

    assert payload["lanes"] == document["lanes"]
    assert payload["approvals"] == document["approvals"]
    assert payload["counts"] == {"pending": 2}
    assert payload["nextAction"].startswith("dispatch")
    assert payload["mission"] == "cli-test"


def test_status_says_none_when_every_lane_is_completed(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    led = Ledger(fixture["ledger"])
    for lane in ("one", "two"):
        led.init_lane(lane, harness="cursor-agent", worktree=str(fixture["repo"]),
                      test_cmd=["true"])
        dispatch_id = led.dispatch(lane)
        led.settle(lane, dispatch_id, True)

    proc = run_cli("status", "--json", "--ledger", str(fixture["ledger"]))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["nextAction"] == "none"
    assert payload["counts"] == {"completed": 2}


def test_status_text_prints_lanes_counts_and_next_action(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    led = Ledger(fixture["ledger"])
    led.init_lane("alpha", harness="codex", worktree=str(fixture["repo"]), test_cmd=["true"])

    proc = run_cli("status", "--ledger", str(fixture["ledger"]))
    assert proc.returncode == 0, proc.stderr
    assert "alpha" in proc.stdout
    assert "pending=1" in proc.stdout
    assert "nextAction: dispatch alpha" in proc.stdout


# --- settle ------------------------------------------------------------------
def test_settle_needs_review_parks_the_lane_without_committing(tmp_path: Path) -> None:
    fixture = make_repo(tmp_path)
    git(fixture["repo"], "init", "-q", "-b", "main")
    (fixture["repo"] / "base.txt").write_text("base\n", encoding="utf-8")
    git(fixture["repo"], "add", "-A")
    git(fixture["repo"], "commit", "-q", "-m", "base")
    (fixture["repo"] / "work.txt").write_text("wip\n", encoding="utf-8")
    assert run_cli("init", str(fixture["repo"]), "--mission", "m").returncode == 0

    led = Ledger(fixture["ledger"])
    led.init_lane("alpha", harness="cursor-agent", worktree=str(fixture["repo"]),
                  test_cmd=["true"])
    dispatch_id = led.dispatch("alpha")
    head = git(fixture["repo"], "rev-parse", "HEAD")

    proc = run_cli("settle", "alpha", "--needs-review", "flaky test",
                   "--ledger", str(fixture["ledger"]))
    assert proc.returncode == 0, proc.stderr

    lane = read_ledger(fixture["ledger"])["lanes"]["alpha"]
    assert lane["status"] == "failed"
    assert lane["needs_review_reason"] == "flaky test"
    assert lane["evidence"]["reason"] == "flaky test"
    assert lane["evidence"]["verdict"] == "needs_review"
    assert git(fixture["repo"], "rev-parse", "HEAD") == head, "needs_review must not commit"
    # `accepted` is the ledger's word for "this dispatch token was consumed", not "succeeded":
    # the parked lane holds the consumed token and no rejection.
    assert lane["accepted"] == [dispatch_id]
    assert lane["rejected"] == []
    assert dispatch_id == "alpha-d1"


def test_settle_needs_review_requires_a_reason(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    led = Ledger(fixture["ledger"])
    led.init_lane("alpha", harness="cursor-agent", worktree=str(fixture["repo"]),
                  test_cmd=["true"])
    led.dispatch("alpha")

    for blank in ("", "   "):
        proc = run_cli("settle", "alpha", "--needs-review", blank,
                       "--ledger", str(fixture["ledger"]))
        assert proc.returncode != 0, blank
        assert "Traceback" not in proc.stderr
        assert "reason" in proc.stderr.lower()
    # the lane is untouched by the refused park
    assert read_ledger(fixture["ledger"])["lanes"]["alpha"]["status"] == "dispatched"


def test_settle_accept_refuses_a_lane_that_is_not_in_flight(tmp_path: Path) -> None:
    fixture = init_mission(tmp_path)
    Ledger(fixture["ledger"]).init_lane("alpha", harness="cursor-agent",
                                       worktree=str(fixture["repo"]), test_cmd=["true"])
    proc = run_cli("settle", "alpha", "--accept", "--ledger", str(fixture["ledger"]))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "not dispatched" in proc.stderr


# --- no daemon, no scheduler, no model in the completion path ----------------
def test_the_cli_contains_no_daemon_and_no_model_call() -> None:
    source = CLI.read_text(encoding="utf-8")
    for banned in ("hermes kanban", "nohup", "crontab", "Timer(", "schedule.every",
                   "openai", "anthropic", "litellm", "chat.completions", "import requests"):
        assert banned not in source, f"oprun.py must not contain {banned!r}"
    # probe --wait polls, but every loop must be bounded by a deadline
    assert "--wait" in source and "deadline" in source


def test_the_cli_never_shells_out_to_herdr_or_tmux() -> None:
    source = CLI.read_text(encoding="utf-8")
    for banned in ("herdr", "tmux", "screen "):
        assert banned not in source, f"oprun.py must not reference {banned!r}"


def test_dispatch_uses_launch_launch_and_never_a_shell_background() -> None:
    source = CLI.read_text(encoding="utf-8")
    assert "launch.launch(" in source
    assert "Popen" not in source, "the detach story is launch.launch(), not a shell job"
    assert "shell=True" not in source
    assert shutil.which("systemd-run") is not None
