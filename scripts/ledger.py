"""oprun v0.2 ledger — pure transition table, dispatch fencing, circuit breaker, flock.

It also hosts :func:`git_evidence`, the **one** builder of git evidence. Both acceptance paths
(``oprun settle --accept``, attended, and ``scripts/advance.py``, unattended) call it: two copies
of that rule drifted once already, and the drifted copy recorded the worktree's HEAD as the lane's
``commit`` even when the lane had never committed — which, for a lane cut from the shared base, is
the base SHA. Evidence that names the base as the lane's work is worse than evidence that names
nothing.

Shipped implementation, ported (not reinvented) from two proven references on this box:

  ``/tmp/canary3/oprun_ledger.py``  transitions + fencing + circuit breaker (10/10 green)
  ``/tmp/oprun5/oprun_ledger.py``   file locking + parallel safety (6 procs x 25 cycles)

One JSON file is the only source of truth for lane status. Writers (a conductor, a bounded
advance runner, a lane's own bookkeeping) are serialised by ``flock`` on ``<path>.lock``,
and every mutation **re-reads the file under that lock** — that single line is what makes
concurrent writers safe, because a writer that flushes its own stale snapshot would erase a
peer's committed change (the lost-update bug ``/tmp/oprun5`` was written to kill).

No daemon, no database, no second authority: stdlib only, files over RPCs.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# --- pure transition table ---------------------------------------------------
PENDING, READY, DISPATCHED, COMPLETED, FAILED, BLOCKED = (
    "pending", "ready", "dispatched", "completed", "failed", "blocked",
)
TERMINAL = {COMPLETED, FAILED, BLOCKED}

#: **Display only** — the word a reader sees for a lane that was settled as a non-acceptance
#: (``settle --needs-review``, or an ``--accept`` the controller refused). It is deliberately NOT
#: a state: it is absent from :data:`TRANSITIONS` and :data:`TERMINAL`, ``apply`` rejects it as a
#: from-state, and the lane's stored status stays ``failed`` (fenced, terminal, exactly as before).
#: The distinction is real: ``failed`` reads as "the mission broke", ``parked`` reads as "waiting
#: for a decision" — which is what the ledger actually holds.
PARKED = "parked"

TRANSITIONS: dict[tuple[str, str], str] = {
    (PENDING, "ready"): READY,
    (PENDING, "dispatch"): DISPATCHED,
    (READY, "dispatch"): DISPATCHED,
    (READY, "block"): BLOCKED,
    (DISPATCHED, "success"): COMPLETED,
    (DISPATCHED, "failure"): FAILED,
    (DISPATCHED, "block"): BLOCKED,
    (DISPATCHED, "retry"): DISPATCHED,
    # An INFRA outcome is not a failure, so it cannot be the failure transition: the attempt never
    # ran the lane's tests at all (the controller's own re-run could not start the command, or its
    # budget killed it), so nothing about the lane was proven. The lane returns to ``ready`` — the
    # ordinary dispatch gate re-enters it, and the worker is relaunched by the conductor — while the
    # failure streak it must never touch stays exactly where it was (issue #1).
    (DISPATCHED, "infra"): READY,
    (FAILED, "retry"): DISPATCHED,
    (FAILED, "block"): BLOCKED,
    (BLOCKED, "retry"): DISPATCHED,
    (BLOCKED, "unblock"): READY,
    (COMPLETED, "reopen"): READY,
}

#: Exact token a caller may grep for when nesting runs too deep (a worker spawning a
#: worker is a boundary violation, not a retryable failure).
DEPTH_EXCEEDED_TOKEN = "nested_worker_depth_exceeded"

#: How many **infra** outcomes a lane may accumulate before the ledger parks it for review.
#: Deliberately NOT the circuit breaker: ``failure_limit`` counts failing *tests*, while an infra
#: outcome means no test ever ran (the controller's own re-run could not start the command, or its
#: budget killed it). Nothing about the lane was proven, so the streak stays untouched — but a
#: permanently unrunnable command must still reach a human instead of being retried forever, and
#: this cap is how. Two: one retry after the fault is visible, then a park that names the
#: environment (issue #1).
DEFAULT_INFRA_LIMIT = 2

#: Separator class ignored by :func:`normalize_model`. Dropped, never replaced with a
#: space, so "Cursor Grok 4.6" and "cursor-grok-4.6" collapse to the same string.
MODEL_SEPARATORS = "-_ ."


class IllegalTransition(Exception):
    """Raised for a (state, event) pair the table does not allow."""


class DepthExceeded(Exception):
    """Raised when a lane would nest deeper than ``max_depth``.

    The message always carries :data:`DEPTH_EXCEEDED_TOKEN` so a controller can match on
    the token instead of parsing prose.
    """


class ParallelismExceeded(Exception):
    """Raised when a dispatch would exceed ``max_parallel`` concurrently-DISPATCHED lanes."""


def apply(state: str, event: str) -> str:
    """Pure ``(from_state, event) -> to_state``. Illegal pairs are an error, never a guess."""
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise IllegalTransition(f"illegal transition: {state!r} + {event!r}") from None


def display_state(lane: dict) -> str:
    """The word a reader sees for ``lane``: :data:`PARKED` for a settlement parked for review.

    Derived, never stored — the lane's status stays ``failed`` and every fence, transition and
    terminal-set check keeps working on the real state:

    * settled as a non-acceptance (``needs_review`` verdict: ``settle --needs-review``, or an
      ``--accept`` the controller refused on identity/evidence) -> ``parked``. The lane is
      waiting for a decision, not evidence that the mission broke.
    * anything else -> its own status. A lane ``advance`` failed for its own reasons (red test,
      unusable artifact) keeps the honest ``failed``, so the two words stay distinct.
    """
    status = str((lane or {}).get("status") or "")
    if status != FAILED:
        return status
    evidence = lane.get("evidence")
    verdict = evidence.get("verdict") if isinstance(evidence, dict) else None
    return PARKED if verdict == "needs_review" else status


def normalize_model(name: str) -> str:
    """Normalise a model pin for comparison: casefold, then drop ``[-_ .]``.

    "Cursor Grok 4.6" and "cursor-grok-4.6" normalise equal, so a re-spelled but identical
    pin is not reported as a model mismatch. Two genuinely different pins ("gpt-5.6-sol"
    vs "grok-4.6") stay different. Callers keep the raw strings too; only this comparison
    is normalised.
    """
    return "".join(ch for ch in name.casefold() if ch not in MODEL_SEPARATORS)


# --- git evidence: the ONE builder both acceptance paths call -----------------
#
# ``oprun settle --accept`` (attended) and ``scripts/advance.py`` (unattended) must describe the
# same worktree the same way, so they must not each carry their own copy of this rule. They did,
# and the copy in ``advance`` recorded ``git rev-parse HEAD`` as the lane's ``commit`` even when the
# lane still had uncommitted work — for a lane cut from the shared base that is the BASE SHA, so
# three parallel lanes' evidence was indistinguishable and the ledger named work the lane never did.
# There is one function now, and no ``commit`` it returns is ever the commit the lane started from.

#: Wall-clock ceiling for one read-only git query made while building evidence.
GIT_TIMEOUT = 30.0
#: Changed paths hashed for the uncommitted-work evidence path, so one huge lane cannot bloat the
#: ledger.
HASH_LIMIT = 50


def _git(worktree: Path, *args: str) -> tuple[int, str, str]:
    """Run one read-only git command in ``worktree``. ``(rc, stdout, stderr)`` — never raises."""
    try:
        proc = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 128, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _head(worktree: Path) -> str | None:
    """The worktree's HEAD, or ``None`` when git cannot say (never a guess)."""
    rc, out, _ = _git(worktree, "rev-parse", "HEAD")
    head = out.strip()
    return head if rc == 0 and head else None


def _current_branch(worktree: Path) -> str | None:
    """The checked-out branch name, or ``None`` when detached/unknown (never a guess)."""
    rc, out, _ = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    name = out.strip()
    return name if rc == 0 and name and name != "HEAD" else None


def _toplevel(worktree: Path) -> Path:
    """The repository root ``worktree`` belongs to (porcelain paths are relative to it)."""
    rc, out, _ = _git(worktree, "rev-parse", "--show-toplevel")
    root = out.strip()
    return Path(root) if rc == 0 and root else Path(worktree)


def _porcelain_paths(status: str) -> list[str]:
    """Changed paths from ``status --porcelain``, renames resolved to the new name.

    The output is sliced, never stripped as a whole first: the first line of a worktree-only change
    begins with a space (``" M work.txt"``), and a blanket strip turns that path into ``ork.txt`` —
    evidence that silently hashes a file that does not exist.
    """
    paths: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        paths.append(line[3:].strip().split(" -> ")[-1])
    return paths


def _content_hashes(base: Path, paths: list[str],
                    count: int = HASH_LIMIT) -> dict[str, str | None]:
    """sha256 per changed path, so uncommitted work is identifiable without a commit.

    An untracked directory (``.oprun/`` and friends) is digested too — it is part of what the lane
    produced; a path that is gone (deleted/renamed away) is honestly ``None``.
    """
    hashes: dict[str, str | None] = {}
    for rel in paths[:count]:
        target = base / rel
        try:
            if target.is_file():
                hashes[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
            elif target.is_dir():
                digest = hashlib.sha256()
                files = sorted(path for path in target.rglob("*") if path.is_file())
                for path in files[:count]:
                    digest.update(str(path.relative_to(base)).encode("utf-8"))
                    digest.update(hashlib.sha256(path.read_bytes()).digest())
                hashes[rel] = f"dir:{digest.hexdigest()}:{len(files)}files"
            else:
                hashes[rel] = None
        except OSError:
            hashes[rel] = None
    return hashes


def git_evidence(worktree: Path | str, base_commit: str | None = None) -> dict:
    """``branch`` / ``commit`` / ``uncommitted`` / ``hashes`` for a lane's worktree.

    ``commit`` is **the lane's own commit, or nothing** — never the commit the lane started from:

    * uncommitted changes present -> ``commit: None``, ``uncommitted: True``, and ``hashes``
      identifies the work. This is the common lane case: a worker edits and does not commit.
    * worktree clean and HEAD differs from ``base_commit`` (the lane's starting commit, anchored by
      :meth:`Ledger.dispatch`) -> ``commit`` is that HEAD, ``uncommitted: False``, ``hashes: {}``.
    * worktree clean, HEAD unknown or still the starting commit (the lane did nothing) ->
      ``commit: None``: without an anchor git cannot prove the HEAD is the lane's own work, so
      nothing is claimed on its behalf.
    * git unavailable or failed -> the reason is recorded as ``git_error``, ``commit: None``.

    A lane that committed and then kept working reports the uncommitted branch (``commit: None``,
    hashes for the newer work): the commit is not what the worktree currently holds.
    """
    worktree = Path(worktree)
    rc_status, status, status_err = _git(worktree, "status", "--porcelain")
    if rc_status != 0:
        return {"branch": _current_branch(worktree), "commit": None, "uncommitted": None,
                "hashes": {},
                "git_error": (status_err or status).strip() or f"git status exited {rc_status}"}
    paths = _porcelain_paths(status)
    if paths:
        return {"branch": _current_branch(worktree), "commit": None, "uncommitted": True,
                "hashes": _content_hashes(_toplevel(worktree), paths)}
    head = _head(worktree)
    committed = head if (head and base_commit and head != base_commit) else None
    return {"branch": _current_branch(worktree), "commit": committed, "uncommitted": False,
            "hashes": {}}


class Ledger:
    """Single-file ledger: many readers, one writer at a time, across processes.

    Every mutation runs inside :meth:`_locked` — an exclusive ``flock`` on
    ``<path>.lock`` plus a re-read of the JSON file before the change is applied — and every
    write is atomic (``<path>.tmp`` then :func:`os.replace`), so a reader never observes a
    half-written document. Lanes are independent records.
    """

    def __init__(self, path: str | os.PathLike[str], failure_limit: int = 3,
                 max_parallel: int = 4, max_depth: int = 1,
                 infra_limit: int = DEFAULT_INFRA_LIMIT) -> None:
        self.path = Path(path)
        self.failure_limit = failure_limit
        self.max_parallel = max_parallel
        self.max_depth = max_depth
        # The infra cap is a SEPARATE bound from the circuit breaker on purpose: `failure_limit`
        # counts red tests, `infra_limit` counts attempts whose tests never ran at all.
        self.infra_limit = infra_limit
        # "<path>.lock" / "<path>.tmp" are *appended* (contract spelling), so two ledgers
        # named state.json and state.json.bak can never end up sharing one lock file.
        self._lock_path = Path(f"{self.path}.lock")
        self._tmp_path = Path(f"{self.path}.tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Document shape carried over from /tmp/oprun5 so the CLI/approval lanes read the
        # same file without inventing a second store. Read-oriented: there is no public
        # write API; mutations go through the methods below.
        self.data: dict = {"mission": "", "approvals": {}, "lanes": {}}
        if self.path.exists():
            self._reload()

    # --- persistence --------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise writers, and re-read the file under the lock before mutating.

        The re-read is the whole point: without it each writer applies its change to a
        snapshot it read *before* a peer committed, then flushes that stale snapshot over
        the peer's write. ``/tmp/oprun5`` measured the difference (6 processes x 25
        dispatch/settle cycles, zero lost lanes) and this line is why.
        """
        with self._lock_path.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                self._reload()          # never clobber a peer's committed write
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _reload(self) -> None:
        """Read the state file as it is right now (called under the lock, or for reads)."""
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.data.setdefault("mission", "")
        self.data.setdefault("approvals", {})
        self.data.setdefault("lanes", {})

    def _write(self) -> None:
        """Atomic write: a whole document or nothing. Never a partial file."""
        self._tmp_path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(self._tmp_path, self.path)

    # --- lanes --------------------------------------------------------------
    @staticmethod
    def _blank_lane(*, harness: str, worktree: str, test_cmd: list[str],
                    model: str | None, depends_on: list[str] | None,
                    parent_dispatch: str | None, depth: int,
                    merge_into: str | None = None,
                    test_timeout_s: float | None = None) -> dict:
        """A complete lane record. Every field is always present on every lane."""
        return {
            #: The acceptance budget this lane DECLARED at dispatch, in seconds — or ``None`` when it
            #: declared none (a lane whose ledger predates the field), which every acceptance path
            #: then reads as the shipped default. Immutable after dispatch: only ``init_lane``'s
            #: dispatch-time argument can write it, and every re-run site READS it instead of
            #: carrying a constant of its own. Recorded here as the value the caller resolved, never
            #: as a policy: the ledger holds the number, the modules hold the rule.
            "test_timeout_s": test_timeout_s,
            "status": PENDING,
            "harness": harness,
            "worktree": worktree,
            "test_cmd": list(test_cmd),
            "model_requested": model,
            "depends_on": list(depends_on or []),
            "parent_dispatch": parent_dispatch,
            "depth": depth,
            "dispatch_id": None,
            #: The branch this lane's work is meant to land on, recorded by ``dispatch --into``.
            #: ``settle --accept`` merges ONLY when the primary worktree has this branch checked
            #: out; ``None`` means no target was recorded and the legacy behaviour stands (merge
            #: into whatever the primary worktree happens to have checked out).
            "merge_into": merge_into,
            #: The commit the lane started from, anchored once at its first dispatch. Evidence
            #: compares HEAD against this to tell "the lane committed its own work" (HEAD moved)
            #: from "the lane is still sitting on the commit it was cut from" (HEAD unchanged, so
            #: there is no lane commit to name). Additive field: nothing is renamed or removed.
            "base_commit": None,
            "attempt": 0,
            "consecutive_failures": 0,
            #: Every **infra** outcome for this lane, in order: ``{dispatch_id, reason, evidence, at}``.
            #: Infra means the controller's own re-run never ran the lane's tests (the command could
            #: not be started, or the budget killed it), so none of these is a failed test and none
            #: of them touches ``consecutive_failures``. The list is also the ledger's bound: at
            #: ``infra_limit`` entries the lane is parked for review (issue #1).
            "infra": [],
            #: Every **over_budget** outcome for this lane, in order: ``{dispatch_id, reason,
            #: evidence, at}``. Over budget means the lane's own recorded acceptance budget ran out
            #: before its ``test_cmd`` reached a verdict: not a red test (so never a strike on the
            #: breaker) and not an environment fault (so never an ``infra`` retry) — the lane parks.
            #: Kept apart from ``infra`` on purpose: two clocks, two words, two outcomes.
            "over_budget": [],
            "accepted": [],              # dispatch ids whose settlement was accepted
            "rejected": [],              # {dispatch_id, reason, at}
            "evidence": {},
            "history": [],
        }

    def _lane(self, lane_id: str) -> dict:
        """The live record for ``lane_id`` in the currently loaded snapshot."""
        try:
            return self.data["lanes"][lane_id]
        except KeyError:
            raise KeyError(f"unknown lane {lane_id!r}: call init_lane first") from None

    def _move(self, lane: dict, event: str, new: str) -> None:
        """Record one legal transition. ``apply`` has already validated ``new``."""
        lane["history"].append({"event": event, "from": lane["status"], "to": new,
                                "at": time.time()})
        lane["status"] = new

    def init_lane(self, lane_id: str, *, harness: str, worktree: str, test_cmd: list[str],
                  model: str | None = None, depends_on: list[str] | None = None,
                  parent_dispatch: str | None = None, depth: int = 0,
                  merge_into: str | None = None,
                  test_timeout_s: float | None = None) -> dict:
        """Create a lane. Idempotent: an existing lane is returned untouched.

        Raises :class:`DepthExceeded` when ``depth > max_depth`` — depth is a hard ceiling
        checked at creation, so a nesting bug cannot be discovered halfway through a run.

        ``merge_into`` is the ONE field that is not setdefault-ed: an explicit ``--into`` at
        dispatch *replaces* the recorded target (a re-dispatch is the conductor re-stating where
        the work lands), while passing nothing leaves whatever was recorded before. ``None`` means
        "not recorded", and ``settle --accept`` then merges into whatever the primary worktree has
        checked out, exactly as before ``--into`` existed.

        ``test_timeout_s`` is the lane's acceptance budget, resolved by the caller at dispatch. It
        follows the opposite rule to ``merge_into`` for a re-dispatch: explicit replaces, absent
        never clears — so a lane's declared budget cannot silently reset to a default just because
        the conductor dispatched it again without repeating the flag. ``None`` means the lane
        declared no budget of its own, and every acceptance path then reads the shipped default.
        """
        if depth > self.max_depth:
            raise DepthExceeded(
                f"{DEPTH_EXCEEDED_TOKEN}: lane {lane_id!r} at depth {depth} exceeds "
                f"max_depth {self.max_depth}"
            )
        record = self._blank_lane(harness=harness, worktree=worktree, test_cmd=test_cmd,
                                  model=model, depends_on=depends_on,
                                  parent_dispatch=parent_dispatch, depth=depth,
                                  merge_into=merge_into, test_timeout_s=test_timeout_s)
        with self._locked():
            lane = self.data["lanes"].setdefault(lane_id, record)
            # every record carries every field, even one hand-edited outside this module
            for key, value in record.items():
                lane.setdefault(key, value)
            if merge_into:
                lane["merge_into"] = merge_into
            # An explicit dispatch-time budget REPLACES whatever was recorded (the conductor
            # re-stating this lane's acceptance contract); passing none leaves it exactly as it was,
            # so a re-dispatch can never silently reset a lane's declared budget to the default.
            if test_timeout_s is not None:
                lane["test_timeout_s"] = float(test_timeout_s)
            self._write()
            return lane

    def dispatch(self, lane_id: str) -> str:
        """Start ONE attempt. Returns a fresh fencing token ``<lane_id>-d<attempt>``.

        Refuses (and writes nothing) when the lane is not dispatchable:
        :class:`IllegalTransition` for a blocked lane, an illegal state/event pair, or a
        ``depends_on`` parent that is not COMPLETED; :class:`ParallelismExceeded` when it
        would push concurrently-DISPATCHED lanes past ``max_parallel``.
        """
        with self._locked():
            lane = self._lane(lane_id)
            if lane["status"] == BLOCKED:
                # The table holds (blocked, retry) -> dispatched, but a dispatch verb is
                # not a retry: a parked lane re-enters only through an explicit unblock.
                raise IllegalTransition(
                    f"lane {lane_id!r} is blocked; unblock explicitly first"
                )
            # a failed lane re-enters via 'retry'; a fresh/ready lane via 'dispatch'
            event = "retry" if lane["status"] == FAILED else "dispatch"
            new = apply(lane["status"], event)      # legality first: never guess
            # dependency gate keeps parallel lanes honest
            for dep in lane["depends_on"]:
                dep_status = self.data["lanes"].get(dep, {}).get("status", "missing")
                if dep_status != COMPLETED:
                    raise IllegalTransition(
                        f"lane {lane_id!r} blocked: dependency {dep!r} is "
                        f"{dep_status!r}, not completed"
                    )
            live = sum(1 for other in self.data["lanes"].values()
                       if other["status"] == DISPATCHED)
            if live >= self.max_parallel:
                raise ParallelismExceeded(
                    f"{live} lanes are already dispatched (max_parallel "
                    f"{self.max_parallel}); refusing to dispatch {lane_id!r}"
                )
            self._move(lane, event, new)
            lane["attempt"] += 1
            lane["dispatch_id"] = f"{lane_id}-d{lane['attempt']}"
            # Anchor the lane's STARTING commit, once, in the same atomic write as its dispatch
            # token — so a lane can never be dispatched without its anchor, and a retry cannot move
            # the anchor onto a commit the lane itself made. A worktree git cannot read leaves the
            # anchor ``None`` (unknown); evidence then claims no commit rather than guessing one.
            if not lane.get("base_commit"):
                lane["base_commit"] = _head(Path(str(lane.get("worktree") or ".")))
            self._write()
            return lane["dispatch_id"]

    def settle(self, lane_id: str, dispatch_id: str, ok: bool,
               evidence: dict | None = None, reason: str = "", infra: bool = False,
               over_budget: bool = False) -> dict:
        """Accept or reject one dispatch's outcome, and apply the circuit breaker.

        Fencing runs FIRST, before the duplicate check: a superseded dispatch is stale
        whether or not it was already accepted, and "stale" is the reason a conductor has
        to see (``/tmp/canary3`` ordering, kept verbatim). Anything that is not the current
        token, or a token already settled, or a lane that is not mid-flight, is recorded in
        ``lane["rejected"]`` and returned as ``{"accepted": False, "reason": ...}``.

        On acceptance the outcome is recorded exactly as given. Evidence is stored verbatim
        (never synthesised, and a commit is never invented here — a lane that did not commit
        has no commit). Returns ``{"accepted": True, "status": ..., "consecutive_failures":
        ...}``.

        ``infra=True`` is the one settlement that is **not** an outcome of the lane: the
        controller's own re-run never ran the lane's ``test_cmd`` (it could not start it, or its
        budget killed it), so no test was proven either way. Such an attempt is recorded in
        ``lane["infra"]`` with its evidence, the lane moves ``dispatched -> ready`` so the ordinary
        dispatch gate can re-enter it, and ``consecutive_failures`` is **left exactly as it was**:
        the circuit breaker counts red tests, and an ENOENT is not one (issue #1). ``ok=True`` with
        ``infra=True`` is refused outright — an infra outcome is never an acceptance. At
        ``infra_limit`` accumulated infra outcomes the lane is parked ``blocked``, with a
        ``blocked_reason`` naming the environment, so a permanently unrunnable command reaches a
        human instead of being retried forever; that park touches no failure streak either.
        Returns that settlement with an extra ``infra_count``.

        ``over_budget=True`` is the other settlement that is not an outcome of the lane, and it is a
        different clock from ``infra``: the lane's ``test_cmd`` started, but it outran the acceptance
        budget the lane itself RECORDED at dispatch (``test_timeout_s``), so it never reached a
        verdict. Nothing failed in the environment (never ``infra``, never an ``infra`` retry) and no
        test was proven red (never a strike on the breaker) — so the lane parks ``blocked`` with a
        ``blocked_reason`` naming ``over_budget``, its ``consecutive_failures`` is left untouched,
        and the outcome is recorded in ``lane["over_budget"]``. Parked is the point: the budget it
        declared has already proved too small, and a silent re-run of the same unchanged command
        would blow it again. Returns that settlement with an extra ``over_budget_count``.
        """
        if infra and ok:
            raise ValueError("infra=True means no test verdict was reached: it cannot accept a lane")
        if over_budget and ok:
            raise ValueError(
                "over_budget=True means no test verdict was reached: it cannot accept a lane")
        if infra and over_budget:
            raise ValueError(
                "an attempt is either the environment failing (infra) or the lane's own recorded "
                "budget running out (over_budget), never both")
        with self._locked():
            lane = self._lane(lane_id)

            def reject(why: str) -> dict:
                lane["rejected"].append({"dispatch_id": dispatch_id, "reason": why,
                                         "at": time.time()})
                self._write()
                return {"accepted": False, "reason": why}

            # fencing FIRST: only the CURRENT dispatch may settle this lane
            if lane["dispatch_id"] is None or dispatch_id != lane["dispatch_id"]:
                return reject(
                    f"stale dispatch {dispatch_id!r} cannot settle lane {lane_id!r} "
                    f"(current is {lane['dispatch_id']!r})"
                )
            # exactly-once: a duplicate is rejected after fencing, so a superseded token
            # is always reported stale even if it was accepted earlier
            if dispatch_id in lane["accepted"]:
                return reject("duplicate: this dispatch was already settled")
            # the lane must still be mid-flight for a settlement to mean anything
            if lane["status"] != DISPATCHED:
                return reject(f"lane is {lane['status']!r}, not dispatched")

            if over_budget:
                # A park that is deliberately NOT the failure transition: the lane's tests never
                # reached a verdict, so nothing about the lane's work was proven — the failure
                # streak must not move (DISPATCHED -> blocked is a legal pair, and the breaker never
                # sees this attempt at all).
                outcomes = lane.setdefault("over_budget", [])
                self._move(lane, "over_budget", apply(lane["status"], "block"))
                outcomes.append({"dispatch_id": dispatch_id, "reason": reason,
                                 "evidence": dict(evidence) if evidence else {},
                                 "at": time.time()})
                lane["blocked_reason"] = (
                    f"over_budget: {reason} ({len(outcomes)} over-budget outcome(s); the acceptance "
                    f"command exceeded the lane's recorded test_timeout_s, so no test verdict was "
                    f"reached — the failure streak is untouched at "
                    f"{lane['consecutive_failures']}, and this park is for review)"
                )
                self._write()
                return {"accepted": True, "status": lane["status"],
                        "consecutive_failures": lane["consecutive_failures"],
                        "over_budget_count": len(outcomes)}

            if infra:
                infra_outcomes = lane.setdefault("infra", [])
                self._move(lane, "infra", READY)
                infra_outcomes.append({"dispatch_id": dispatch_id, "reason": reason,
                                       "evidence": dict(evidence) if evidence else {},
                                       "at": time.time()})
                if len(infra_outcomes) >= self.infra_limit:
                    # Parked for a human, and parked by the INFRA cap — never by the breaker, which
                    # counts failing tests and has just been deliberately left untouched.
                    self._move(lane, "block", apply(lane["status"], "block"))
                    lane["blocked_reason"] = (
                        f"infra: {reason} ({len(infra_outcomes)} infra outcomes; limit "
                        f"{self.infra_limit}; the lane's tests never ran, so the failure streak "
                        f"is untouched)"
                    )
                self._write()
                return {"accepted": True, "status": lane["status"],
                        "consecutive_failures": lane["consecutive_failures"],
                        "infra_count": len(infra_outcomes)}

            self._move(lane, "success" if ok else "failure",
                       COMPLETED if ok else FAILED)
            lane["accepted"].append(dispatch_id)
            lane["evidence"] = dict(evidence) if evidence else {}
            lane["consecutive_failures"] = 0 if ok else lane["consecutive_failures"] + 1
            if not ok:
                lane["needs_review_reason"] = reason
                if lane["consecutive_failures"] >= self.failure_limit:
                    # circuit breaker: park the lane the moment the streak hits the limit,
                    # inside the same locked section, so no failure streak is ever durable
                    # without its parked state.
                    self._move(lane, "block", apply(lane["status"], "block"))
                    lane["blocked_reason"] = (
                        f"circuit breaker: {lane['consecutive_failures']} consecutive "
                        f"failures (limit {self.failure_limit})"
                    )
            self._write()
            return {"accepted": True, "status": lane["status"],
                    "consecutive_failures": lane["consecutive_failures"]}

    # --- read-only views ----------------------------------------------------
    def lane(self, lane_id: str) -> dict:
        """The lane record as currently on disk."""
        self._reload()
        return self._lane(lane_id)

    def dispatched(self) -> list[str]:
        """Lane ids currently mid-flight (status DISPATCHED)."""
        self._reload()
        return [lane_id for lane_id, lane in self.data["lanes"].items()
                if lane["status"] == DISPATCHED]

    def summary(self) -> dict:
        """``{status: count}`` over all lanes; statuses with no lanes are omitted."""
        self._reload()
        counts: dict[str, int] = {}
        for lane in self.data["lanes"].values():
            counts[lane["status"]] = counts.get(lane["status"], 0) + 1
        return counts
