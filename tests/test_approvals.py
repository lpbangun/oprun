"""C7 — the approval envelope, exercised against **real git** in a scratch repo.

Every assertion here runs real ``git`` in ``tmp_path``: a real repository, a real bare remote, a
real linked worktree. Nothing is mocked — a mocked commit/push would prove nothing about the
envelope, which is exactly why the frozen benchmark scores a mock as a failure.

The lanes are registered with ``ledger.Ledger`` directly rather than through ``oprun dispatch``:
the dispatch verb launches a detached ``systemd-run --user`` unit, and these tests must not
touch live systemd. Everything the envelope governs (settle, commit, push, merge, the ask, the
refusals) goes through the CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "oprun.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ledger import Ledger  # noqa: E402

#: Every git call in this file runs with its own identity and no user/system config, so the
#: host's gitconfig can never change what the test observes.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "oprun test",
    "GIT_AUTHOR_EMAIL": "oprun@test.local",
    "GIT_COMMITTER_NAME": "oprun test",
    "GIT_COMMITTER_EMAIL": "oprun@test.local",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}

APPROVAL_KEYS = ("commit", "push", "merge", "deploy", "publish", "destructive")


# --- helpers -----------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    """Run git and insist it worked (test setup failures must be loud)."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=GIT_ENV)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def git_ok(repo: Path, *args: str) -> bool:
    """Run git and report whether it worked, for the negative assertions."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=GIT_ENV)
    return proc.returncode == 0


def run_cli(*args: str, stdin: int = subprocess.DEVNULL,
            env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the CLI with stdin **closed** by default: a prompt must never be able to block.

    The CLI's own ``git commit``/``push``/``merge`` get the same isolated identity as the setup
    helpers above (``GIT_ENV``), so a host whose ``~/.gitconfig`` carries no ``[user]`` cannot
    fail a real commit: the suite must behave identically on a clean machine.
    """
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          stdin=stdin, timeout=180, env=GIT_ENV if env is None else env)


def read_ledger(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dispatch_lane(scratch: dict, lane: str = "alpha") -> str:
    """Register + dispatch one lane in the ledger. No process is launched (no live systemd)."""
    ledger = Ledger(scratch["ledger"], max_parallel=4, failure_limit=3)
    ledger.init_lane(lane, harness="cursor-agent", worktree=str(scratch["worktree"]),
                     test_cmd=["true"], model="cursor-grok-4.6")
    return ledger.dispatch(lane)


@pytest.fixture()
def scratch(tmp_path: Path) -> dict:
    """A real repo + bare remote + linked worktree carrying one uncommitted change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")

    (tmp_path / "wt").mkdir()
    worktree = tmp_path / "wt" / "alpha"
    git(repo, "worktree", "add", "-q", str(worktree), "-b", "alpha")
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")

    return {"tmp": tmp_path, "repo": repo, "remote": remote, "worktree": worktree,
            "ledger": repo / ".tmp" / "oprun" / "state.json"}


# --- the envelope itself -----------------------------------------------------
def test_init_approve_grants_exactly_the_named_keys(scratch: dict) -> None:
    proc = run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push,merge")
    assert proc.returncode == 0, proc.stderr

    approvals = read_ledger(scratch["ledger"])["approvals"]
    assert set(approvals) == set(APPROVAL_KEYS)
    for key in ("commit", "push", "merge"):
        entry = approvals[key]
        assert entry["granted"] is True, key
        assert entry["at"] and entry["by"] == "user" and entry["scope"] == "mission", key
    for key in ("deploy", "publish", "destructive"):
        assert approvals[key] == {"granted": False}, key


def test_init_without_approve_grants_nothing_including_destructive(scratch: dict) -> None:
    proc = run_cli("init", str(scratch["repo"]), "--mission", "m")
    assert proc.returncode == 0, proc.stderr

    approvals = read_ledger(scratch["ledger"])["approvals"]
    assert set(approvals) == set(APPROVAL_KEYS)
    for key in APPROVAL_KEYS:
        assert approvals[key] == {"granted": False}, key
    assert approvals["destructive"]["granted"] is False


def test_init_approve_is_repeatable_and_comma_separated(scratch: dict) -> None:
    proc = run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push", "--approve", "merge")
    assert proc.returncode == 0, proc.stderr
    approvals = read_ledger(scratch["ledger"])["approvals"]
    assert [key for key in APPROVAL_KEYS if approvals[key]["granted"]] == ["commit", "push", "merge"]


def test_init_refuses_an_unknown_approval_key(scratch: dict) -> None:
    proc = run_cli("init", str(scratch["repo"]), "--mission", "m", "--approve", "commit,root")
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "root" in proc.stderr


def test_approve_records_at_by_and_can_revoke(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m").returncode == 0

    granted = run_cli("approve", "--grant", "commit,push", "--ledger", str(scratch["ledger"]))
    assert granted.returncode == 0, granted.stderr
    approvals = read_ledger(scratch["ledger"])["approvals"]
    assert approvals["commit"]["granted"] is True
    assert approvals["commit"]["at"] and approvals["commit"]["by"] == "user"
    assert approvals["push"]["granted"] is True
    assert "commit" in granted.stdout and "granted" in granted.stdout

    revoked = run_cli("approve", "--revoke", "push", "--ledger", str(scratch["ledger"]))
    assert revoked.returncode == 0, revoked.stderr
    approvals = read_ledger(scratch["ledger"])["approvals"]
    assert approvals["push"] == {"granted": False}
    assert approvals["commit"]["granted"] is True, "revoking push must not touch commit"


def test_approve_refuses_an_unknown_key_and_changes_nothing(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m").returncode == 0
    before = read_ledger(scratch["ledger"])

    proc = run_cli("approve", "--grant", "bogus", "--ledger", str(scratch["ledger"]))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert read_ledger(scratch["ledger"]) == before, "a refused approval must be inert"


# --- granted envelope: settle acts, with no prompt ---------------------------
def test_granted_envelope_settles_commit_push_merge_without_any_prompt(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push,merge").returncode == 0
    dispatch_id = dispatch_lane(scratch)
    base = git(scratch["worktree"], "rev-parse", "HEAD")

    proc = run_cli("settle", "alpha", "--accept", "--ledger", str(scratch["ledger"]))
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "Approve now?" not in combined, "a granted envelope must never ask"
    assert "needs commit" not in combined

    sha = git(scratch["worktree"], "rev-parse", "HEAD")
    assert sha != base, "settle --accept must commit the lane's work"
    assert git(scratch["remote"], "rev-parse", "refs/heads/alpha") == sha, "must have pushed"
    assert git(scratch["repo"], "rev-parse", "main") == sha, "must have merged into main"

    lane = read_ledger(scratch["ledger"])["lanes"]["alpha"]
    assert lane["status"] == "completed"
    assert lane["accepted"] == [dispatch_id]
    evidence = lane["evidence"]
    assert evidence["commit"] == sha
    assert evidence["push"]["sha"] == sha
    assert evidence["push"]["remote"] == "origin"
    assert evidence["merge"]["into"] == "main"
    assert evidence["approval_prompt"] is None
    assert evidence["test_exit_code"] == 0


def test_settle_commits_with_no_usable_identity_in_the_cli_environment(scratch: dict) -> None:
    """A host gitconfig without ``[user]`` must not be able to fail the CLI's real commit.

    The child environment is deliberately stripped: no global/system config and every
    ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``/``GIT_CONFIG*`` the host might export is scrubbed before
    the test's own identity goes back in — so the commit the CLI runs can only be authorised by
    ``GIT_ENV``, never by whatever the machine happens to hold.
    """
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push,merge").returncode == 0
    dispatch_id = dispatch_lane(scratch)
    base = git(scratch["worktree"], "rev-parse", "HEAD")

    stripped = {key: value for key, value in os.environ.items()
                if key not in {"EMAIL", "GIT_ASKPASS"}
                and not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG"))}
    stripped.update({
        "GIT_AUTHOR_NAME": "oprun test",
        "GIT_AUTHOR_EMAIL": "oprun@test.local",
        "GIT_COMMITTER_NAME": "oprun test",
        "GIT_COMMITTER_EMAIL": "oprun@test.local",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })

    proc = run_cli("settle", "alpha", "--accept", "--ledger", str(scratch["ledger"]),
                   env=stripped)
    assert proc.returncode == 0, proc.stderr

    sha = git(scratch["worktree"], "rev-parse", "HEAD")
    assert sha != base, "the CLI's own git commit must succeed with no host-held identity"
    lane = read_ledger(scratch["ledger"])["lanes"]["alpha"]
    assert lane["status"] == "completed"
    assert lane["accepted"] == [dispatch_id]
    assert lane["evidence"]["commit"] == sha, "the commit SHA must be recorded in the evidence"


def test_accept_refuses_when_the_lane_test_is_red(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push,merge").returncode == 0
    ledger = Ledger(scratch["ledger"], max_parallel=4, failure_limit=3)
    ledger.init_lane("red", harness="cursor-agent", worktree=str(scratch["worktree"]),
                     test_cmd=["false"], model="cursor-grok-4.6")
    ledger.dispatch("red")
    base = git(scratch["worktree"], "rev-parse", "HEAD")

    proc = run_cli("settle", "red", "--accept", "--ledger", str(scratch["ledger"]))
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert git(scratch["worktree"], "rev-parse", "HEAD") == base
    assert read_ledger(scratch["ledger"])["lanes"]["red"]["status"] == "dispatched"


# --- envelope absent: ask exactly once, and closed stdin means no -------------
def test_absent_envelope_asks_exactly_once_and_takes_no_for_an_answer(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m").returncode == 0
    dispatch_id = dispatch_lane(scratch)
    base = git(scratch["worktree"], "rev-parse", "HEAD")

    proc = run_cli("settle", "alpha", "--accept", "--ledger", str(scratch["ledger"]))
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert combined.count("Approve now?") == 1, f"must ask exactly once, asked: {combined!r}"

    assert git(scratch["worktree"], "rev-parse", "HEAD") == base, "a 'no' must not commit"
    assert not git_ok(scratch["remote"], "rev-parse", "--verify", "refs/heads/alpha")

    lane = read_ledger(scratch["ledger"])["lanes"]["alpha"]
    assert lane["status"] == "completed"
    evidence = lane["evidence"]
    assert evidence["commit"] is None
    assert evidence["push"] is None
    assert evidence["approval_prompt"]["answer"] == "no"
    assert set(evidence["approval_prompt"]["asked"]) == {"commit", "push", "merge"}
    assert lane["accepted"] == [dispatch_id]
    assert read_ledger(scratch["ledger"])["approvals"]["commit"] == {"granted": False}


# --- no implicit widening ----------------------------------------------------
def test_a_push_grant_does_not_widen_to_force_tag_release_rewrite_delete_or_other_repo(
        scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push,merge").returncode == 0
    dispatch_lane(scratch)
    base = git(scratch["worktree"], "rev-parse", "HEAD")
    other = scratch["tmp"] / "other.git"
    git(scratch["tmp"], "init", "-q", "--bare", str(other))

    cases = {
        "force-push": ["--force"],
        "tag": ["--tag", "v1"],
        "release": ["--release", "--tag", "v1"],
        "history rewrite": ["--rewrite-history"],
        "branch delete": ["--delete-branch", "alpha"],
        "other repo": ["--repo", str(other)],
    }
    for label, flags in cases.items():
        proc = run_cli("settle", "alpha", "--accept", *flags, "--ledger", str(scratch["ledger"]))
        assert proc.returncode != 0, f"{label} was not refused: {proc.stdout}"
        assert "Traceback" not in proc.stderr
        assert (proc.stdout + proc.stderr).strip(), f"{label}: refusal must say something"
        # refused BEFORE any mutation, so the lane is untouched and still in flight
        assert git(scratch["worktree"], "rev-parse", "HEAD") == base, label
        assert read_ledger(scratch["ledger"])["lanes"]["alpha"]["status"] == "dispatched", label

    assert not git_ok(scratch["remote"], "rev-parse", "--verify", "refs/heads/alpha")
    assert not git_ok(scratch["remote"], "rev-parse", "--verify", "refs/tags/v1")
    assert not git_ok(other, "rev-parse", "--verify", "refs/heads/alpha")


def test_a_fresh_grant_authorises_the_widening(scratch: dict) -> None:
    """`each needs a fresh approve --grant` — and then the action really happens."""
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push,merge").returncode == 0
    dispatch_lane(scratch)

    refused = run_cli("settle", "alpha", "--accept", "--tag", "v1",
                      "--ledger", str(scratch["ledger"]))
    assert refused.returncode != 0
    assert not git_ok(scratch["remote"], "rev-parse", "--verify", "refs/tags/v1")

    granted = run_cli("approve", "--grant", "publish", "--ledger", str(scratch["ledger"]))
    assert granted.returncode == 0, granted.stderr

    proc = run_cli("settle", "alpha", "--accept", "--tag", "v1", "--ledger", str(scratch["ledger"]))
    assert proc.returncode == 0, proc.stderr
    sha = git(scratch["worktree"], "rev-parse", "HEAD")
    assert git(scratch["remote"], "rev-parse", "refs/tags/v1") == sha


def test_destructive_is_never_defaultable_but_can_be_named_explicitly(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "destructive").returncode == 0
    approvals = read_ledger(scratch["ledger"])["approvals"]
    assert approvals["destructive"]["granted"] is True
    assert approvals["push"] == {"granted": False}
    # and it is grantable later, deliberately, by name
    assert run_cli("approve", "--grant", "destructive", "--ledger",
                   str(scratch["ledger"])).returncode == 0


# --- the envelope is visible -------------------------------------------------
def test_status_prints_the_active_envelope(scratch: dict) -> None:
    assert run_cli("init", str(scratch["repo"]), "--mission", "m",
                   "--approve", "commit,push").returncode == 0

    text = run_cli("status", "--ledger", str(scratch["ledger"]))
    assert text.returncode == 0, text.stderr
    for key in APPROVAL_KEYS:
        assert key in text.stdout, key
    assert "granted" in text.stdout
    assert "NOT granted" in text.stdout

    machine = run_cli("status", "--json", "--ledger", str(scratch["ledger"]))
    assert machine.returncode == 0, machine.stderr
    payload = json.loads(machine.stdout)
    assert payload["approvals"] == read_ledger(scratch["ledger"])["approvals"]
    assert payload["approvals"]["push"]["granted"] is True
    assert payload["approvals"]["destructive"] == {"granted": False}
