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

``done`` requires all three to agree. Anything less is ``needs_input`` (a human decides),
``stalled`` (the timeout passed with no acceptable artifact), ``pending`` (the unit is still
running and there is no artifact yet), or ``failed`` (the ledger parked the lane).

Boundaries, deliberately: no model, no network, and no subprocess except ``systemctl --user
is-active`` (process **lifetime**) and the lane's own ``test_cmd`` (the acceptance re-run). systemd
is **never** asked whether a lane is done — a dead unit with a valid sidecar can be ``done``; a
live unit with no sidecar is not.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from ledger import BLOCKED, DISPATCHED, FAILED, Ledger

#: The complete verdict vocabulary. Every return value of :func:`probe` is one of these.
VERDICTS = ("done", "pending", "needs_input", "stalled", "failed")

#: Registry ids, matching ``harnesses.py``. Every shipped harness must be mapped below: an
#: unmapped harness that reached :func:`probe` would be silently treated as success.
HARNESS_IDS = ("cursor-agent", "codex", "droid", "claude", "hermes", "pi", "opencode")

#: Ledger statuses that mean the lane is parked, not in flight.
PARKED_STATUSES = frozenset({FAILED, BLOCKED})

#: Sidecar filename, relative to the lane's ``.oprun`` directory.
SIDECAR_TEMPLATE = "result.{dispatch_id}.json"

#: Seconds the acceptance re-run of a lane's own ``test_cmd`` may take (``timeout(1)`` rc 124).
TEST_CMD_TIMEOUT_SECONDS = 900

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


def _relative_candidates(candidate: str, root: Path, base: Path) -> Iterator[Path]:
    """Resolution order for an evidence path: the lane root first, the sidecar dir as fallback."""
    path = Path(candidate)
    if path.is_absolute():
        yield path
        return
    yield root / path
    if base != root:
        yield base / path


def _roots(lane: dict, base: Path) -> Path:
    """The lane root evidence paths are relative to (the worktree, else the sidecar's parent)."""
    worktree = lane.get("worktree")
    if worktree:
        return Path(str(worktree)).expanduser()
    return base.parent


def _missing_evidence_paths(sidecar: dict, root: Path, base: Path) -> list[str]:
    """Every evidence path named by ``sidecar`` that does not resolve on disk.

    ``evidence.files`` is a list; ``evidence.log_path`` is checked only when non-empty (the canary
    sidecars ship an empty string, which means "no log", not "a missing file").
    """
    evidence = sidecar.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    files = evidence.get("files") or []
    if isinstance(files, str):
        files = [files]
    candidates = [str(item) for item in files if str(item).strip()]
    log_path = str(evidence.get("log_path") or "").strip()
    if log_path:
        candidates.append(log_path)
    missing: list[str] = []
    for candidate in candidates:
        if not any(path.exists() for path in _relative_candidates(candidate, root, base)):
            missing.append(candidate)
    return missing


def _run_test_cmd(test_cmd: list[str], worktree: Path) -> tuple[int, str]:
    """The controller's OWN re-run of the lane's ``test_cmd``. Returns ``(rc, output tail)``.

    Nothing the worker reported matters here: this is the acceptance test executing on the
    controller's side. rc 127 = could not run, rc 124 = timed out.
    """
    if not worktree.is_dir():
        return 127, f"worktree is not a directory: {worktree}"
    try:
        proc = subprocess.run(
            [str(part) for part in test_cmd],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=TEST_CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, f"test_cmd timed out after {TEST_CMD_TIMEOUT_SECONDS}s"
    except (OSError, ValueError) as exc:
        return 127, f"test_cmd could not be run: {exc}"
    tail = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, tail.strip()[-400:]


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
    controller re-runs ``lane["test_cmd"]`` and it exits 0; every evidence path resolves.

    ``unit_active`` (systemd lifetime) and ``timeout_exceeded`` only classify *absence* of an
    artifact: pending while the unit runs, ``stalled`` once the timeout has passed, otherwise
    ``needs_input``. A parked (FAILED/BLOCKED) ledger lane is ``failed``, full stop.
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
        "test_rc": None,
        "missing_evidence_paths": [],
    }

    def verdict(name: str, reason: str) -> dict:
        return {"verdict": name, "reason": reason, "evidence": dict(evidence)}

    def absent(reason: str) -> dict:
        """Classify the absence of an acceptable artifact — never as success."""
        if timeout_exceeded:
            return verdict("stalled", f"{reason}; timeout exceeded with no acceptable artifact")
        if unit_active:
            return verdict("pending", f"{reason}; unit still active")
        return verdict("needs_input", f"{reason}; unit is not active and no artifact will appear")

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

    # 4. the controller re-runs the lane's OWN test command.
    if not test_cmd:
        return verdict("needs_input", "lane has no test_cmd: evidence alone cannot be accepted")
    rc, tail = _run_test_cmd(test_cmd, root)
    evidence["test_rc"] = rc
    evidence["test_output_tail"] = tail
    if rc != 0:
        return verdict("needs_input",
                       f"controller re-run of test_cmd exited {rc} "
                       f"({test_cmd!r}): {tail[-200:]!r}")

    # 5. every evidence path must resolve.
    missing = _missing_evidence_paths(sidecar, root, base)
    evidence["missing_evidence_paths"] = missing
    if missing:
        return verdict("needs_input", f"evidence paths do not resolve: {missing!r}")

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
