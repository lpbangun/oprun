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

The last section freezes the two git side effects issue #2 named: ``git add -A`` shipped the lane's
own ``.oprun/`` sidecars into the product commit, and ``merge`` went into whatever the primary
worktree happened to have checked out. Both are asserted on the commits and branches that really
exist afterwards, never on what the CLI printed about them.
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


def _mission(tmp_path: Path, lane_id: str = "alpha", *, branch: str = "alpha",
             ignore_evidence: bool = True, merge_into: str | None = None) -> dict:
    """A real repo + base commit + a lane worktree cut from it + a dispatched lane.

    ``.oprun/`` is ignored in the base commit *by default*, so a lane's sidecar never shows up as
    uncommitted work and the tests observe only the lane's own edits. ``ignore_evidence=False``
    builds the other case on purpose: a mission repo with **no** ignore rule at all, which is what a
    lane worktree usually is — the CLI cannot rely on a ``.gitignore`` being there, so the staging
    rule has to live in the code (issue #2).

    ``merge_into`` is the branch recorded by ``dispatch --into`` (``None`` = not recorded).
    """
    repo = tmp_path / f"repo-{lane_id}"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    if ignore_evidence:
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
                     test_cmd=list(TEST_PASS), model=LANE_MODEL, merge_into=merge_into)
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


# --- 4. the settle commit holds the lane's work and NONE of its evidence -----
def _lane_record(ledger_path: Path, lane_id: str) -> dict:
    """The whole lane record, read from disk — what a reviewer (and ``status``) sees."""
    return json.loads(Path(ledger_path).read_text(encoding="utf-8"))["lanes"][lane_id]


def _committed_paths(worktree: Path, rev: str = "HEAD") -> list[str]:
    """The paths a commit actually recorded, read off the commit itself."""
    return [line for line in git(worktree, "show", "--name-only", "--format=", rev).splitlines()
            if line.strip()]


def _is_evidence(path: str) -> bool:
    """The staging rule under test, spelled out here so the test cannot inherit a bug from it."""
    return (any(part in (".oprun", "__pycache__", ".pytest_cache") for part in path.split("/"))
            or path.endswith(".pyc"))


def _grant(mission: dict, *keys: str) -> None:
    """Record the envelope through the real CLI, exactly as an operator would."""
    proc = run_cli("approve", "--grant", ",".join(keys), "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_settle_commit_contains_no_lane_evidence_path(tmp_path: Path) -> None:
    """Issue #2: ``git add -A`` shipped ``<worktree>/.oprun/`` — the ledger's own review record.

    The fixture deliberately has **no** ignore rule for ``.oprun/``: a lane worktree belongs to the
    mission repo, so the CLI cannot assume a ``.gitignore`` covers the sidecar dir. The commit must
    hold the lane's work and no evidence path at all.
    """
    mission = _mission(tmp_path, ignore_evidence=False)
    worktree, lane = mission["worktree"], mission["lane"]
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    sidecar = _sidecar(worktree, mission["dispatch_id"], lane)
    cache = worktree / "pkg" / "__pycache__" / "mod.cpython-312.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_text("bytecode\n", encoding="utf-8")

    _grant(mission, "commit")
    proc = run_cli("settle", lane, "--accept", "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    committed = _committed_paths(worktree)
    assert "lane.txt" in committed, committed
    assert [path for path in committed if _is_evidence(path)] == [], committed
    # the evidence is still on disk: it IS evidence, it is simply not product
    assert sidecar.is_file() and cache.is_file()

    evidence = _lane_evidence(mission["ledger_path"], lane)
    assert evidence["commit"] == git(worktree, "rev-parse", "HEAD")
    assert evidence["commit"] != mission["base_sha"]
    assert evidence["commit_paths"] == ["lane.txt"], evidence["commit_paths"]
    excluded = evidence["commit_excluded"] or []
    assert any(path.startswith(".oprun/") for path in excluded), excluded
    assert any(path.endswith("mod.cpython-312.pyc") for path in excluded), excluded


def test_settle_commit_unstages_evidence_a_worker_already_staged(tmp_path: Path) -> None:
    """A worker's own ``git add -A`` must not smuggle its sidecar past the staging rule."""
    mission = _mission(tmp_path, ignore_evidence=False)
    worktree, lane = mission["worktree"], mission["lane"]
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    _sidecar(worktree, mission["dispatch_id"], lane)
    git(worktree, "add", "-A")                       # the sloppy worker stages everything
    staged_before = git(worktree, "diff", "--cached", "--name-only")
    assert ".oprun/" in staged_before, staged_before

    _grant(mission, "commit")
    proc = run_cli("settle", lane, "--accept", "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    committed = _committed_paths(worktree)
    assert committed == ["lane.txt"], committed
    assert [path for path in committed if _is_evidence(path)] == []


def test_settle_commit_keeps_a_staged_rename(tmp_path: Path) -> None:
    """``status -z`` reports a rename as TWO records (new name, then original): only the new name
    is a path to stage, and the sidecar must still be dropped from the same list."""
    mission = _mission(tmp_path, ignore_evidence=False)
    worktree, lane = mission["worktree"], mission["lane"]
    (worktree / "base.txt").rename(worktree / "base-renamed.txt")
    git(worktree, "add", "base-renamed.txt", "base.txt")      # stages the rename in the index
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    _sidecar(worktree, mission["dispatch_id"], lane)
    assert git(worktree, "status", "--porcelain", "base.txt")        # the original is gone
    _grant(mission, "commit")

    proc = run_cli("settle", lane, "--accept", "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    committed = sorted(_committed_paths(worktree))
    assert committed == ["base-renamed.txt", "lane.txt"], committed
    assert [path for path in committed if _is_evidence(path)] == []


def test_settle_into_mismatch_refuses_before_any_mutation(tmp_path: Path) -> None:
    """A settle may repeat the target dispatch recorded — never quietly replace it."""
    mission = _mission(tmp_path, ignore_evidence=False, merge_into="release")
    worktree, lane = mission["worktree"], mission["lane"]
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    _sidecar(worktree, mission["dispatch_id"], lane)
    _grant(mission, "commit", "merge")
    head_before = git(worktree, "rev-parse", "HEAD")

    proc = run_cli("settle", lane, "--accept", "--into", "other",
                   "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 2, proc.stdout + proc.stderr       # EXIT_USAGE: a retarget, not a merge
    assert "release" in proc.stderr and "other" in proc.stderr, proc.stderr

    # nothing moved: no commit, no index entry, no ledger write
    assert git(worktree, "rev-parse", "HEAD") == head_before
    assert "lane.txt" in git(worktree, "status", "--porcelain")
    record = _lane_record(mission["ledger_path"], lane)
    assert record["status"] == "dispatched"
    assert record["merge_into"] == "release"
    assert record["accepted"] == []


def test_settle_refuses_when_the_primary_checkout_is_not_the_recorded_target(tmp_path: Path) -> None:
    """The recorded target is enforced against the real checkout, by name, before any mutation."""
    mission = _mission(tmp_path, branch="lane/alpha", ignore_evidence=False, merge_into="release")
    worktree, lane, repo = mission["worktree"], mission["lane"], mission["repo"]
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    _sidecar(worktree, mission["dispatch_id"], lane)
    _grant(mission, "commit", "merge")
    head_before = git(worktree, "rev-parse", "HEAD")

    proc = run_cli("settle", lane, "--accept", "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 4, proc.stdout + proc.stderr       # EXIT_REFUSED
    # the refusal names BOTH branches: the target, and what is actually checked out
    assert "release" in proc.stderr and "main" in proc.stderr, proc.stderr
    # the target is printed before the refusal, so the operator sees what was about to happen
    assert "merge target: release" in proc.stdout, proc.stdout

    assert git(worktree, "rev-parse", "HEAD") == head_before, "no commit may precede the refusal"
    assert git(repo, "rev-parse", "HEAD") == mission["base_sha"], "the primary must be untouched"
    assert _lane_record(mission["ledger_path"], lane)["status"] == "dispatched"


def test_a_wrong_checkout_does_not_block_a_settle_that_will_not_merge(tmp_path: Path) -> None:
    """The target gates the MERGE, nothing else.

    A settle whose envelope does not grant ``merge`` performs no merge, so a primary worktree on
    some other branch must not block the commit+push the envelope does grant. Coupling them would
    make an unrequested merge silently veto the lane's own work.
    """
    mission = _mission(tmp_path, branch="lane/alpha", ignore_evidence=False, merge_into="release")
    worktree, lane = mission["worktree"], mission["lane"]
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    _sidecar(worktree, mission["dispatch_id"], lane)
    _grant(mission, "commit")                     # commit only: no merge can happen

    proc = run_cli("settle", lane, "--accept", "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "merge target: release" in proc.stdout, proc.stdout

    evidence = _lane_evidence(mission["ledger_path"], lane)
    assert evidence["commit"] == git(worktree, "rev-parse", "HEAD")
    assert evidence["merge"] is None, "a merge that was never granted must not be reported"
    assert _committed_paths(worktree) == ["lane.txt"]


def test_settle_merges_into_the_recorded_target_only(tmp_path: Path) -> None:
    """The happy path the target exists for: lane branch -> the branch it was dispatched into."""
    mission = _mission(tmp_path, branch="lane/alpha", ignore_evidence=False, merge_into="main")
    worktree, lane, repo = mission["worktree"], mission["lane"], mission["repo"]
    (worktree / "lane.txt").write_text("lane work\n", encoding="utf-8")
    _sidecar(worktree, mission["dispatch_id"], lane)
    _grant(mission, "commit", "merge")

    proc = run_cli("settle", lane, "--accept", "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "merge target: main" in proc.stdout, proc.stdout

    evidence = _lane_evidence(mission["ledger_path"], lane)
    assert evidence["merge_target"] == "main"
    assert evidence["merge"]["ok"] is True and evidence["merge"]["into"] == "main"
    # the merge is real: the primary branch now holds the lane's commit and its file
    assert git(repo, "rev-parse", "HEAD") == evidence["commit"]
    assert (repo / "lane.txt").is_file()
    assert _lane_record(mission["ledger_path"], lane)["merge_into"] == "main"


def test_status_reads_a_parked_lane_as_parked_not_failed(tmp_path: Path) -> None:
    """What a conductor reads: a lane waiting for a decision, never the word ``failed``."""
    mission = _mission(tmp_path)                       # --needs-review mutates no git state
    proc = run_cli("settle", mission["lane"], "--needs-review", "flaky test",
                   "--ledger", str(mission["ledger_path"]))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    text = run_cli("status", "--ledger", str(mission["ledger_path"]))
    assert text.returncode == 0, text.stderr
    assert "parked" in text.stdout
    assert "failed" not in text.stdout, text.stdout
    assert "flaky test" in text.stdout, "a parked lane must say why it is waiting"

    payload = json.loads(run_cli("status", "--json", "--ledger",
                                 str(mission["ledger_path"])).stdout)
    assert payload["counts"] == {"parked": 1}
    assert payload["display"] == {mission["lane"]: "parked"}
    # the stored state is untouched: still the terminal, fenced `failed`
    assert payload["lanes"][mission["lane"]]["status"] == "failed"
