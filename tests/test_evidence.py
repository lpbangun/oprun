"""The git-evidence contract, asserted on the **shipping acceptance paths**.

``scripts/advance.py`` (unattended) and ``oprun settle --accept`` (attended) both decide a lane from
the same question — *what did this lane actually produce?* — so they must answer it identically.

The defect these tests freeze out: ``advance`` had its own copy of the git-evidence builder, and
that copy recorded ``git rev-parse HEAD`` as the lane's ``commit`` even when the lane still had
uncommitted work. For a lane cut from the shared base, HEAD *is* the base commit, so three parallel
lanes all recorded the same SHA as their "commit" and their evidence was indistinguishable — a lie
in the ledger, not a gap. The rule now: ``commit`` is the lane's OWN commit or ``None``, decided by
comparing the worktree's HEAD against the commit the lane was dispatched from.

No mocks: every case runs real ``git`` in a real repository with a real linked worktree, and drives
the real acceptance path (``advance.advance`` / the ``settle`` CLI), because a helper tested in
isolation is exactly how the two paths drifted apart in the first place.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "oprun.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import advance  # noqa: E402  (the unattended acceptance path)
from ledger import Ledger  # noqa: E402  (the ledger + the one git-evidence builder)

#: Every git call runs with its own identity and no host config, so a machine without a
#: ``~/.gitconfig`` ``[user]`` cannot change what these tests observe.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "oprun evidence test",
    "GIT_AUTHOR_EMAIL": "oprun@test.local",
    "GIT_COMMITTER_NAME": "oprun evidence test",
    "GIT_COMMITTER_EMAIL": "oprun@test.local",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}

LANE_HARNESS = "cursor-agent"
LANE_MODEL = "cursor-grok-4.6"
#: The lane's own test command, re-run by BOTH acceptance paths before they accept anything.
TEST_PASS = [sys.executable, "-c", "raise SystemExit(0)"]


# --- helpers -----------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    """Run git and insist it worked: a broken fixture must be loud, never a silent pass."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=GIT_ENV)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stdout}{proc.stderr}"
    return proc.stdout.strip()


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI with stdin closed: an approval prompt may never block a test."""
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=180, env=GIT_ENV)


def _sidecar(worktree: Path, dispatch_id: str, lane_id: str) -> Path:
    """The artifact a worker would leave: written atomically, at the exact path, claiming the run."""
    directory = worktree / advance.SIDECAR_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "task_id": lane_id, "dispatch_id": dispatch_id,
               "status": "success", "harness": LANE_HARNESS, "model": LANE_MODEL, "exit_code": 0}
    target = directory / f"{advance.SIDECAR_PREFIX}{dispatch_id}{advance.SIDECAR_SUFFIX}"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return target


def _mission(tmp_path: Path, lane_id: str = "alpha", *, branch: str = "alpha") -> dict:
    """A real repo + base commit + a lane worktree cut from it + a dispatched lane.

    ``.oprun/`` is ignored in the base commit, so the lane's sidecar never shows up as uncommitted
    work and the tests observe only the lane's own edits.
    """
    repo = tmp_path / f"repo-{lane_id}"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".oprun/\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD")

    worktree = tmp_path / f"wt-{lane_id}"
    git(repo, "worktree", "add", "-q", str(worktree), "-b", branch)

    ledger_path = repo / ".tmp" / "oprun" / "state.json"
    ledger = Ledger(ledger_path)
    ledger.init_lane(lane_id, harness=LANE_HARNESS, worktree=str(worktree),
                     test_cmd=list(TEST_PASS), model=LANE_MODEL)
    dispatch_id = ledger.dispatch(lane_id)
    return {"repo": repo, "worktree": worktree, "ledger_path": ledger_path,
            "ledger": ledger, "base_sha": base_sha, "dispatch_id": dispatch_id, "lane": lane_id}


def _lane_evidence(ledger_path: Path, lane_id: str) -> dict:
    """The evidence the ledger holds for a lane — read from disk, exactly as a reviewer sees it."""
    document = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    return document["lanes"][lane_id]["evidence"]


# --- 1. the unattended path: uncommitted work names no commit ----------------
def test_advance_records_no_commit_for_a_lane_that_did_not_commit(tmp_path: Path) -> None:
    mission = _mission(tmp_path)
    worktree, base_sha = mission["worktree"], mission["base_sha"]

    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")   # worker edits, no commit
    _sidecar(worktree, mission["dispatch_id"], mission["lane"])

    summary = advance.advance(mission["ledger_path"], timeout=10, poll=0.05,
                              emit=lambda line: None)
    assert summary["accepted"] == ["alpha"], summary

    evidence = _lane_evidence(mission["ledger_path"], "alpha")
    assert evidence["uncommitted"] is True
    assert evidence["hashes"], "uncommitted work must be identified by content hashes"
    assert "lane.txt" in evidence["hashes"]
    # `base_sha` is what `git rev-parse HEAD` answers inside this worktree, because the lane never
    # committed. Recording it as the lane's `commit` is the defect: it names the shared base as the
    # lane's work, which is what made three parallel lanes' evidence indistinguishable.
    assert evidence.get("commit") is None
    assert evidence.get("commit") != base_sha
    # The mechanism that makes the verdict possible at all: the lane's dispatch anchored the commit
    # it started from, so "the lane committed" (HEAD moved) is distinguishable from "the lane is
    # still sitting on the base" (HEAD unmoved).
    assert mission["ledger"].lane("alpha").get("base_commit") == base_sha


# --- 2. the unattended path: a lane that DID commit names its own commit ------
def test_advance_records_the_lanes_own_commit_when_it_made_one(tmp_path: Path) -> None:
    mission = _mission(tmp_path)
    worktree, base_sha = mission["worktree"], mission["base_sha"]

    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "lane work")
    lane_sha = git(worktree, "rev-parse", "HEAD")
    assert lane_sha != base_sha, "the lane must have moved HEAD off the base commit"
    _sidecar(worktree, mission["dispatch_id"], mission["lane"])   # ignored: tree stays clean

    summary = advance.advance(mission["ledger_path"], timeout=10, poll=0.05,
                              emit=lambda line: None)
    assert summary["accepted"] == ["alpha"], summary

    evidence = _lane_evidence(mission["ledger_path"], "alpha")
    # The over-correction guard: an always-None `commit` is not a fix, it is a different lie.
    assert evidence["commit"] == lane_sha
    assert evidence["commit"] != base_sha
    assert evidence["uncommitted"] is False
    assert evidence["hashes"] == {}


# --- 3. both acceptance paths agree about the same worktree state ------------
def test_settle_and_advance_produce_the_same_evidence_for_the_same_state(tmp_path: Path) -> None:
    """The divergence guard: the attended and unattended paths must not drift apart again.

    Same branch name, same base content, same uncommitted edit in two real repositories — one
    accepted by ``advance``, the other by ``settle --accept`` — then compare what each recorded.
    """
    advanced = _mission(tmp_path / "advanced")
    settled = _mission(tmp_path / "settled")
    for mission in (advanced, settled):
        (mission["worktree"] / "lane.txt").write_text("lane work\n", encoding="utf-8")
        _sidecar(mission["worktree"], mission["dispatch_id"], mission["lane"])

    summary = advance.advance(advanced["ledger_path"], timeout=10, poll=0.05,
                              emit=lambda line: None)
    assert summary["accepted"] == ["alpha"], summary

    proc = run_cli("settle", "alpha", "--accept", "--ledger", str(settled["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    from_advance = _lane_evidence(advanced["ledger_path"], "alpha")
    from_settle = _lane_evidence(settled["ledger_path"], "alpha")

    for key in ("branch", "commit", "uncommitted", "hashes"):
        assert key in from_advance, f"advance evidence is missing {key!r}"
        assert key in from_settle, f"settle evidence is missing {key!r}"
        assert from_advance[key] == from_settle[key], (
            f"the two acceptance paths disagree about {key!r}: "
            f"advance={from_advance[key]!r} settle={from_settle[key]!r}"
        )
    # ... and the shared verdict is the honest one: no commit claimed, work named by hashes.
    assert from_advance["commit"] is None and from_advance["commit"] != advanced["base_sha"]
    assert from_settle["commit"] is None and from_settle["commit"] != settled["base_sha"]
    assert from_advance["hashes"] and from_settle["hashes"]
