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
2. **A unit's lifetime answers "should I keep waiting?" — never "is it done?"** (see
   :func:`_unit_finished`). It is used to stop waiting on a provably-exited worker, never as a
   completion signal.
3. **Every loop is bounded.** The runner returns when the lanes are decided or when the deadline
   passes. A lane that fails is *parked*, not retried blindly: the ledger's circuit breaker owns
   the failure streak, and a ``blocked`` lane is terminal as far as this process is concerned.

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
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

from ledger import TERMINAL, Ledger

import launch  # sibling module: the only detach/lifetime authority (scripts/launch.py)

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

#: Ceiling for one controller test re-run. Also capped by the remaining run budget, so the whole
#: ``advance`` call stays inside its timeout even if a lane's tests hang.
TEST_TIMEOUT = 300.0
#: Characters of test output kept as evidence (the tail is where failures are named).
TEST_TAIL = 2000
#: Files hashed for the uncommitted-work evidence path, so one huge lane cannot bloat the ledger.
HASH_LIMIT = 50

_GIT_TIMEOUT = 30.0

#: Verdicts a lane can carry, matching ``probe``'s vocabulary.
DONE, PENDING, NEEDS_INPUT, STALLED, FAILED_VERDICT = (
    "done", "pending", "needs_input", "stalled", "failed",
)


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


# --- controller evidence -----------------------------------------------------


def _run_test(lane: dict, worktree: Path, deadline: float) -> tuple[int, str, str]:
    """Re-run the lane's OWN ``test_cmd`` in its worktree. Returns ``(exit_code, tail, note)``.

    The command and the working directory come from the ledger, never from the worker: a lane
    can neither choose nor skip the test that decides its own acceptance.
    """
    cmd = [str(part) for part in (lane.get("test_cmd") or [])]
    if not cmd:
        return 1, "", "lane has no test_cmd, so nothing can be re-run to accept it"
    budget = min(TEST_TIMEOUT, max(1.0, deadline - time.monotonic()))
    try:
        proc = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True,
                              timeout=budget)
    except subprocess.TimeoutExpired:
        return 124, "", f"test command exceeded {budget:.0f}s"
    except OSError as exc:
        return 127, "", f"test command could not run: {exc}"
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-TEST_TAIL:]
    return proc.returncode, tail, f"exited {proc.returncode}"


def _hash_path(base: Path, rel: str) -> str | None:
    """A content hash for one changed path: the file's sha256, or a digest of a whole directory.

    An untracked directory (``.oprun/`` and friends) is part of what a lane produced, so it is
    digested rather than reported as unknown; a path that is gone (deleted/renamed away) is
    honestly ``None``.
    """
    target = base / rel
    try:
        if target.is_file():
            return hashlib.sha256(target.read_bytes()).hexdigest()
        if target.is_dir():
            digest = hashlib.sha256()
            files = sorted(path for path in target.rglob("*") if path.is_file())
            for path in files[:HASH_LIMIT]:
                digest.update(str(path.relative_to(base)).encode("utf-8"))
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            return f"dir:{digest.hexdigest()}:{len(files)}files"
        return None
    except OSError:
        return None


def _git_evidence(worktree: Path) -> dict:
    """``commit`` / ``uncommitted`` / ``hashes`` for the lane's worktree.

    A lane that did not commit gets ``uncommitted=True`` plus content hashes of its changed
    files — never the shared base SHA dressed up as the lane's work (the defect that made three
    parallel lanes' evidence indistinguishable). Unavailable git is recorded, not guessed.
    """
    def git(*args: str, strip: bool = True) -> tuple[int, str]:
        try:
            proc = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                                  text=True, timeout=_GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return 128, str(exc)
        out = proc.stdout or ""
        # Porcelain output must NOT be stripped as a whole: the first line of `status --porcelain`
        # begins with a space for worktree-only changes (" M work.txt"), and a blanket strip turns
        # the path into "ork.txt" — silently hashing a file that does not exist.
        return proc.returncode, out.strip() if strip else out

    rc, head = git("rev-parse", "HEAD")
    if rc != 0 or not head:
        return {"commit": None, "uncommitted": None, "hashes": {},
                "git_error": head or f"git rev-parse exited {rc}"}
    # Porcelain paths are relative to the REPOSITORY ROOT, not to `-C`, so the root is asked
    # for explicitly: a lane worktree is normally the root, but a lane pointed at a subdirectory
    # must not silently hash the wrong files.
    rc_root, root = git("rev-parse", "--show-toplevel")
    base = Path(root) if rc_root == 0 and root else worktree
    rc_status, status = git("status", "--porcelain", strip=False)
    changed = [line[3:].strip().split(" -> ")[-1] for line in status.splitlines() if line.strip()]
    hashes = {rel: _hash_path(base, rel) for rel in changed[:HASH_LIMIT]}
    return {"commit": head, "uncommitted": bool(changed), "hashes": hashes}


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
                 witness: dict | None = None, exit_code: int | None = None) -> _Decision:
    """Refuse an outcome: the lane goes to ``needs_review`` — and never to ``completed``."""
    evidence = {
        "controller": "advance",
        "dispatch_id": dispatch_id,
        "verdict": (verdict or {}).get("verdict"),
        "rejected_because": reason,
        "sidecar": (verdict or {}).get("evidence", {}).get("sidecar"),
        "test_exit_code": exit_code,
        "witness": witness,
        "checked_at": _utc_now(),
    }
    result = led.settle(lane_id, dispatch_id, False, evidence=evidence, reason=reason)
    if not result.get("accepted"):
        emit(f"[{lane_id}] SETTLE_REFUSED {result.get('reason')}")
        return _Decision(lane_id, "parked", "evidence",
                         f"ledger refused the settlement: {result.get('reason')}")
    suffix = f" exit={exit_code}" if exit_code is not None else ""
    emit(f"[{lane_id}] NEEDS_REVIEW{suffix} {reason}")
    return _Decision(lane_id, "parked", category, reason)


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
        return _settle_park(led, lane_id, dispatch_id, "evidence", verdict["reason"], emit,
                            verdict=verdict, witness=witness)
    if verdict["verdict"] != DONE:
        return _settle_park(led, lane_id, dispatch_id, "evidence", verdict["reason"], emit,
                            verdict=verdict, witness=witness)

    # Evidence is complete. The controller now re-runs the lane's OWN test command: the sidecar
    # says the worker believes it succeeded, this says it actually did.
    exit_code, tail, note = _run_test(lane, worktree, deadline)
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
        "model_requested": lane.get("model_requested"),
        "test_cmd": [str(part) for part in (lane.get("test_cmd") or [])],
        "test_exit_code": exit_code,
        "test_result": "pass",
        "test_output_tail": tail,
        "worktree": str(worktree),
        "witness": witness,
        "checked_at": _utc_now(),
        **_git_evidence(worktree),
    }
    result = led.settle(lane_id, dispatch_id, True, evidence=evidence,
                        reason="accepted on evidence: valid sidecar + controller test exit 0")
    if not result.get("accepted"):
        emit(f"[{lane_id}] SETTLE_REFUSED {result.get('reason')}")
        return _Decision(lane_id, "parked", "evidence",
                         f"ledger refused the settlement: {result.get('reason')}")
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
    daemon: lanes left in flight at the deadline are *reported*, never settled on a hunch.

    Retrying is deliberately absent. A lane that fails is parked; the ledger's circuit breaker
    owns the failure streak, and re-dispatching a genuinely broken lane is a conductor decision
    ("a lane that fails parks at blocked/needs_review rather than being retried blindly").
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

    # Lanes still in flight at the hard stop. Absence of evidence is reported, not settled: the
    # worker may still be running, and inventing a failure would poison the failure streak.
    for lane_id in led.dispatched():
        if lane_id in decided:
            continue
        if _unit_finished(lane_id):
            _append_unique(stalled, lane_id)
            decided[lane_id] = STALLED
            emit(f"[{lane_id}] STALLED unit exited with no acceptable sidecar")
        else:
            _append_unique(timed_out, lane_id)
            decided[lane_id] = "timed_out"
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
