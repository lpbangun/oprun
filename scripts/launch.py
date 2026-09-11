#!/usr/bin/env python3
"""oprun v0.2 launch — start a worker as a DETACHED transient systemd user unit.

Why a unit and not a shell trick: a unit started with ``systemd-run --user`` lands in
``user@1000.service/app.slice``, outside the chat/login session's own scope, so closing the
desktop cannot reap the worker — and systemd, not the launcher, keeps the process lifetime and
the main process's exit status readable from outside the process. A backgrounded shell job can
promise neither.

``--collect`` is always passed, so a transient unit is removed the moment it exits and units
never accumulate (``systemctl --user list-units 'oprun-*'`` returns to clean once lanes are
settled).

This module has exactly one detach story: the systemd unit. It deliberately contains no shell
backgrounding and no terminal-multiplexer fallback, because those leave the worker inside the
launching session's scope — the failure mode v0.2 exists to kill.

What this module answers, and what it does not:

* **Process lifetime** — :func:`launch` starts a unit, :func:`unit_status` reads what systemd
  says about it, :func:`stop` tears it down.
* It never answers *whether a lane is done*. A dead unit with valid evidence is a finished
  lane; a live unit with no evidence is not. Only the sidecar plus a controller re-run settles
  a lane (see ``scripts/advance.py``).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: The real user home. The launcher itself may run inside a sandbox with a redirected HOME;
#: workers must not inherit that, so HOME is pinned explicitly on every launch.
REAL_HOME = "/home/logani"

#: One unit per lane, so ``systemctl --user show oprun-alpha`` answers for that lane alone.
UNIT_PREFIX = "oprun-"

#: Every systemctl call is bounded: a wedged user bus must not hang a mission.
_SHOW_TIMEOUT = 10.0
_STOP_TIMEOUT = 15.0
_LAUNCH_TIMEOUT = 30.0
#: systemd-run's own chatter is kept as the launch detail; truncated only for sanity.
_DETAIL_LIMIT = 500


def _as_int(value: str) -> int | None:
    """``"7"`` -> ``7``; empty or non-numeric -> ``None`` (never a guessed number)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _property(unit: str, name: str) -> str:
    """Read ONE systemd property of ``unit``, freshly, or ``""`` if systemd cannot say.

    One property per call on purpose: ``systemctl show --value`` prints values in *systemd's*
    own property order, not in the order of the ``-p`` flags (measured on this box: requesting
    ``-p LoadState -p Result`` prints ``Result`` first), so positional parsing of a multi-``-p``
    call silently mis-attributes values. Per-property calls are order-safe by construction.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", name, "--value"],
            capture_output=True,
            text=True,
            timeout=_SHOW_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return ""
    return (proc.stdout or "").strip()


def unit_name(lane_id: str) -> str:
    """The unit name a lane is launched as. One convention, used by dispatch and by advance."""
    return f"{UNIT_PREFIX}{lane_id}"


def unit_known(unit: str) -> bool:
    """Whether systemd holds a record for ``unit`` at all.

    Needed because ``systemd show`` happily answers for a unit that does not exist, with
    *default* property values that look like a successful run. An unknown unit is never
    evidence about a lane.

    Both signals are read from systemd: ``FragmentPath`` (a transient unit carries a
    ``/run/user/<uid>/systemd/transient/...`` fragment, a destroyed one has none) and
    ``LoadState``. Measured on this box: a live ``systemd-run --user --collect`` unit is
    ``LoadState=loaded`` with a fragment path; a name that was never created is
    ``LoadState=not-found`` with an empty path.
    """
    return _property(unit, "LoadState") == "loaded" or bool(_property(unit, "FragmentPath"))


def build_systemd_run_argv(unit: str, argv: list[str], workdir: Path,
                           env: dict[str, str] | None = None) -> list[str]:
    """The exact argv that starts ``argv`` as the detached transient unit ``unit``.

    Pure: nothing is spawned, so the detach story is unit-testable and reviewable without a
    live user session. ``HOME`` is always set from :data:`REAL_HOME` and cannot be overridden
    through ``env`` — a worker inheriting a sandboxed HOME is a routing bug, not a preference.
    """
    if not str(unit).strip():
        raise ValueError("unit name must be a non-empty string")
    if not argv:
        raise ValueError("argv must contain at least the program to run")
    built = [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit={unit}",
        f"--working-directory={workdir}",
        f"--setenv=HOME={REAL_HOME}",
    ]
    for key, value in (env or {}).items():
        if str(key) == "HOME":
            continue
        built.append(f"--setenv={key}={value}")
    built.append("--")
    built.extend(str(part) for part in argv)
    return built


def launch(unit: str, argv: list[str], *, workdir: Path, env: dict | None = None) -> dict:
    """Start ``argv`` as a DETACHED transient user unit named ``unit``.

    Returns ``{"unit": str, "started": bool, "detail": str}``. ``started`` is systemd-run's
    own exit status, not an inference: a launcher that reports success it did not observe is
    worse than one that reports nothing.
    """
    workdir = Path(workdir)
    if not workdir.is_dir():
        return {"unit": unit, "started": False, "detail": f"working directory not found: {workdir}"}
    built = build_systemd_run_argv(unit, argv, workdir, env)
    try:
        proc = subprocess.run(built, capture_output=True, text=True, timeout=_LAUNCH_TIMEOUT)
    except FileNotFoundError:
        return {"unit": unit, "started": False, "detail": "systemd-run is not available on this host"}
    except subprocess.TimeoutExpired:
        return {"unit": unit, "started": False,
                "detail": f"systemd-run did not return within {_LAUNCH_TIMEOUT:g}s"}
    except OSError as exc:
        return {"unit": unit, "started": False, "detail": f"systemd-run could not run: {exc}"}
    detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
    detail = detail[:_DETAIL_LIMIT] if detail else f"systemd-run exited {proc.returncode}"
    return {"unit": unit, "started": proc.returncode == 0, "detail": detail}


def unit_status(unit: str) -> dict:
    """What systemd says about ``unit`` — read, never inferred, never cached.

    Every field comes from ``systemctl --user show <unit> -p <prop> --value`` (``ActiveState``,
    ``ExecMainStatus``, ``Result``, ``ControlGroup``, plus ``FragmentPath``/``LoadState`` for the
    existence check in :func:`unit_known`). Keys are exactly ``active``, ``exec_main_status``,
    ``result``, ``control_group``. An unknown unit reports ``active=False`` with the three value
    fields ``None``: systemd's *default* values for a unit that never existed are
    ``Result=success``/``ExecMainStatus=0``, and echoing those would read as the successful run
    of a unit that was never created.
    """
    if not unit_known(unit):
        return {"active": False, "exec_main_status": None, "result": None, "control_group": None}
    return {
        "active": _property(unit, "ActiveState") == "active",
        "exec_main_status": _as_int(_property(unit, "ExecMainStatus")),
        "result": _property(unit, "Result") or None,
        "control_group": _property(unit, "ControlGroup") or None,
    }


def unit_finished(unit: str) -> bool | None:
    """Whether the unit's main process has **exited** — ``True``/``False``/``None`` (unknown).

    This answers *"should I keep waiting?"* and nothing else. ``None`` is returned whenever
    systemd has no record of the unit: a transient unit is created asynchronously, and
    ``--collect`` removes it the moment it exits, so "no record" cannot be told apart from
    "not created yet" — and treating that as death would park a healthy lane.
    """
    if not unit_known(unit):
        return None
    if _property(unit, "ActiveState") == "active":
        return False
    if _as_int(_property(unit, "ExecMainStatus")) is None:
        return None                      # never ran: no exit to report
    return True


def stop(unit: str) -> None:
    """Stop ``unit`` if it is running. Best effort: teardown must never fail a mission.

    A unit that is already gone, or a host with no user bus, is not an error here — settlement
    has already recorded the lane's evidence by the time teardown runs.
    """
    try:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, text=True,
                       timeout=_STOP_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return
