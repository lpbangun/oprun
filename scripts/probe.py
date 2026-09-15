#!/usr/bin/env python3
"""oprun v0.2 ``probe`` — the deterministic completion witness.

One question: **is this lane done — proven by evidence, not by report?**

A model never decides completion here. ``probe`` reads three things that are already on disk or
already true, in this order:

1. the ledger's own status for the lane (a FAILED/BLOCKED lane is ``failed``, full stop);
2. the lane's sidecar at ``<worktree>/.oprun/result.<current dispatch_id>.json`` — the worker's
   *claim*, fenced by ``task_id`` + ``dispatch_id`` so a superseded claim is stale and a sibling
   lane's claim is foreign, and neither can ever be accepted;
3. the controller's **own re-run** of the lane's ``test_cmd``, plus a disk check of every evidence
   path the sidecar names.

``done`` requires all three to agree. Anything less is ``infra`` (the controller could not RUN the
lane's own ``test_cmd`` — ENOENT, not executable, or the controller's own clock ran out on a lane
that declared no budget: an environment fault, never a red test, and never counted as a lane
failure), ``over_budget`` (the lane RECORDED an acceptance budget at dispatch and its own command
exceeded it — no verdict was reached, so it parks for a human and touches no failure streak),
``needs_input`` (a human decides), ``stalled`` (the timeout passed with no acceptable artifact),
``pending`` (the unit is still running and there is no artifact yet), or ``failed`` (the ledger
parked the lane).

Four clocks, never one: the lane's **acceptance budget** (``test_timeout_s``, recorded at dispatch),
**staleness** (``--timeout``: how long a lane may produce no artifact), **process lifetime**
(systemd) and **human parking**. Collapsing them turns a slow suite into a fake environment fault
and kills valid workers.

Boundaries, deliberately: no model, no network, and no subprocess except ``systemctl --user
is-active`` (process **lifetime**) and the lane's own ``test_cmd`` (the acceptance re-run). systemd
is **never** asked whether a lane is done — a dead unit with a valid sidecar can be ``done``; a
live unit with no sidecar is not.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ledger import (BLOCKED, DISPATCHED, EVIDENCE_UNRESOLVED, FAILED, Ledger,
                    REVIEW_UNAVAILABLE, evidence_outcome, missing_evidence_paths)

#: The complete verdict vocabulary. Typed parks are deliberately distinct from a red product test.
#:
#: ``infra`` is the environment failing, never a red test: the controller could not RUN the lane's
#: acceptance command at all — the program is not on PATH, or a lane that declared no budget of its
#: own blew the controller's default clock. Conflating the two is issue #1, where an ENOENT inside a
#: systemd unit came back as rc 127 and was settled as a lane failure with a circuit-breaker strike
#: attached.
#:
#: ``over_budget`` is a *different* clock on purpose: the lane declared its own acceptance budget
#: (``test_timeout_s``, written at dispatch) and its command exceeded it. No test was proven red
#: either, so it is neither ``infra`` (nothing in the environment failed) nor a failure: it parks
#: through the human-review path, touches no failure streak and gets no infra retry.
VERDICTS = ("done", "infra", "over_budget", "pending", "needs_input", "stalled", "evidence_unresolved", "review_unavailable", "failed")


#: Registry ids, matching ``harnesses.py``. Every shipped harness must be mapped below: an
#: unmapped harness that reached :func:`probe` would be silently treated as success.
HARNESS_IDS = ("cursor-agent", "codex", "droid", "claude", "hermes", "pi", "opencode")

#: Ledger statuses that mean the lane is parked, not in flight.
PARKED_STATUSES = frozenset({FAILED, BLOCKED})

#: Sidecar filename, relative to the lane's ``.oprun`` directory.
SIDECAR_TEMPLATE = "result.{dispatch_id}.json"

#: The controller's own clock on a single acceptance re-run (``timeout(1)`` rc 124), and the ONE
#: place the shipped default acceptance budget is written. A lane that declared no budget of its own
#: (a legacy ledger with no ``test_timeout_s``) is read as this number, and a run THIS clock kills is
#: the environment fault ``infra`` — never a red test, and never ``over_budget``.
TEST_CMD_TIMEOUT_SECONDS = 900.0

#: The shipped default acceptance budget, in seconds — **derived** from the controller's clock above
#: so the two can never drift into two different 900s. This is the number ``dispatch`` RECORDS on a
#: lane (``--test-timeout``'s default, read by ``oprun`` and by ``advance``); a lane that recorded
#: nothing is judged by the controller's clock above, and a kill by that clock is ``infra``.
DEFAULT_TEST_TIMEOUT_S = TEST_CMD_TIMEOUT_SECONDS

#: The lane field carrying the lane's ACCEPTANCE budget, written once at dispatch and read by every
#: acceptance re-run site (this module, ``advance._run_test``, ``oprun.settle --accept``). Spelled
#: identically in those modules and pinned to one string by the suite, like ``UNIT_PATH_FIELD``.
TEST_TIMEOUT_FIELD = "test_timeout_s"

#: rc the controller assigns when the lane's ``test_cmd`` could not be STARTED at all: the program
#: is not on PATH (ENOENT), is not executable, or the arguments are malformed. The command never
#: reached a verdict of its own, so this is the environment's failure — the ``infra`` verdict —
#: and never a red test.
TEST_CMD_NOT_RUNNABLE_RC = 127

#: rc the controller assigns when the re-run blew its own wall-clock budget (``timeout(1)``
#: convention). Also the environment's fault rather than the lane's: the tests never finished.
TEST_CMD_TIMEOUT_RC = 124

#: The lane field recording the PATH the lane's worker unit was launched with (written at
#: dispatch from ``launch()``'s own report: see ``scripts/oprun.py``). Read here, never derived:
#: an absent/blank value means "not recorded", and the re-run then inherits the ambient
#: environment exactly as it did before the field existed. The same field name is pinned in
#: ``advance.py`` (``UNIT_PATH_FIELD``) and asserted identical by the suite.
UNIT_PATH_FIELD = "unit_path"

#: The one environment variable the re-run overrides. Nothing is resolved from it, ever.
PATH_ENV = "PATH"

#: Seconds ``systemctl --user is-active`` may take before liveness is reported as False.
SYSTEMCTL_TIMEOUT_SECONDS = 15

#: How often ``--wait`` re-probes while the verdict is still ``pending``.
WAIT_POLL_SECONDS = 5.0

#: The only sidecar status that can lead to ``done``.
SUCCESS_STATUS = "success"

_SESSION_LINE_RE = re.compile(r"^\s*(?:session_id|session|Session)\s*[:=]\s*([A-Za-z0-9._-]+)")
_ERROR_LINE_RE = re.compile(r"^\s*(?:error|fatal|traceback|panic)\b", re.IGNORECASE)

#: Event types that are an explicit failure, whatever else the stream says.
_FAILURE_EVENT_TYPES = frozenset({"turn.failed", "error", "session.error"})


# --- stdout -> terminal event ------------------------------------------------
def _json_objects(stdout_text: str) -> list[dict]:
    """Every JSON object in ``stdout_text``: a whole-document object plus one per JSON line.

    Harnesses mix prose and JSON on the same stream (``codex exec --json`` prints
    "Reading additional input from stdin..." first), so a line that is not JSON is skipped, never
    guessed at. Order is preserved; duplicates are harmless because parsers look at the last event.
    """
    objects: list[dict] = []
    whole = stdout_text.strip()
    if whole.startswith("{"):
        try:
            parsed = json.loads(whole)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            objects.append(parsed)
    for raw in stdout_text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def _explicit_errors(events: list[dict]) -> list[str]:
    """Explicit negative signals in an event stream — never inferred from absence."""
    found: list[str] = []
    for event in events:
        if event.get("is_error") is True:
            found.append("is_error=true")
        if event.get("type") in _FAILURE_EVENT_TYPES:
            found.append(f"type={event.get('type')}")
        if event.get("subtype") in ("error", "failed", "failure"):
            found.append(f"subtype={event.get('subtype')}")
        if event.get("api_error_status"):
            found.append(f"api_error_status={event.get('api_error_status')}")
    return found


def _session_id(stdout_text: str) -> str | None:
    """Last ``session_id:``/``Session:`` value printed by the harness, if any."""
    found: str | None = None
    for line in stdout_text.splitlines():
        match = _SESSION_LINE_RE.match(line)
        if match:
            found = match.group(1)
    return found


def _error_line(stdout_text: str) -> str | None:
    """First line of stdout that opens with an explicit error keyword, if any."""
    for line in stdout_text.splitlines():
        if _ERROR_LINE_RE.match(line):
            return line.strip()[:160]
    return None


def _parse_result_event(stdout_text: str) -> dict:
    """The shared ``{"type":"result","subtype":...,"is_error":...}`` shape.

    Used by cursor-agent, droid, claude and (defensively) pi. ``subtype`` alone is never success:
    Claude emitted ``subtype:"success"`` *while* returning ``is_error:true`` and exit 1 (a 403), so
    ``is_error`` is authoritative here and the OS exit code is authoritative at the lane level.
    """
    events = [event for event in _json_objects(stdout_text) if event.get("type") == "result"]
    if not events:
        return {"present": False, "ok": False, "detail": "no type=result event on stdout"}
    event = events[-1]
    subtype = event.get("subtype")
    is_error = event.get("is_error") is True
    ok = bool(subtype == "success" and not is_error)
    detail = f"type=result subtype={subtype!r} is_error={is_error}"
    status = event.get("api_error_status")
    if status:
        detail += f" api_error_status={status!r}"
    detail += (" -> ok" if ok else " -> NOT ok (is_error + OS exit code are authoritative)")
    return {"present": True, "ok": ok, "detail": detail}


def _parse_codex(stdout_text: str) -> dict:
    """codex ``exec --json``: the terminal event is ``turn.completed``."""
    events = _json_objects(stdout_text)
    types = [event.get("type") for event in events if event.get("type")]
    completed = "turn.completed" in types
    errors = _explicit_errors(events)
    if not completed and not errors:
        return {"present": False, "ok": False,
                "detail": f"no turn.completed/turn.failed event (observed={types!r})"}
    ok = bool(completed and not errors)
    detail = f"turn.completed={completed} errors={errors}"
    detail += (" -> ok" if ok else " -> NOT ok")
    return {"present": True, "ok": ok, "detail": detail}


def _parse_opencode(stdout_text: str) -> dict:
    """opencode ``run --format json``: the terminal event is ``step_finish``.

    Known to drop ``step_finish`` (#26855), which is exactly why absence must never be read as
    success: no step_finish means no terminal event, so the harness cannot be accepted.
    """
    events = _json_objects(stdout_text)
    types = [event.get("type") for event in events if event.get("type")]
    finished = "step_finish" in types
    errors = _explicit_errors(events)
    if not finished and not errors:
        return {"present": False, "ok": False,
                "detail": f"no step_finish event (observed={types!r}; known to drop it)"}
    ok = bool(finished and not errors)
    detail = f"step_finish={finished} errors={errors}"
    detail += (" -> ok" if ok else " -> NOT ok")
    return {"present": True, "ok": ok, "detail": detail}


def _parse_hermes(stdout_text: str) -> dict:
    """hermes chat: the witness is the OS exit code plus the session id it prints.

    With no exit code in hand, stdout can only show *that the session started*; ``ok`` therefore
    requires a session id and the absence of an explicit error signal, and the caller still owns
    the exit code.
    """
    session = _session_id(stdout_text)
    events = _json_objects(stdout_text)
    errors = _explicit_errors(events)
    problem = _error_line(stdout_text)
    present = session is not None or bool(errors)
    if not present and problem is None:
        return {"present": False, "ok": False, "detail": "no session id and no error signal on stdout"}
    ok = bool(session is not None and not errors and problem is None)
    detail = f"session={session!r} errors={errors} error_line={problem!r}"
    detail += (" -> ok" if ok else " -> NOT ok")
    return {"present": True, "ok": ok, "detail": detail}


def _parse_pi(stdout_text: str) -> dict:
    """pi ``-p``: the witness is the OS exit code; ``--mode json`` adds a terminal event.

    When a structured ``type=result`` event is present it is authoritative (``is_error`` first).
    Plain text mode has no terminal event, so any non-empty output counts only as *something was
    produced* — never as success on its own.
    """
    events = _json_objects(stdout_text)
    if any(event.get("type") == "result" for event in events):
        return _parse_result_event(stdout_text)
    errors = _explicit_errors(events)
    problem = _error_line(stdout_text)
    text = stdout_text.strip()
    if not text:
        return {"present": False, "ok": False, "detail": "no output; pi's witness is the OS exit code"}
    ok = bool(not errors and problem is None)
    detail = f"pi: no structured terminal event (witness is the OS exit code) errors={errors} error_line={problem!r}"
    detail += (" -> ok" if ok else " -> NOT ok")
    return {"present": True, "ok": ok, "detail": detail}


#: harness id -> stdout parser. Coverage is guarded by ``tests/test_harness_mapping.py``.
TERMINAL_EVENT_PARSERS: dict[str, Callable[[str], dict]] = {
    "cursor-agent": _parse_result_event,
    "codex": _parse_codex,
    "droid": _parse_result_event,
    "claude": _parse_result_event,
    "hermes": _parse_hermes,
    "pi": _parse_pi,
    "opencode": _parse_opencode,
}


def parse_terminal_event(harness: str, stdout_text: str) -> dict:
    """Extract ``{"present": bool, "ok": bool, "detail": str}`` from a harness's stdout.

    Raises :class:`KeyError` for an unknown harness: an unmapped harness must never be silently
    treated as success, and the failure has to be loud enough to stop a dispatch.
    """
    try:
        parser = TERMINAL_EVENT_PARSERS[harness]
    except KeyError:
        raise KeyError(
            f"unmapped harness {harness!r} (known: {', '.join(sorted(TERMINAL_EVENT_PARSERS))})"
        ) from None
    return parser(stdout_text)


# --- process lifetime --------------------------------------------------------
def unit_state(unit_name: str) -> bool:
    """True iff ``systemctl --user is-active <unit_name>`` reports ``active``. Lifetime ONLY.

    This answers *is the process still alive* and nothing else: it is never lane state, so it can
    keep a lane ``pending`` but can never make one ``done``. Any failure to observe the unit
    (no systemd, timeout, non-zero ``systemctl`` exit) is reported as False — a unit that cannot
    be observed as active is not evidence of liveness.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.strip() == "active"


# --- the acceptance rule -----------------------------------------------------
def _sidecar_path(base: Path, dispatch_id: str | None) -> Path | None:
    """Canonical sidecar path for the lane's CURRENT dispatch, or None if it never dispatched."""
    if not dispatch_id:
        return None
    return base / SIDECAR_TEMPLATE.format(dispatch_id=dispatch_id)


def _roots(lane: dict, base: Path) -> Path:
    """The lane root evidence paths are relative to (the worktree, else the sidecar's parent)."""
    worktree = lane.get("worktree")
    if worktree:
        return Path(str(worktree)).expanduser()
    return base.parent


def _evidence_unresolved(sidecar: dict, root: Path, base: Path) -> tuple[str, list[str]]:
    """Compatibility wrapper around the shared evidence policy."""
    return evidence_outcome(sidecar, missing_evidence_paths(sidecar, root, base))


def recorded_test_timeout_s(lane: dict) -> float | None:
    """The acceptance budget the lane **declared at dispatch**, or ``None`` when it declared none.

    Read from the lane dict handed in — never sniffed from the environment, never recomputed, never
    defaulted here (see :func:`acceptance_budget_s` for the defaulted read). Only a positive finite
    number counts as a declaration: a missing field, ``None``, ``0``, a negative value or a
    non-numeric string all mean "this lane declared no budget", which is exactly what a ledger
    written before the field existed looks like.
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
    """The budget the lane's acceptance re-run gets, in seconds.

    A lane that declared a budget of its own is judged against THAT budget: exceeding it is
    ``over_budget``, because the clock that ran out belongs to the lane's own contract. A lane that
    declared none — every ledger written before ``test_timeout_s`` existed — reads the controller's
    own default clock (:data:`TEST_CMD_TIMEOUT_SECONDS`, 900 seconds by default), and a kill by that
    clock is ``infra``, exactly the behaviour such a ledger had before.

    The default is read **live** off the module attribute rather than off a copy: it is the
    controller's own clock, and whoever tightens that clock must tighten this read with it.
    """
    recorded = recorded_test_timeout_s(lane)
    if recorded is not None:
        return recorded
    return float(TEST_CMD_TIMEOUT_SECONDS)


def _test_cmd_env(unit_path: str | None) -> dict[str, str] | None:
    """The environment a lane's ``test_cmd`` is re-run under — ``None`` means "inherit ours".

    A lane whose worker unit was pinned with a PATH (``unit_path``, recorded at dispatch from
    ``launch()``'s own report) gets the *same* PATH back here: a copy of the current environment
    whose ``PATH`` is exactly that recorded value. Without this, the worker and the controller
    re-run the lane's acceptance command under two different PATHs by construction, so "the test
    the worker passed" and "the test the controller can run" need not be the same test.

    Deliberately nothing else: no ``shutil.which``, no resolution, no heuristic, no default
    entries. An absent or blank recording is not a PATH, and the caller then passes ``env=None``
    so the child inherits the ambient environment — the behaviour every older ledger keeps.

    ``advance._test_cmd_env`` is this function's twin (``advance`` must run with or without this
    module) and ``scripts/oprun.py``'s ``settle --accept`` re-run calls this one directly rather
    than keeping a third copy. Every one of them reads the same lane field.
    """
    recorded = str(unit_path or "").strip()
    if not recorded:
        return None
    return {**os.environ, PATH_ENV: recorded}


def _run_test_cmd(test_cmd: list[str], worktree: Path,
                  unit_path: str | None = None, *,
                  budget_s: float | None = None) -> tuple[int, str, str]:
    """The controller's OWN re-run of the lane's ``test_cmd``: ``(rc, output tail, reason)``.

    Nothing the worker reported matters here: this is the acceptance test executing on the
    controller's side. ``unit_path`` is the PATH the lane's worker unit was launched with, when
    the ledger has one (see :func:`_test_cmd_env`); the command then runs under that PATH instead
    of the controller's own. ``budget_s`` is the budget the run gets — the lane's recorded
    ``test_timeout_s`` (see :func:`acceptance_budget_s` when the caller wants the defaulted read);
    ``None`` falls back to the controller's own clock, :data:`TEST_CMD_TIMEOUT_SECONDS`.

    ``reason`` is non-empty **only** when the run never produced a verdict of the command's own:

    * the command could not be started (ENOENT/OSError/ValueError) -> rc 127;
    * the budget ran out -> rc 124.

    Which *clock* that budget was decides the caller's verdict, not this function's: a declared
    lane budget is ``over_budget``, the controller's own default clock is ``infra``. A non-zero rc
    with an empty ``reason`` is the lane's own failing test. The two are kept apart on purpose
    (issue #1): an environment fault settled as a red test parks a lane and feeds the circuit
    breaker for something the lane never did.
    """
    budget = float(budget_s) if budget_s is not None else float(TEST_CMD_TIMEOUT_SECONDS)
    if not worktree.is_dir():
        return TEST_CMD_NOT_RUNNABLE_RC, f"worktree is not a directory: {worktree}", \
            f"worktree is not a directory: {worktree}"
    try:
        proc = subprocess.run(
            [str(part) for part in test_cmd],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=budget,
            env=_test_cmd_env(unit_path),
        )
    except subprocess.TimeoutExpired:
        reason = f"test_cmd timed out after {budget:g}s (its acceptance budget)"
        return TEST_CMD_TIMEOUT_RC, reason, reason
    except (OSError, ValueError) as exc:
        return TEST_CMD_NOT_RUNNABLE_RC, f"test_cmd could not be run: {exc}", \
            f"test_cmd could not be run: {exc}"
    tail = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, tail.strip()[-400:], ""


def _read_sidecar(path: Path | None) -> tuple[dict | None, str]:
    """Read one sidecar JSON document. Returns ``(sidecar, error)`` — ``(None, "")`` when absent."""
    if path is None or not path.is_file():
        return None, ""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"sidecar unreadable: {exc}"
    if not isinstance(document, dict):
        return None, "sidecar is not a JSON object"
    return document, ""


def _stray_sidecar(base: Path, dispatch_id: str | None) -> str:
    """Diagnostic note when the CURRENT dispatch has no sidecar but older ones are lying around.

    Never changes a verdict; it only turns "no artifact" into an answer that names the stale or
    foreign artifact a human would otherwise have to hunt for.
    """
    if not base.is_dir():
        return ""
    notes: list[str] = []
    for path in sorted(base.glob("result.*.json")):
        name = path.name
        if dispatch_id and name == SIDECAR_TEMPLATE.format(dispatch_id=dispatch_id):
            continue
        sidecar, error = _read_sidecar(path)
        if sidecar is None:
            notes.append(f"{name} ({error or 'unreadable'})")
            continue
        notes.append(f"{name} (task_id={sidecar.get('task_id')!r} "
                     f"dispatch_id={sidecar.get('dispatch_id')!r})")
    return "; ".join(notes)


def probe(lane: dict, *, unit_active: bool, timeout_exceeded: bool,
          sidecar_dir: Path | None = None) -> dict:
    """Return ``{"verdict": str, "reason": str, "evidence": dict}`` for one lane.

    Pure and deterministic: the same inputs give the same verdict. I/O is limited to reading the
    lane's sidecar and re-running its ``test_cmd`` — no network, no model, no other subprocess.

    Acceptance (all five, in this order): the sidecar exists at
    ``<worktree>/.oprun/result.<dispatch_id>.json`` for the ledger's CURRENT dispatch; its
    ``task_id``/``dispatch_id`` match that lane and dispatch; ``status == "success"``; the
    controller re-runs ``lane["test_cmd"]`` and it exits 0; every evidence path resolves. The
    re-run executes under the PATH the lane's worker unit was launched with
    (``lane["unit_path"]``, recorded at dispatch), falling back to the ambient environment when the
    lane has no recorded PATH — so a lane's acceptance command runs where its worker ran it.

    ``unit_active`` (systemd lifetime) and ``timeout_exceeded`` only classify *absence* of an
    artifact: ``pending`` while the unit runs **and** the cutoff has not passed, ``stalled`` once it
    has (whatever the unit is doing — the reason and the evidence carry the liveness the caller would
    otherwise have to ask systemd for), otherwise ``needs_input``. A parked (FAILED/BLOCKED) ledger
    lane is ``failed``, full stop.

    Two verdicts are not about the lane's work at all, and they are different clocks:
    ``infra`` when the acceptance re-run cannot be *started*, or a lane that declared no budget of
    its own is killed by the controller's default clock — the command never ran, so no test is
    proven red and the lane is not failing; and ``over_budget`` when the lane DID declare an
    acceptance budget (``test_timeout_s``) and its own command outran it — also "no verdict was
    reached", but the clock that ran out belongs to the lane's own contract, so it parks for review
    instead of being retried as an environment fault. A red re-run (a real non-zero exit of the
    command itself) stays ``needs_input``, exactly as before.
    """
    status = lane.get("status")
    dispatch_id = lane.get("dispatch_id")
    lane_id = lane.get("lane_id") or lane.get("id")
    worktree_value = lane.get("worktree")
    worktree = Path(str(worktree_value)).expanduser() if worktree_value else None
    base = Path(sidecar_dir) if sidecar_dir is not None else (
        (worktree / ".oprun") if worktree is not None else Path(".oprun")
    )
    root = _roots(lane, base)
    path = _sidecar_path(base, dispatch_id)
    test_cmd = [str(part) for part in lane.get("test_cmd") or []]
    # Recorded at dispatch on the lane's own record: the PATH its worker unit was pinned with.
    # Read from the lane dict handed in — never sniffed from the environment, never recomputed.
    unit_path = str(lane.get(UNIT_PATH_FIELD) or "").strip()
    # The ACCEPTANCE budget, also read from the lane's own record: the re-run gets exactly what the
    # lane declared at dispatch (`over_budget` when that runs out), and a lane that declared nothing
    # gets the shipped default clock (the environment's `infra`). Two clocks, deliberately not one.
    recorded_budget = recorded_test_timeout_s(lane)
    budget = acceptance_budget_s(lane)

    evidence: dict[str, Any] = {
        "lane_id": lane_id,
        "dispatch_id": dispatch_id,
        "ledger_status": status,
        "worktree": str(worktree) if worktree is not None else None,
        "sidecar": str(path) if path is not None else None,
        "sidecar_present": False,
        "sidecar_status": None,
        "harness": lane.get("harness"),
        "unit_active": bool(unit_active),
        "timeout_exceeded": bool(timeout_exceeded),
        "test_cmd": test_cmd,
        # ``None`` means "no recorded PATH": the re-run then inherits the ambient environment.
        "unit_path": unit_path or None,
        # The budget this lane's acceptance re-run will get, and whether the lane declared it.
        "acceptance_budget_s": budget,
        "budget_recorded": recorded_budget is not None,
        "test_rc": None,
        "missing_evidence_paths": [],
    }

    def verdict(name: str, reason: str) -> dict:
        return {"verdict": name, "reason": reason, "evidence": dict(evidence)}

    def absent(reason: str) -> dict:
        """Classify the absence of an acceptable artifact — never as success.

        The frozen rule (C8, wording untouched): *"Lane with no sidecar past ``--timeout``: probe in
        ``{needs_input,stalled}``; must not remain pending; must not auto-wait forever."* So past the
        cutoff the answer is ``stalled`` whatever the unit is doing — ``pending`` is only valid
        *before* the cutoff, because a lane that has produced no artifact for a whole timeout is a
        fact a conductor has to escalate even while the process is still alive. The unit's liveness is
        not dropped: it is surfaced in the reason and in the evidence, so a reader can tell "still
        running, probe did not wait" from "nothing is running at all". ``advance`` may separately
        report such a lane ``timed_out`` and leave it ``dispatched`` — that is scheduler behaviour,
        not a probe classification.
        """
        liveness = "unit still active" if unit_active else "unit is not active"
        if timeout_exceeded:
            note = f"{liveness}; probe did not wait" if unit_active else liveness
            evidence["liveness_note"] = note
            return verdict("stalled", f"{reason}; timeout exceeded with no acceptable artifact "
                                      f"({note})")
        evidence["liveness_note"] = liveness
        if unit_active:
            return verdict("pending", f"{reason}; {liveness}")
        return verdict("needs_input", f"{reason}; {liveness} and no artifact will appear")

    # 1. the ledger's own word: a parked lane is failed, and no sidecar can overrule that.
    if status in PARKED_STATUSES:
        parked = lane.get("blocked_reason") or lane.get("needs_review_reason") or ""
        return verdict("failed", f"ledger says {status!r}{': ' + str(parked) if parked else ''}")

    if path is None:
        return absent("lane has no dispatch_id (never dispatched)")

    sidecar, error = _read_sidecar(path)
    evidence["sidecar_present"] = sidecar is not None
    if sidecar is None:
        stray = _stray_sidecar(base, dispatch_id)
        note = f"no sidecar at {path}"
        if stray:
            note += f" (stale/foreign artifacts present: {stray})"
        if error:
            note = f"{note} ({error})"
        return absent(note)

    evidence["sidecar_status"] = sidecar.get("status")
    evidence["harness_reported"] = sidecar.get("harness")
    evidence["model_reported"] = sidecar.get("model")
    evidence["task_id"] = sidecar.get("task_id")
    evidence["sidecar_dispatch_id"] = sidecar.get("dispatch_id")

    # 2. fencing: the ledger's CURRENT dispatch is the only one that may settle this lane.
    if str(sidecar.get("dispatch_id")) != str(dispatch_id):
        return verdict("needs_input",
                       f"stale sidecar: dispatch_id {sidecar.get('dispatch_id')!r} is not "
                       f"the current dispatch {dispatch_id!r}")
    if lane_id is not None and str(sidecar.get("task_id")) != str(lane_id):
        return verdict("needs_input",
                       f"foreign sidecar: task_id {sidecar.get('task_id')!r} is not "
                       f"lane {lane_id!r}")

    # 3. the worker's own claim.
    if str(sidecar.get("status")) != SUCCESS_STATUS:
        return verdict("needs_input",
                       f"sidecar reports status={sidecar.get('status')!r}, not "
                       f"{SUCCESS_STATUS!r}")

    # 4. the controller re-runs the lane's OWN test command, in the lane's OWN acceptance budget.
    if not test_cmd:
        return verdict("needs_input", "lane has no test_cmd: evidence alone cannot be accepted")
    rc, tail, reason = _run_test_cmd(test_cmd, root, unit_path=unit_path, budget_s=budget)
    evidence["test_rc"] = rc
    evidence["test_output_tail"] = tail
    if rc == TEST_CMD_TIMEOUT_RC and recorded_budget is not None:
        # The lane DECLARED this budget and its own command outran it. Nothing was proven red — the
        # command never reached a verdict — so this is not `infra` (the environment did not fail) and
        # not a failure: it is `over_budget`, which parks through the human-review path and leaves
        # the failure streak untouched.
        evidence["over_budget_reason"] = reason
        return verdict("over_budget",
                       f"acceptance command exceeded this lane's recorded "
                       f"{TEST_TIMEOUT_FIELD}={budget:g}s: {reason}; park for review "
                       f"(not infra, no failure streak, no infra retry)")
    if reason:
        # The command never reached a verdict of its own: the environment could not run it, or the
        # controller's own default clock killed a lane that declared no budget. This is `infra`,
        # never `failed`/`needs_input` — no test was proven red, so nothing here may be counted as a
        # lane failure (issue #1).
        evidence["infra_reason"] = reason
        return verdict("infra", f"controller could not run test_cmd {test_cmd!r}: {reason}")
    if rc != 0:
        return verdict("needs_input",
                       f"controller re-run of test_cmd exited {rc} "
                       f"({test_cmd!r}): {tail[-200:]!r}")

    # 5. evidence must contain an artifact, or an explicit ledger-recorded waiver.
    park_kind, missing = _evidence_unresolved(sidecar, root, base)
    evidence["missing_evidence_paths"] = missing
    if park_kind:
        evidence["park_kind"] = park_kind
        reason = ("reviewer is unavailable" if park_kind == REVIEW_UNAVAILABLE else
                  (f"evidence paths do not resolve: {missing!r}" if missing else
                   "green sidecar has neither an artifact nor an explicit waiver"))
        return verdict(park_kind, reason)

    return verdict("done", "sidecar + current dispatch + test_cmd rc=0 + evidence paths all verified")


# --- CLI ---------------------------------------------------------------------
def _dispatch_age_seconds(lane: dict) -> float | None:
    """Seconds since the current dispatch, from the lane's own history. No clock inside ``probe``."""
    for entry in reversed(lane.get("history") or []):
        if entry.get("to") == DISPATCHED and isinstance(entry.get("at"), (int, float)):
            return max(0.0, time.time() - float(entry["at"]))
    return None


def _unit_name(lane_id: str, lane: dict) -> str:
    """The systemd unit owning this lane: ``<lane.unit>`` when set, else ``oprun-<lane_id>``."""
    return str(lane.get("unit") or f"oprun-{lane_id}")


def _probe_lane(lane_id: str, lane: dict, *, timeout: float, waited_out: bool) -> dict:
    """One probe pass with lifetime + timeout resolved for the CLI."""
    age = _dispatch_age_seconds(lane)
    timed_out = waited_out or (age is not None and age > timeout)
    return probe(
        lane,
        unit_active=unit_state(_unit_name(lane_id, lane)),
        timeout_exceeded=timed_out,
        sidecar_dir=None,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: print one line naming the verdict; exit 0 unless the verdict is ``failed``.

    ``--wait`` re-probes until the verdict leaves ``pending`` or ``--timeout`` seconds pass;
    ``--json`` prints the full ``{"verdict","reason","evidence"}`` document instead of the token.
    """
    parser = argparse.ArgumentParser(prog="oprun probe", description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", required=True, type=Path, help="path to the mission ledger")
    parser.add_argument("lane_id", help="lane to probe")
    parser.add_argument("--wait", action="store_true", help="poll until the verdict is not pending")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="seconds before an artifact-less lane is stalled (default 900)")
    parser.add_argument("--json", action="store_true", help="print the full result document")
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)
    try:
        lane = dict(ledger.lane(args.lane_id))
    except KeyError as exc:
        print(f"oprun probe: {exc}", file=sys.stderr)
        return 2
    lane.setdefault("lane_id", args.lane_id)

    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        result = _probe_lane(args.lane_id, lane, timeout=args.timeout, waited_out=False)
        if not args.wait or result["verdict"] != "pending":
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = _probe_lane(args.lane_id, lane, timeout=args.timeout, waited_out=True)
            break
        time.sleep(min(WAIT_POLL_SECONDS, remaining))
        try:
            lane = dict(ledger.lane(args.lane_id))
        except KeyError:
            break
        lane.setdefault("lane_id", args.lane_id)

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result["verdict"])
        print(f"{args.lane_id}: {result['reason']}", file=sys.stderr)
    return 1 if result["verdict"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
