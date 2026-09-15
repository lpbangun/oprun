#!/usr/bin/env python3
"""oprun v0.2 advance — the bounded unattended settle loop.

``advance`` is a **process, not a daemon**: it waits for the lanes that are DISPATCHED, decides
each one from evidence that lane itself produced, settles the ledger, and **exits**. It holds no
state of its own — ``state.json`` is the only record — so it can be launched detached next to the
lanes (``systemd-run --user … python3 scripts/advance.py --ledger …``) and a mission makes
progress with **zero conductor turns**. That is what makes "pre-approve the shape, then sleep"
work without a resident scheduler.

Three rules are load-bearing:

1. **Acceptance is evidence, never liveness.** A lane is accepted only when its sidecar exists,
   matches the lane's CURRENT ``dispatch_id``, reports ``status == "success"``, *and* the
   controller re-runs the lane's own ``test_cmd`` to exit 0. Nothing else can accept a lane. A
   dead unit with a valid sidecar is done; a live unit with no sidecar is not.
2. **A model substitution is never accepted.** The sidecar's ``model`` is compared against the lane's
   requested pin (``identity_reason``, imported from the registry — the same function the CLI's
   ``settle`` calls): a normalized mismatch, or a pin that was never reported at all, PARK the lane.
   The one exception is a harness that declares ``pin_verifiable=False``, where the pin reaches the
   CLI but nothing the CLI emits can corroborate it, so a reported mismatch proves nothing and is
   recorded as an advisory ``identity_warning`` instead. Both raw strings are kept in the settled
   evidence, so "the requested model actually ran" is checkable after the fact. **Harness identity is
   not compared at all**: the registry picked the binary, so a worker's harness self-report is
   recorded verbatim and warned about, never enforced (``identity_warning``).
3. **A unit's lifetime answers "should I keep waiting?" — never "is it done?"** (see
   :func:`_unit_lifetime`). At the deadline it decides exactly one thing: whether a *missing*
   sidecar is still possible. A still-active unit is left alone (the worker may be about to write
   its evidence); a finished-or-absent one makes the silence final, so the dispatch is settled as
   a **failure**. Lifetime can never accept a lane — acceptance is evidence only.
4. **Every loop is bounded.** The runner returns when the lanes are decided or when the deadline
   passes. A lane that fails is *parked*, not retried blindly: the ledger's circuit breaker owns
   the failure streak, and a ``blocked`` lane is terminal as far as this process is concerned.
5. **The environment failing is not the lane failing.** When the acceptance re-run cannot be
   *started* (the program is not on PATH: ENOENT), or a lane that recorded no acceptance budget of
   its own is killed by the controller's default clock, no test was proven red. That outcome is
   recorded as ``infra``, the lane goes back to ``ready`` and its failure streak is left untouched —
   an ENOENT must never park a lane or strike the breaker (issue #1). The ledger caps how many infra
   outcomes a lane may accumulate, so a permanently unresolvable command still reaches a human.
6. **Four clocks, never one.** The lane's **acceptance budget** (``test_timeout_s``, recorded at
   dispatch and read here) is how long the lane's ``test_cmd`` may run; ``--timeout`` is only the
   **staleness** clock for a lane that has produced no artifact yet; systemd answers **process
   lifetime**; and a human owns **parking**. A run that outgrows the lane's own recorded budget is
   ``over_budget``: it parks for review — never ``infra`` (nothing in the environment failed, so it
   spends no infra retry) and never a red test (nothing was proven, so it touches no failure streak).
   This runner owns no ceiling of its own any more, and its deadline never shrinks a command that has
   already started.

No model is involved anywhere here: no LLM imports, no harness subprocess. The only subprocesses
are the lane's own ``test_cmd`` and read-only ``systemctl``/``git`` queries.

``scripts/probe.py`` (the witness module, shipped by a sibling lane) is imported opportunistically
and its verdict is recorded in the settlement evidence as corroboration. The acceptance decision
itself is computed by :func:`lane_verdict`, which implements the same three conditions locally, so
the runner is deterministic and correct with or without that module — a **documented local
fallback**. Two authorities over one question ("is this lane done?") is exactly what this design
refuses; the ledger, the sidecar and systemd each answer a different question.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

# The ONE git-evidence builder lives in the ledger, next to the lane records whose ``base_commit``
# anchor it needs: this module used to carry its own copy, and that copy named the worktree's HEAD
# (the shared base SHA, for a lane that never committed) as the lane's ``commit``.
from ledger import (BLOCKED, TERMINAL, Ledger, git_evidence, evidence_outcome,
                    missing_evidence_paths, EVIDENCE_UNRESOLVED, REVIEW_UNAVAILABLE)

import launch  # sibling module: the only detach/lifetime authority (scripts/launch.py)
# The identity rule — did this lane run the model and harness the ledger designated? — lives with
# the registry that owns the harness ids and their accepted spellings, and the CLI's ``settle``
# imports the same functions. One rule, two acceptance paths: the runner can never disagree with the
# CLI about a substitution. ``identity_reason`` can park a lane (the model pin); ``identity_warning``
# can only ever be recorded and printed (a harness self-report, or a model report on a harness that
# cannot corroborate a pin). The imports are deliberately NOT optional — a guarantee that can
# silently be absent is not a guarantee — and they stay leaf imports (``harnesses`` imports nothing
# but the ledger), so ``advance`` still runs with or without ``probe.py``.
from harnesses import identity_reason, identity_warning  # sibling: the registry's identity rule

try:  # scripts/probe.py is written by a sibling lane; advance must not require it
    from probe import probe as _probe_lane  # type: ignore[import-not-found]
except Exception as _probe_exc:             # any failure (missing file, half-written, bad import)
    _probe_lane = None                       # type: ignore[assignment]
    _PROBE_IMPORT_ERROR = f"{type(_probe_exc).__name__}: {_probe_exc}"
else:
    _PROBE_IMPORT_ERROR = ""

#: ``True`` when the witness module could be imported. Recorded in evidence, never required.
PROBE_AVAILABLE = _probe_lane is not None

#: Sidecar convention: one evidence file per dispatch, inside the lane's own worktree.
SIDECAR_DIRNAME = ".oprun"
SIDECAR_PREFIX = "result."
SIDECAR_SUFFIX = ".json"

#: The shipped default acceptance budget, in seconds: what a lane that RECORDED no
#: ``test_timeout_s`` of its own reads. There is deliberately no runner-owned ceiling constant any
#: more — the old ``TEST_TIMEOUT = 300.0`` is gone, the budget comes from the lane's own record, and
#: nothing may shrink it: not this runner's ``--timeout``/deadline (staleness is a different clock),
#: not the loop's remaining time.
#:
#: ``advance`` must run with or without ``probe.py``, so the number is spelled here too — and the
#: suite pins it equal to ``probe``'s and ``oprun``'s, so three copies cannot drift into three
#: different 900s.
DEFAULT_TEST_TIMEOUT_S = 900.0

#: The lane field carrying the lane's acceptance budget. Spelled identically in ``probe.py`` and
#: ``oprun.py`` and pinned to one string by the suite, exactly like :data:`UNIT_PATH_FIELD`.
TEST_TIMEOUT_FIELD = "test_timeout_s"

#: Characters of test output kept as evidence (the tail is where failures are named).
TEST_TAIL = 2000

#: Verdicts a lane can carry, matching ``probe``'s vocabulary. ``INFRA`` is the environment's
#: failure, not the lane's: the acceptance command could not be RUN (ENOENT), so no test was proven
#: red and nothing about it may count as a lane failure (issue #1). ``OVER_BUDGET`` is the other clock
#: — the lane recorded an acceptance budget of its own and its command outran it: also "no verdict was
#: reached", but the clock belongs to the lane's contract, so it parks for review instead of being
#: retried as an environment fault.
DONE, PENDING, NEEDS_INPUT, STALLED, FAILED_VERDICT, INFRA, OVER_BUDGET = (
    "done", "pending", "needs_input", "stalled", "failed", "infra", "over_budget",
)

#: Acceptance re-runs the runner retries **in place** when the command could not be run at all.
#: Bounded on purpose: an environment that cannot start the command will not start it on the third
#: try either, and the dead attempts cost a mission nothing but a fraction of a second.
INFRA_RUN_RETRIES = 2

#: Seconds between those in-place re-runs.
INFRA_RETRY_BACKOFF = 0.25

#: The lane field recording the PATH the lane's worker unit was launched with (written at
#: dispatch from ``launch()``'s own report). Read from the lane dict handed to the runner, never
#: derived: an absent/blank value means "not recorded" and the acceptance re-run then inherits the
#: ambient environment exactly as it did before the field existed. Spelled identically in
#: ``probe.py``; the suite asserts both modules agree, so the two re-run sites can never drift.
UNIT_PATH_FIELD = "unit_path"

#: The one environment variable the re-run overrides. Nothing is resolved from it, ever.
PATH_ENV = "PATH"


def _test_cmd_env(unit_path: str | None) -> dict[str, str] | None:
    """The environment a lane's ``test_cmd`` is re-run under — ``None`` means "inherit ours".

    ``launch.py`` pins a PATH onto every worker unit, and the launcher's own report of it is
    recorded on the lane at dispatch (``unit_path``). The acceptance re-run must happen under that
    same PATH, or the worker and the controller run the lane's ``test_cmd`` under two different
    PATHs by construction — "the test the worker passed" need not be the test the controller can
    run. With a recorded PATH the command runs with a copy of the current environment whose
    ``PATH`` is exactly that value; with none, ``None`` is returned so the child inherits the
    ambient environment (every ledger written before the field keeps working unchanged).

    Deliberately nothing else: no ``shutil.which``, no resolution, no heuristic, no default
    entries. Twin of ``probe._test_cmd_env`` — ``advance`` must run with or without ``probe.py``,
    so the rule lives in both and each reads the same lane field.
    """
    recorded = str(unit_path or "").strip()
    if not recorded:
        return None
    return {**os.environ, PATH_ENV: recorded}


def recorded_test_timeout_s(lane: dict) -> float | None:
    """The acceptance budget the lane **declared at dispatch**, or ``None`` when it declared none.

    Read from the lane dict handed to this runner — never sniffed from the environment, never
    recomputed, never defaulted here (:func:`acceptance_budget_s` is the defaulted read). Only a
    positive finite number counts as a declaration; a missing field, ``None``, ``0``, a negative
    value or a non-numeric string all mean "this lane declared no budget", which is what a ledger
    written before the field existed looks like.

    Twin of ``probe.recorded_test_timeout_s`` — ``advance`` must run with or without ``probe.py``,
    so the rule lives in both and each reads the same lane field.
    """
    raw = lane.get(TEST_TIMEOUT_FIELD) if isinstance(lane, dict) else None
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if (value > 0 and value != float("inf")) else None


def acceptance_budget_s(lane: dict) -> float:
    """The budget this lane's acceptance re-run gets, in seconds.

    A lane that declared a budget of its own is judged against THAT budget (exceeding it is
    ``over_budget``: the clock that ran out is the lane's own contract). A lane that declared none
    reads the shipped default, and the kill then belongs to the controller's own clock — ``infra``,
    exactly the behaviour such a ledger had before the field existed.

    Twin of ``probe.acceptance_budget_s``, for the same reason as above.
    """
    recorded = recorded_test_timeout_s(lane)
    if recorded is not None:
        return recorded
    return float(DEFAULT_TEST_TIMEOUT_S)


class _Decision(NamedTuple):
    """What one lane's dispatch turned into during this pass."""

    lane_id: str
    kind: str          # "accepted" | "parked"
    category: str      # "ok" | "test_failure" | "evidence"
    detail: str


# --- sidecar reading ---------------------------------------------------------


def sidecar_path(worktree: Path | str, dispatch_id: str) -> Path:
    """Where a lane's evidence for one dispatch lives: ``<worktree>/.oprun/result.<id>.json``."""
    return Path(worktree) / SIDECAR_DIRNAME / f"{SIDECAR_PREFIX}{dispatch_id}{SIDECAR_SUFFIX}"


def _read_object(path: Path) -> tuple[dict | None, str]:
    """Parse a JSON object file. Returns ``(payload, "")`` or ``(None, why-not)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, ""


def _find_sidecar(directory: Path, dispatch_id: str) -> tuple[Path | None, dict | None, str]:
    """Locate the sidecar for ``dispatch_id``, and say what else was found.

    The exact path (``result.<dispatch_id>.json``) wins. A sidecar written under another name
    still counts when it *claims* this dispatch — but a file that claims a **superseded**
    dispatch is reported, not ignored, because "the artifact on disk belongs to an older
    attempt" is precisely the fact a conductor needs to see in ``needs_review``.
    """
    exact = directory / f"{SIDECAR_PREFIX}{dispatch_id}{SIDECAR_SUFFIX}"
    if exact.is_file():
        # The canonical path wins and its *contents* are then validated by _validate_sidecar, so
        # a file named for this dispatch that claims a superseded one is reported precisely.
        payload, why = _read_object(exact)
        if payload is None:
            return None, None, f"unusable sidecar: {exact.name} ({why})"
        return exact, payload, ""
    stale: list[str] = []
    unusable: list[str] = []
    candidates: list[Path] = []
    if directory.is_dir():
        candidates = sorted(directory.glob(f"{SIDECAR_PREFIX}*{SIDECAR_SUFFIX}"))
    for path in candidates:
        if not path.is_file():
            continue
        payload, why = _read_object(path)
        if payload is None:
            unusable.append(f"{path.name} ({why})")
            continue
        if payload.get("dispatch_id") == dispatch_id:
            return path, payload, ""
        stale.append(f"{path.name} carries dispatch_id {payload.get('dispatch_id')!r}")
    note = ""
    if stale:
        note = f"stale sidecar: {stale[0]} (current dispatch is {dispatch_id!r})"
    if unusable:
        note = f"{note}; " if note else ""
        note += f"unusable sidecar: {unusable[0]}"
    return None, None, note


def _validate_sidecar(payload: dict, lane_id: str, dispatch_id: str) -> str:
    """The frozen acceptance rule for the artifact itself. Returns ``""`` when it holds."""
    found = payload.get("dispatch_id")
    if found != dispatch_id:
        return (f"stale dispatch: sidecar says {found!r}, lane's current dispatch is "
                f"{dispatch_id!r}")
    task = payload.get("task_id")
    if lane_id and isinstance(task, str) and task and task != lane_id:
        return f"sidecar task_id {task!r} does not match lane {lane_id!r}"
    status = payload.get("status")
    if status != "success":
        return f"sidecar status is {status!r}, not 'success'"
    return ""




def _sidecar_facts(payload: dict) -> dict:
    """A compact verbatim excerpt of the worker's sidecar (never a rewrite of it)."""
    return {
        "keys": sorted(str(key) for key in payload)[:32],
        "status": payload.get("status"),
        "harness": payload.get("harness"),
        "model": payload.get("model"),
        "exit_code": payload.get("exit_code"),
    }


def lane_verdict(lane: dict, *, lane_id: str = "", unit_active: bool | None = None,
                 timeout_exceeded: bool = False, sidecar_dir: Path | None = None) -> dict:
    """The **local** witness for one lane — the documented fallback when ``probe.py`` is absent.

    Same contract as ``probe.py``: ``{"verdict", "reason", "evidence"}``, plus ``"case"``
    (``no_artifact`` | ``unusable_artifact`` | ``ok``) so a caller can tell "nothing to decide
    yet" apart from "there is something here and it is not acceptable".

    A missing artifact is never a failure on its own: without evidence the verdict can only be
    ``pending``/``needs_input``/``stalled``, and only a **provably exited** unit makes it
    ``stalled``.
    """
    dispatch_id = str(lane.get("dispatch_id") or "")
    worktree = Path(str(lane.get("worktree") or "."))
    lane_id = lane_id or str(lane.get("lane_id") or lane.get("id") or "")
    directory = Path(sidecar_dir) if sidecar_dir is not None else worktree / SIDECAR_DIRNAME
    path, payload, note = _find_sidecar(directory, dispatch_id)
    found: dict = {
        "dispatch_id": dispatch_id,
        "sidecar": str(path) if path is not None else None,
        "sidecar_dir": str(directory),
        "worktree": str(worktree),
        "found": path is not None,
    }
    if payload is None:
        if note:
            return {"verdict": FAILED_VERDICT, "reason": note, "case": "unusable_artifact",
                    "evidence": found}
        unit = launch.unit_name(lane_id) if lane_id else "?"
        if unit_active is False:
            return {"verdict": STALLED,
                    "reason": f"unit {unit} exited without writing a sidecar", "case": "no_artifact",
                    "evidence": found}
        if timeout_exceeded:
            return {"verdict": NEEDS_INPUT,
                    "reason": f"no sidecar within the timeout for dispatch {dispatch_id!r}",
                    "case": "no_artifact", "evidence": found}
        return {"verdict": PENDING,
                "reason": f"waiting for {sidecar_path(worktree, dispatch_id)}",
                "case": "no_artifact", "evidence": found}
    found["facts"] = _sidecar_facts(payload)
    why = _validate_sidecar(payload, lane_id, dispatch_id)
    if why:
        return {"verdict": FAILED_VERDICT, "reason": why, "case": "unusable_artifact",
                "evidence": found}
    # The artifact is well-formed and claims success — now check it claims the RIGHT run. The model
    # pin is the guard: a mismatch (or a pin that was never reported) is a failed dispatch, so it
    # parks and is never accepted — except where the harness declares ``pin_verifiable=False``, in
    # which case the comparison cannot establish anything and ``identity_warning`` records it for the
    # reader instead. The harness itself is never compared here: the registry chose the binary, so a
    # worker's harness self-report carries no routing information and can only ever produce a
    # warning. The warning is computed before the verdict so BOTH outcomes carry it in evidence.
    found["identity_warning"] = identity_warning(
        harness=lane.get("harness"), model_requested=lane.get("model_requested"),
        harness_reported=payload.get("harness"), model_reported=payload.get("model"))
    identity = identity_reason(harness=lane.get("harness"),
                               model_requested=lane.get("model_requested"),
                               harness_reported=payload.get("harness"),
                               model_reported=payload.get("model"))
    if identity:
        return {"verdict": FAILED_VERDICT, "reason": identity, "case": "unusable_artifact",
                "evidence": found}
    # Evidence policy is shared with probe.py and remains available if probe itself is absent.
    missing = missing_evidence_paths(payload, worktree, directory)
    park_kind, missing = evidence_outcome(payload, missing)
    found["missing_evidence_paths"] = missing
    if park_kind:
        found["park_kind"] = park_kind
        reason = ("reviewer is unavailable" if park_kind == REVIEW_UNAVAILABLE else
                  (f"evidence paths do not resolve: {missing!r}" if missing else
                   "green sidecar has neither an artifact nor an explicit waiver"))
        return {"verdict": park_kind, "reason": reason, "case": "unusable_artifact",
                "evidence": found}
    return {"verdict": DONE, "reason": f"{path.name} matches dispatch {dispatch_id!r}",
            "case": "ok", "evidence": found}


def witness_verdict(lane: dict, *, lane_id: str = "", unit_active: bool | None = None,
                    timeout_exceeded: bool = False, sidecar_dir: Path | None = None) -> dict:
    """``probe.py``'s verdict when importable, else the local fallback — never raises.

    The result carries ``source`` so a reader can always tell which witness spoke:
    ``"probe.py"`` or ``"local:…"`` with the reason the sibling module could not be used.
    """
    local = lane_verdict(lane, lane_id=lane_id, unit_active=unit_active,
                         timeout_exceeded=timeout_exceeded, sidecar_dir=sidecar_dir)
    if _probe_lane is None:
        return {**local, "source": f"local:probe.py-absent({_PROBE_IMPORT_ERROR})"}
    try:
        got = _probe_lane(lane, unit_active=unit_active, timeout_exceeded=timeout_exceeded,
                          sidecar_dir=sidecar_dir)
    except Exception as exc:  # a witness that raises is a witness we do not have
        return {**local, "source": f"local:probe.py-error({type(exc).__name__})"}
    if not isinstance(got, dict) or "verdict" not in got:
        return {**local, "source": "local:probe.py-unexpected-shape"}
    return {**got, "source": "probe.py"}


# --- process lifetime (read-only, best effort) -------------------------------


def _unit_active(lane_id: str) -> bool | None:
    """``True``/``False``/``None`` — tri-state on purpose, from systemd only.

    ``None`` means "no record": a transient unit is created asynchronously and ``--collect``
    removes it at exit, so its absence must never be read as "the worker died".
    """
    unit = launch.unit_name(lane_id)
    try:
        if not launch.unit_known(unit):
            return None
        return launch.unit_status(unit)["active"]
    except Exception:
        return None


def _unit_finished(lane_id: str) -> bool | None:
    """Whether the lane's unit has provably exited (``None`` when systemd cannot say)."""
    try:
        return launch.unit_finished(launch.unit_name(lane_id))
    except Exception:
        return None


def _unit_lifetime(lane_id: str) -> str:
    """Classify the lane's unit as ``"active"``, ``"finished"`` or ``"absent"``.

    This three-way answer is what a *missing sidecar* is judged by, and nothing else is:

    * ``"active"``  — the unit is running right now. The worker may still write its evidence, so
      the lane must keep waiting and must **never** be settled here: failing a live worker invents
      a failure and poisons the circuit-breaker streak.
    * ``"finished"``— systemd reports the main process exited, so no sidecar is coming.
    * ``"absent"``  — systemd holds no record (never created, or ``--collect`` already removed it)
      and has no exit to report. Nothing is running under this lane's unit either, so the same
      conclusion holds: no artifact will appear.

    ``active`` is tested first so a live lane is never re-classified by a second, slower query.
    """
    if _unit_active(lane_id) is True:
        return "active"
    if _unit_finished(lane_id) is True:
        return "finished"
    return "absent"


# --- controller evidence -----------------------------------------------------


def _run_test(lane: dict, worktree: Path) -> tuple[int, str, str, str]:
    """Re-run the lane's OWN ``test_cmd`` in its worktree: ``(exit_code, tail, note, outcome)``.

    ``outcome`` is ``""`` when the command reached a verdict of its own, else it names the clock
    that killed the run — and the caller must branch on it, never flatten the two:

    * ``INFRA`` — the command could not be STARTED (ENOENT/OSError/ValueError), or this lane
      declared no acceptance budget of its own and the controller's default clock killed it. That is
      an environment fault, so no test was proven red and the caller must never count it as a
      failing test (issue #1: rc 127 was settled as a lane failure and struck the circuit breaker).
    * ``OVER_BUDGET`` — the lane RECORDED an acceptance budget at dispatch (``test_timeout_s``) and
      its own command outran it. Also "no verdict was reached", but the clock belongs to the lane's
      own contract rather than to the environment: it parks for a human, gets no infra retry, and
      touches no failure streak.

    The budget is the lane's recorded one (:func:`acceptance_budget_s`) and NOTHING here may shrink
    it: neither this runner's ``--timeout``/deadline nor the loop's remaining time. A command that
    has already started runs to its own budget — shortening it would turn a slow suite into a fake
    environment fault.

    The command and the working directory come from the ledger, never from the worker: a lane
    can neither choose nor skip the test that decides its own acceptance. The **environment** is
    the lane's own recorded launch PATH when it has one (:func:`_test_cmd_env`), so the re-run
    reproduces the PATH the worker ran under instead of the runner's.
    """
    cmd = [str(part) for part in (lane.get("test_cmd") or [])]
    if not cmd:
        return 1, "", "lane has no test_cmd, so nothing can be re-run to accept it", ""
    budget = acceptance_budget_s(lane)
    declared = recorded_test_timeout_s(lane) is not None
    try:
        proc = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True,
                              timeout=budget,
                              env=_test_cmd_env(lane.get(UNIT_PATH_FIELD)))
    except subprocess.TimeoutExpired:
        why = f"test command exceeded this lane's acceptance budget of {budget:g}s"
        return 124, "", why, (OVER_BUDGET if declared else INFRA)
    except OSError as exc:
        why = f"test command could not run: {exc}"
        return 127, "", why, INFRA
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-TEST_TAIL:]
    return proc.returncode, tail, f"exited {proc.returncode}", ""


def _utc_now() -> str:
    """ISO-8601 UTC, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- the loop ----------------------------------------------------------------


def _append_unique(items: list[str], value: str) -> None:
    """Keep the summary lists ordered and duplicate-free."""
    if value not in items:
        items.append(value)


def _settle_park(led: Ledger, lane_id: str, dispatch_id: str, category: str, reason: str,
                 emit: Callable[[str], None], *, verdict: dict | None = None,
                 witness: dict | None = None, exit_code: int | None = None,
                 park_kind: str | None = None) -> _Decision:
    """Refuse an outcome: the lane goes to ``needs_review`` — and never to ``completed``.

    The advisory ``identity_warning`` travels with the park: a lane can be parked for a model
    substitution while its harness self-report is also wrong, and a reviewer should see both facts
    without having to read the sidecar again. It is recorded, printed, and never the reason.
    """
    lane = led.lane(lane_id)
    facts = ((verdict or {}).get("evidence") or {}).get("facts") or {}
    warning = str(((verdict or {}).get("evidence") or {}).get("identity_warning") or "")
    evidence = {
        "controller": "advance",
        "dispatch_id": dispatch_id,
        "verdict": (verdict or {}).get("verdict"),
        "rejected_because": reason,
        "sidecar": (verdict or {}).get("evidence", {}).get("sidecar"),
        # The registered identity, and what the worker reported — kept verbatim even on a park, so
        # the pair a human reviews is the pair that was actually observed.
        "harness": lane.get("harness"),
        "harness_reported": facts.get("harness"),
        "model_requested": lane.get("model_requested"),
        "model_reported": facts.get("model"),
        "identity_warning": warning,
        "test_exit_code": exit_code,
        "witness": witness,
        "checked_at": _utc_now(),
    }
    if park_kind not in (EVIDENCE_UNRESOLVED, REVIEW_UNAVAILABLE):
        park_kind = None
    result = led.settle(lane_id, dispatch_id, False, evidence=evidence, reason=reason,
                        park_kind=park_kind)
    if not result.get("accepted"):
        emit(f"[{lane_id}] SETTLE_REFUSED {result.get('reason')}")
        return _Decision(lane_id, "parked", "evidence",
                         f"ledger refused the settlement: {result.get('reason')}")
    if warning:
        emit(f"[{lane_id}] IDENTITY_WARNING advisory {warning}")
    suffix = f" exit={exit_code}" if exit_code is not None else ""
    emit(f"[{lane_id}] NEEDS_REVIEW{suffix} {reason}")
    return _Decision(lane_id, "parked", category, reason)


def _settle_over_budget(led: Ledger, lane_id: str, dispatch_id: str, reason: str,
                        emit: Callable[[str], None], *, verdict: dict | None = None,
                        witness: dict | None = None, exit_code: int | None = None,
                        tail: str = "", budget_s: float | None = None) -> _Decision:
    """Park a lane whose acceptance command outran the budget the lane itself RECORDED.

    ``over_budget`` is a clock of its own, deliberately not folded into either of its neighbours:

    * not ``infra`` — nothing in the environment failed; the command started and simply had a budget
      too small for it, so it must not be retried in place as a transient fault;
    * not a failure — no test was proven red, so the lane must not be counted against the circuit
      breaker. ``consecutive_failures`` is left exactly as it was, the ledger records the outcome in
      ``lane["over_budget"]`` and parks the lane ``blocked``, and the park itself is the escalation:
      the lane declared a budget that has already proved too small, and re-running the same command
      unchanged would blow it again.

    The lane therefore reaches a human through the ledger's existing parked path, with the reason
    naming ``over_budget`` and the budget.
    """
    lane = led.lane(lane_id)
    evidence = {
        "controller": "advance",
        "dispatch_id": dispatch_id,
        "verdict": OVER_BUDGET,
        "outcome": OVER_BUDGET,
        "rejected_because": reason,
        "sidecar": (verdict or {}).get("evidence", {}).get("sidecar"),
        "test_exit_code": exit_code,
        "test_output_tail": tail,
        "acceptance_budget_s": budget_s,
        "harness": lane.get("harness"),
        "model_requested": lane.get("model_requested"),
        "witness": witness,
        "checked_at": _utc_now(),
        "note": ("the acceptance command exceeded the acceptance budget this lane recorded at "
                 "dispatch, so it never reached a verdict of its own: this is not an environment "
                 "fault (no infra retry) and not a red test (the failure streak is untouched) — it "
                 "parks for review"),
    }
    result = led.settle(lane_id, dispatch_id, False, evidence=evidence, reason=reason,
                        over_budget=True)
    if not result.get("accepted"):
        emit(f"[{lane_id}] SETTLE_REFUSED {result.get('reason')}")
        return _Decision(lane_id, "parked", "over_budget",
                         f"ledger refused the settlement: {result.get('reason')}")
    count = result.get("over_budget_count")
    suffix = f" exit={exit_code}" if exit_code is not None else ""
    emit(f"[{lane_id}] NEEDS_REVIEW over_budget{suffix} {reason} "
         f"(over-budget outcome {count}; the failure streak is untouched and no infra retry is "
         f"spent — the lane is parked for review)")
    return _Decision(lane_id, "parked", "over_budget", reason)


def _settle_no_evidence(led: Ledger, lane_id: str, dispatch_id: str, *, lifetime: str,
                        verdict: dict | None, witness: dict | None,
                        emit: Callable[[str], None]) -> _Decision:
    """Park a lane that produced no acceptable sidecar and whose unit can no longer produce one.

    ``lifetime`` is :func:`_unit_lifetime`'s answer, and it is what names the ledger reason:
    ``"finished"`` means systemd watched the worker exit, ``"absent"`` means there is no unit to
    run under. Both are a failed dispatch — the worker exited, or it never started, and either way
    no artifact will arrive — so counting it toward the circuit breaker invents nothing.

    ``"active"`` must never reach here: a running worker may still write its evidence, and
    settling it would poison the failure streak with a failure that did not happen.
    """
    if lifetime == "active":
        raise ValueError("an active unit is never settled: it may still produce evidence")
    reason = ("unit exited with no acceptable sidecar" if lifetime == "finished"
              else "no unit and no acceptable sidecar")
    return _settle_park(led, lane_id, dispatch_id, "evidence", reason, emit,
                        verdict=verdict, witness=witness)


def _settle_infra(led: Ledger, lane_id: str, dispatch_id: str, reason: str,
                  emit: Callable[[str], None], *, verdict: dict | None = None,
                  witness: dict | None = None, exit_code: int | None = None,
                  tail: str = "", retries: int = 0) -> _Decision | None:
    """Record an ``infra`` outcome: the controller could not RUN the lane's acceptance command.

    This is the *environment* failing, not the lane: the command was never started (ENOENT, not
    executable) or the controller's own budget killed it, so no test was proven red. The ledger
    therefore records the attempt in ``lane["infra"]`` and puts the lane back in ``ready`` —
    dispatchable again through the ordinary gate, with ``consecutive_failures`` **untouched** —
    instead of settling a failure that would park the lane and strike the circuit breaker (issue #1).

    The bound lives in the ledger (``infra_limit``): once a lane has accumulated that many infra
    outcomes it is parked ``blocked`` with a ``blocked_reason`` naming the environment, so a
    permanently unresolvable command surfaces to a human instead of being retried forever.

    Returns ``None`` when the lane was simply put back in ``ready`` — nothing is decided: it is no
    longer in flight, so the caller must not report it as accepted, parked or timed out, and the
    conductor's next ``dispatch`` is what relaunches the worker.
    """
    lane = led.lane(lane_id)
    evidence = {
        "controller": "advance",
        "dispatch_id": dispatch_id,
        "verdict": INFRA,
        "outcome": "infra",
        "rejected_because": reason,
        "sidecar": (verdict or {}).get("evidence", {}).get("sidecar"),
        "test_exit_code": exit_code,
        "test_output_tail": tail,
        "test_reruns": retries,
        "harness": lane.get("harness"),
        "model_requested": lane.get("model_requested"),
        "witness": witness,
        "checked_at": _utc_now(),
        "note": ("the acceptance command never ran to a verdict of its own: the environment failed, "
                 "so this is not a red test and the failure streak is untouched"),
    }
    result = led.settle(lane_id, dispatch_id, False, evidence=evidence, reason=reason, infra=True)
    if not result.get("accepted"):
        emit(f"[{lane_id}] SETTLE_REFUSED {result.get('reason')}")
        return _Decision(lane_id, "parked", "infra",
                         f"ledger refused the settlement: {result.get('reason')}")
    count = result.get("infra_count")
    if result.get("status") == BLOCKED:
        emit(f"[{lane_id}] NEEDS_REVIEW infra exit={exit_code} {reason} "
             f"(infra limit reached after {count} infra outcomes; failure streak untouched)")
        return _Decision(lane_id, "parked", "infra", reason)
    suffix = f" exit={exit_code}" if exit_code is not None else ""
    emit(f"[{lane_id}] INFRA_RETRY{suffix} {reason} (infra {count}/{led.infra_limit}; lane back to "
         f"ready for re-dispatch, failure streak untouched)")
    return None


def _attempt_lane(led: Ledger, lane_id: str, *, deadline: float,
                  emit: Callable[[str], None]) -> _Decision | None:
    """Decide one DISPATCHED lane, or return ``None`` when there is nothing to decide yet."""
    lane = led.lane(lane_id)
    dispatch_id = str(lane.get("dispatch_id") or "")
    worktree = Path(str(lane.get("worktree") or "."))
    if not dispatch_id:
        return _settle_park(led, lane_id, "", "evidence",
                            "lane is dispatched with no dispatch_id to fence its settlement",
                            emit)
    unit_active = _unit_active(lane_id)
    verdict = lane_verdict(lane, lane_id=lane_id, unit_active=unit_active,
                           timeout_exceeded=time.monotonic() >= deadline)
    if verdict["case"] == "no_artifact" and verdict["verdict"] != STALLED:
        return None                        # no evidence either way: keep waiting, never guess
    witness = witness_verdict(lane, lane_id=lane_id, unit_active=unit_active,
                              timeout_exceeded=time.monotonic() >= deadline)
    if verdict["case"] == "no_artifact":
        # The witness only says "no artifact yet"; the unit's lifetime says whether one can still
        # arrive. An active unit keeps the lane in flight (a live worker must never be failed); a
        # finished or absent one makes this a failed dispatch, which is settled rather than left
        # frozen in ``dispatched`` forever.
        lifetime = _unit_lifetime(lane_id)
        if lifetime == "active":
            return None
        return _settle_no_evidence(led, lane_id, dispatch_id, lifetime=lifetime, verdict=verdict,
                                   witness=witness, emit=emit)
    if verdict["verdict"] != DONE:
        park_kind = verdict["verdict"] if verdict["verdict"] in (EVIDENCE_UNRESOLVED, REVIEW_UNAVAILABLE) else None
        return _settle_park(led, lane_id, dispatch_id, "evidence", verdict["reason"], emit,
                            verdict=verdict, witness=witness, park_kind=park_kind)

    # Evidence is complete. The controller now re-runs the lane's OWN test command: the sidecar
    # says the worker believes it succeeded, this says it actually did. The run gets the acceptance
    # budget the LANE recorded at dispatch, and this runner's deadline is not allowed to shrink it:
    # a command that has already started runs to its own budget (staleness is a different clock).
    exit_code, tail, note, outcome = _run_test(lane, worktree)
    if outcome == OVER_BUDGET:
        # The lane's own recorded budget ran out. Not the environment's fault and not a red test, so
        # it gets no infra retry and no breaker strike: it parks, naming what actually happened.
        return _settle_over_budget(led, lane_id, dispatch_id, note, emit, verdict=verdict,
                                   witness=witness, exit_code=exit_code, tail=tail,
                                   budget_s=acceptance_budget_s(lane))
    retries = 0
    while outcome == INFRA and retries < INFRA_RUN_RETRIES and time.monotonic() < deadline:
        # The command never ran at all. That is the environment's fault, not the lane's tests, so it
        # is retried in place a BOUNDED number of times before it is recorded as ``infra`` rather
        # than settled as a failure (issue #1). A permanent fault (a program that is not installed)
        # survives the retry, which is exactly why the retry is bounded and why the ledger caps how
        # many infra outcomes a lane may accumulate.
        retries += 1
        time.sleep(min(INFRA_RETRY_BACKOFF, max(0.0, deadline - time.monotonic())))
        emit(f"[{lane_id}] INFRA_RETRY attempt={retries}/{INFRA_RUN_RETRIES} {note}")
        exit_code, tail, note, outcome = _run_test(lane, worktree)
    if outcome == INFRA:
        return _settle_infra(led, lane_id, dispatch_id, note, emit, verdict=verdict,
                             witness=witness, exit_code=exit_code, tail=tail, retries=retries)
    if exit_code != 0:
        return _settle_park(led, lane_id, dispatch_id, "test_failure",
                            f"controller test re-run failed: {note}", emit,
                            verdict=verdict, witness=witness, exit_code=exit_code)

    evidence = {
        "controller": "advance",
        "dispatch_id": dispatch_id,
        "verdict": DONE,
        "sidecar": verdict["evidence"]["sidecar"],
        "sidecar_facts": verdict["evidence"].get("facts"),
        "harness": lane.get("harness"),
        "harness_reported": (verdict["evidence"].get("facts") or {}).get("harness"),
        "model_requested": lane.get("model_requested"),
        "model_reported": (verdict["evidence"].get("facts") or {}).get("model"),
        # Advisory only, recorded on every settlement so the reader never has to re-derive it: an
        # accepted lane can still carry a disagreeing harness self-report.
        "identity_warning": str(verdict["evidence"].get("identity_warning") or ""),
        "test_cmd": [str(part) for part in (lane.get("test_cmd") or [])],
        # What the acceptance re-run ran under: the lane's recorded launch PATH, or ``None`` when
        # the lane has none and the runner's ambient environment was inherited. Recorded so a
        # reader can tell which environment a passing re-run actually saw.
        "unit_path": str(lane.get(UNIT_PATH_FIELD) or "").strip() or None,
        "test_exit_code": exit_code,
        "test_result": "pass",
        "test_output_tail": tail,
        "worktree": str(worktree),
        "witness": witness,
        "checked_at": _utc_now(),
        # The ONE builder, from the ledger: the lane's own commit or nothing — never the commit the
        # lane started from (which is what this path used to record, name and all, for a lane with
        # uncommitted work).
        **git_evidence(worktree, lane.get("base_commit")),
    }
    result = led.settle(lane_id, dispatch_id, True, evidence=evidence,
                        reason="accepted on evidence: valid sidecar + controller test exit 0")
    if not result.get("accepted"):
        emit(f"[{lane_id}] SETTLE_REFUSED {result.get('reason')}")
        return _Decision(lane_id, "parked", "evidence",
                         f"ledger refused the settlement: {result.get('reason')}")
    if evidence["identity_warning"]:
        emit(f"[{lane_id}] IDENTITY_WARNING advisory {evidence['identity_warning']}")
    emit(f"[{lane_id}] ACCEPTED exit=0 OK")
    return _Decision(lane_id, "accepted", "ok", f"exit=0 ({note})")


def _max_rounds(led: Ledger, timeout: float, poll: float) -> int:
    """A generous absolute ceiling on loop iterations — a safety net, not the primary bound.

    The deadline is the real bound; this only guarantees termination if some future edit makes a
    round do no work and no sleeping.
    """
    lanes = max(1, len(led.data.get("lanes") or {}))
    polls = int(max(0.0, timeout) / max(0.01, poll)) + 2
    return max(1000, polls * 4 + 4 * lanes * (led.failure_limit + 1) + 32)


def advance(ledger_path: Path | str, *, timeout: int = 600, poll: float = 2.0,
            emit: Callable[[str], None] = print) -> dict:
    """Wait for the DISPATCHED lanes, settle the ledger from evidence, then **return**.

    Returns ``{"accepted": [...], "needs_review": [...], "stalled": [...], "timed_out": [...],
    "final": {status: count}}``. It returns when every in-flight lane has been decided or when
    ``timeout`` seconds have passed — whichever comes first. There is no unbounded wait and no
    daemon.

    At the deadline the unit's lifetime decides what a missing sidecar means (see
    :func:`_unit_lifetime`), and it is the only thing that may. A lane whose unit is still
    **active** is reported in ``timed_out`` and left ``dispatched``: a live worker may still write
    its evidence, and failing it would invent a failure. A lane whose unit has **finished**
    (reported in ``stalled``) or is **absent** (reported in ``timed_out``) cannot produce one any
    more, so it is settled as a failed dispatch: it lands in ``needs_review`` — ``blocked`` once
    the circuit breaker trips — and never in a non-terminal limbo. ``stalled``/``timed_out``
    therefore say *why* a lane ran out of clock; a settled lane appears there and in
    ``needs_review`` both.

    Retrying a *failure* is deliberately absent: a lane that fails its own tests is parked, the
    ledger's circuit breaker owns the failure streak, and re-dispatching a genuinely broken lane is
    a conductor decision ("a lane that fails parks at blocked/needs_review rather than being retried
    blindly").

    The one retry this runner owns is the **infra** one, because it is not a lane failure at all:
    when the acceptance command cannot be *started* (ENOENT) or the controller's budget kills it, the
    re-run is retried in place a bounded number of times (``INFRA_RUN_RETRIES``) and the outcome is
    then recorded as ``infra`` — the lane returns to ``ready`` (re-dispatchable through the ordinary
    gate, which is what relaunches its worker) with ``consecutive_failures`` untouched. After
    ``Ledger.infra_limit`` infra outcomes the ledger parks the lane ``blocked``, naming the
    environment in ``blocked_reason``, so a permanently unrunnable command still reaches a human.

    A lane whose acceptance command outruns the budget it recorded at dispatch is the other
    non-failure outcome, and it takes no retry at all: it is settled ``over_budget``, which parks the
    lane ``blocked`` (``needs_review``) with the streak untouched and no infra attempt spent — the
    budget is what has to change, and re-running the same command unchanged would blow it again.
    """
    path = Path(ledger_path)
    led = Ledger(path)
    timeout_s = max(0.0, float(timeout))
    poll_s = max(0.01, float(poll))
    deadline = time.monotonic() + timeout_s

    accepted: list[str] = []
    needs_review: list[str] = []
    stalled: list[str] = []
    timed_out: list[str] = []
    decided: dict[str, str] = {}
    rounds = 0
    ceiling = _max_rounds(led, timeout_s, poll_s)

    while rounds < ceiling and time.monotonic() < deadline:
        rounds += 1
        for lane_id in led.dispatched():
            if lane_id in decided:
                continue
            decision = _attempt_lane(led, lane_id, deadline=deadline, emit=emit)
            if decision is None:
                continue
            decided[lane_id] = decision.kind
            if decision.kind == "accepted":
                _append_unique(accepted, lane_id)
            else:
                _append_unique(needs_review, lane_id)
        undecided = [lane_id for lane_id in led.dispatched() if lane_id not in decided]
        if not undecided:
            break                          # nothing left in flight: return now, do not idle
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_s, remaining))

    # Lanes still in flight at the hard stop. The unit's LIFETIME is what decides the silence, and
    # it is the only thing that may:
    #
    # * **active** — the worker is running right now, so it is reported as still in flight and left
    #   ``dispatched``. Absence of evidence is reported, not settled: inventing a failure for a
    #   worker that may still write its sidecar would poison the failure streak.
    # * **finished** — systemd saw the main process exit. No sidecar is coming, so this *is* a
    #   failed dispatch and is settled as one.
    # * **absent** — there is no unit record and no exit to report, so nothing is running under
    #   this lane either. Also a failed dispatch, and also settled.
    #
    # The last two are the v0.1 failure shape this runner exists to prevent — a ledger frozen in a
    # non-terminal state with no unit and no artifact to explain it. A lane that produced no
    # artifact is a failed dispatch, and counting it toward the breaker invents nothing.
    for lane_id in led.dispatched():
        if lane_id in decided:
            continue
        lifetime = _unit_lifetime(lane_id)
        if lifetime == "active":
            _append_unique(timed_out, lane_id)
            decided[lane_id] = "timed_out"
            emit(f"[{lane_id}] TIMED_OUT no acceptable sidecar within {timeout_s:g}s "
                 f"(unit still active, left dispatched)")
            continue
        lane = led.lane(lane_id)
        # The witness is recorded as corroboration, and told the truth about the unit: "finished"
        # is a witnessed exit, "absent" is only an absence (never dressed up as an exit).
        unit_active = False if lifetime == "finished" else None
        verdict = lane_verdict(lane, lane_id=lane_id, unit_active=unit_active,
                               timeout_exceeded=True)
        witness = witness_verdict(lane, lane_id=lane_id, unit_active=unit_active,
                                  timeout_exceeded=True)
        decision = _settle_no_evidence(led, lane_id, str(lane.get("dispatch_id") or ""),
                                       lifetime=lifetime, verdict=verdict, witness=witness,
                                       emit=emit)
        decided[lane_id] = decision.kind
        _append_unique(needs_review, lane_id)
        if lifetime == "finished":
            _append_unique(stalled, lane_id)
            emit(f"[{lane_id}] STALLED unit exited with no acceptable sidecar")
        else:
            _append_unique(timed_out, lane_id)
            emit(f"[{lane_id}] TIMED_OUT no acceptable sidecar within {timeout_s:g}s")

    final = led.summary()
    summary = {"accepted": accepted, "needs_review": needs_review, "stalled": stalled,
               "timed_out": timed_out, "final": final}
    emit(f"oprun-advance: accepted={len(accepted)} needs_review={len(needs_review)} "
         f"stalled={len(stalled)} timed_out={len(timed_out)} "
         f"all_terminal={str(all(status in TERMINAL for status in final)).lower()} "
         f"final={json.dumps(final, sort_keys=True)}")
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python3 scripts/advance.py --ledger PATH [--timeout SEC] [--poll SEC] [--json]``.

    One line per lane as it settles, then the final summary. With ``--json`` the per-lane lines
    go to stderr and stdout carries exactly one JSON object, so the runner composes in a pipe.

    The exit status is 0 for a completed run *whatever the lane outcomes*: lane status lives in
    the ledger, and a non-zero exit here would abort a caller that is supposed to inspect it
    (``set -e``) rather than report on it. Genuine failures — usage, unreadable ledger, a crash
    in the runner itself — are non-zero.
    """
    parser = argparse.ArgumentParser(description="oprun v0.2 advance — bounded unattended settle")
    parser.add_argument("--ledger", required=True, type=Path, help="path to state.json")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="hard wall-clock budget in seconds (default: 600)")
    parser.add_argument("--poll", type=float, default=2.0,
                        help="seconds between evidence checks (default: 2)")
    parser.add_argument("--json", action="store_true",
                        help="print the final summary as one JSON object on stdout")
    args = parser.parse_args(argv)

    if not args.ledger.exists():
        print(f"oprun-advance: ledger not found: {args.ledger}", file=sys.stderr)
        return 2

    def emit(line: str) -> None:
        print(line, file=sys.stderr if args.json else sys.stdout, flush=True)

    try:
        summary = advance(args.ledger, timeout=args.timeout, poll=args.poll, emit=emit)
    except Exception as exc:               # a crash must be loud, never a silent stall
        print(f"oprun-advance: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
