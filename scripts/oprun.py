#!/usr/bin/env python3
"""oprun v0.2 CLI — the single entrypoint for a mission.

Six verbs over one JSON ledger. No daemon, no scheduler, no second source of truth, and no
model anywhere in the completion path::

    oprun init <repo> [--mission TEXT] [--approve commit,push,merge]
    oprun dispatch <lane> --harness ID --test-cmd CMD (--brief PATH | --prompt TEXT)
    oprun probe <lane> [--json] [--wait] [--timeout SEC]
    oprun settle <lane> --accept | --needs-review REASON
    oprun status [--json]
    oprun approve --grant commit,push | --revoke push

This module owns exactly four things and delegates the rest:

* **argument parsing + exit codes** — one entrypoint, no traceback dumps;
* **the approval envelope** — decided once at ``init``, recorded in the ledger, printed by
  ``status``, and *never widened implicitly*; risky git actions (force-push, tag, release,
  history rewrite, branch delete, another repo) each need their own grant;
* **the git side effects a granted envelope authorises** — commit / push / merge performed
  by ``settle --accept`` with no prompt;
* **the identity rule** — the model and harness a lane must be running, compared against what
  its worker reported (``settle`` calls ``harnesses.identity_reason``, and
  ``harnesses.identity_warning`` for the advisory half). A **model** substitution parks the lane and
  is never recorded as ``completed``; the one exception is a harness that declares
  ``pin_verifiable=False``, where the pin reaches the CLI but nothing the CLI emits can corroborate
  it, so a reported mismatch is recorded as an advisory ``identity_warning`` instead of failing
  correct work. **Harness identity comes from the registry** — oprun picked the binary — so a
  worker's harness self-report is recorded verbatim and warned about, never enforced.
  ``scripts/advance.py`` calls the same two functions, so the runner and the CLI can never disagree
  about it.

It does not own lane status (``scripts/ledger.py``), process lifetime (``scripts/launch.py``)
or completion (``scripts/probe.py``). ``init`` and ``dispatch`` never settle a lane, ``probe``
never asks systemd whether a lane is *done*, and nothing here invokes a model.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# ``scripts/`` is not a package: make the sibling modules importable whether this file is run
# as a script (``python3 scripts/oprun.py``) or imported by a test.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import harnesses  # noqa: E402  (sibling: routing registry)
import launch  # noqa: E402  (sibling: systemd-run --user detach)
import probe as probe_mod  # noqa: E402  (sibling: the deterministic witness)
from ledger import (  # noqa: E402  (sibling: the only lane-status authority)
    BLOCKED,
    COMPLETED,
    DISPATCHED,
    FAILED,
    PARKED,
    PENDING,
    DepthExceeded,
    IllegalTransition,
    Ledger,
    ParallelismExceeded,
    display_state,
    git_evidence,
)

# --- frozen vocabulary -------------------------------------------------------
#: The six verbs. ``--help`` lists exactly these; a gate checks it.
VERBS = ("init", "dispatch", "probe", "settle", "status", "approve")

#: The complete approval vocabulary, in the ledger's own order. Every envelope carries every
#: key; an ungranted key is exactly ``{"granted": false}``, nothing more.
APPROVAL_KEYS = ("commit", "push", "merge", "deploy", "publish", "destructive")

#: The three actions ``settle --accept`` performs when the envelope allows them.
GIT_ACTIONS = ("commit", "push", "merge")

#: Ledger location relative to the mission repo, and the file ``init`` creates.
LEDGER_TAIL = Path(".tmp") / "oprun" / "state.json"
MISSION_STATE = "state.json"

#: ``<repo>/.oprun`` is also the sidecar directory every lane writes into.
SIDECAR_DIRNAME = ".oprun"
SIDECAR_TEMPLATE = "result.{dispatch_id}.json"

#: The lane field recording the PATH the lane's worker unit was launched with. Written here at
#: dispatch from ``launch()``'s own report (``result["path"]``), and read by every acceptance
#: re-run site — ``probe._run_test_cmd``, ``advance._run_test`` and this module's own
#: ``_run_test_cmd`` (``settle --accept``) — so a lane's ``test_cmd`` runs under the environment its
#: worker actually had. Spelled identically in those modules; the suite pins them to one string so
#: the contract cannot drift.
UNIT_PATH_FIELD = "unit_path"

#: Paths that are **lane evidence, never product**: the sidecar directory, byte-code caches, and
#: compiled artefacts. This is the same rule set the repo's own ``.gitignore`` commits, but it is
#: enforced here as well, because the lane worktree belongs to the *mission* repo — which may have
#: no ignore rule at all — and ``git add -A`` there shipped ``.oprun/result.<dispatch>.json`` inside
#: the product commit. A name matching any path component is excluded, and ``*.pyc`` matches by
#: suffix, so a nested ``pkg/__pycache__/mod.cpython-312.pyc`` is excluded too.
EVIDENCE_DIRNAMES = (SIDECAR_DIRNAME, "__pycache__", ".pytest_cache")
EVIDENCE_SUFFIXES = (".pyc",)

#: Accepted spellings that unambiguously mean one registry id. The *ledger* always records the
#: canonical id, so two spellings can never split a harness's identity. The table itself lives with
#: the ids it maps to (:data:`harnesses.HARNESS_ALIASES`) and is re-exported here, because the CLI
#: is where an operator reads the accepted spellings — there is exactly one table for identity.
HARNESS_ALIASES = harnesses.HARNESS_ALIASES

#: Default profile for a harness whose recipe needs ``<profile>`` (hermes).
DEFAULT_PROFILE = "coder"
#: Default autonomy level for a harness whose recipe needs ``<low|medium|high>`` (droid).
DEFAULT_AUTONOMY = "medium"

SCHEMA_VERSION = 1

#: Exit codes. Non-zero is never a surprise: every path below is classified.
EXIT_OK = 0
EXIT_ERROR = 1          # unknown lane, missing ledger, bad harness, failed pre-flight
EXIT_USAGE = 2          # argparse
EXIT_ILLEGAL = 3        # illegal transition (dependency gate, wrong lane state, cap)
EXIT_REFUSED = 4        # refused approval: unknown key, or an action outside the envelope
EXIT_SIDE_EFFECT = 5    # a granted git action failed
EXIT_INTERNAL = 70      # a bug; never a traceback on stderr unless OPRUN_DEBUG is set

GIT_TIMEOUT = 120.0
TEST_TIMEOUT = 900.0
#: Characters of a test/git output tail kept in evidence.
TAIL_CHARS = 400

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"


class CLIError(Exception):
    """A refusal or usage error the operator must read as one line, not a traceback."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# --- small utilities ---------------------------------------------------------
def _utc_now() -> str:
    """ISO-8601 UTC, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_doc(path: Path) -> dict:
    """The ledger document on disk, or ``{}`` when the file does not exist yet."""
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(f"ledger is unreadable: {path}: {exc}") from None
    if not isinstance(doc, dict):
        raise CLIError(f"ledger is not a JSON object: {path}")
    return doc


def _write_document(path: Path, doc: dict) -> None:
    """Atomic whole-document write, byte-compatible with ``ledger.Ledger``'s own format."""
    tmp = Path(f"{path}.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _update_document(path: Path, mutate: Callable[[dict], None]) -> dict:
    """Read-modify-write the ledger under **its own** lock, then return the new document.

    The lock path and the re-read discipline belong to ``ledger.py``; inventing a second lock
    here would be a second writer. ``Ledger._locked`` re-reads on entry, so ``mutate`` always
    sees the freshest document — a peer's committed approvals cannot be clobbered.
    """
    led = Ledger(path)
    with led._locked():  # noqa: SLF001 - the ledger module owns the lock, by design
        mutate(led.data)
        led._write()  # noqa: SLF001 - and its file format
    return led.data


def _record_unit_path(ledger_path: Path, lane_id: str, unit_path: str) -> None:
    """Persist the PATH a lane's worker unit was pinned with, on that lane's own record.

    ``launch()`` already **returns** the exact PATH it put on the unit; this writes that value —
    never a recomputed one — onto the lane at dispatch. It is what lets the controller's own
    acceptance re-run (``probe``/``advance``) run the lane's ``test_cmd`` under the PATH its worker
    actually had, instead of the launcher's own PATH: without it the two are different variables by
    construction, and "the test the worker passed" need not be the test the controller can run.

    An empty value is never recorded: absent means "no recorded PATH", which the re-run sites read
    as the ambient environment — exactly the behaviour older ledgers keep.
    """
    if not unit_path:
        return

    def mutate(doc: dict) -> None:
        lane = (doc.get("lanes") or {}).get(lane_id)
        if isinstance(lane, dict):
            lane[UNIT_PATH_FIELD] = unit_path

    _update_document(ledger_path, mutate)


def _load_ledger(path: Path) -> Ledger:
    """A ``Ledger`` carrying the mission's persisted ``max_parallel`` / ``failure_limit``.

    Those two live in the JSON document (``ledger.py`` takes them as constructor arguments and
    does not persist them itself), so the CLI is what makes ``init --max-parallel`` stick.
    """
    doc = _read_doc(path)
    return Ledger(
        path,
        failure_limit=int(doc.get("failure_limit", 3)),
        max_parallel=int(doc.get("max_parallel", 4)),
    )


def _require_ledger(path: Path) -> Path:
    """The ledger must already exist for every verb except ``init``."""
    if not path.is_file():
        raise CLIError(f"no ledger at {path} — run: oprun init <repo> --mission \"…\"")
    return path


def _find_ledger_upwards(start: Path) -> Path | None:
    """Nearest ``<dir>/.tmp/oprun/state.json`` at or above ``start``, so the CLI works from a
    subdirectory of the mission repo without ``--ledger``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        ledger = candidate / LEDGER_TAIL
        if ledger.is_file():
            return ledger
    return None


def _ledger_path(args: argparse.Namespace, *, repo: Path | None = None) -> Path:
    """Resolve the ledger: ``--ledger`` wins, then ``<repo>``, then the nearest one upwards."""
    explicit = getattr(args, "ledger", None)
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    if repo is not None:
        return (repo / LEDGER_TAIL).resolve()
    return _find_ledger_upwards(Path.cwd()) or (Path.cwd() / LEDGER_TAIL).resolve()


def _repo_for(ledger_path: Path) -> Path:
    """The mission repo a ledger belongs to (``<repo>/.tmp/oprun/state.json`` -> ``<repo>``)."""
    if ledger_path.parent.name == "oprun" and ledger_path.parent.parent.name == ".tmp":
        return ledger_path.parent.parent.parent
    return ledger_path.parent


def _split_keys(values: Iterable[str] | None) -> list[str]:
    """Flatten ``--grant a,b`` / repeated ``--grant a --grant b`` into an ordered dedupe."""
    out: list[str] = []
    for chunk in values or ():
        for part in str(chunk).split(","):
            key = part.strip()
            if key and key not in out:
                out.append(key)
    return out


def _validate_keys(keys: Iterable[str]) -> None:
    """Unknown permission names are refused, never ignored."""
    bad = [key for key in keys if key not in APPROVAL_KEYS]
    if bad:
        raise CLIError(
            f"unknown approval {bad!r}: expected any of {', '.join(APPROVAL_KEYS)}",
            EXIT_REFUSED,
        )


# --- the approval envelope ---------------------------------------------------
def blank_envelope() -> dict:
    """Every key present, nothing granted. This is what ``init`` with no ``--approve`` leaves."""
    return {key: {"granted": False} for key in APPROVAL_KEYS}


def normalize_envelope(raw: Any) -> dict:
    """Coerce whatever is in the file into the frozen shape, preserving recorded grants."""
    if not isinstance(raw, dict):
        return blank_envelope()
    envelope: dict = {}
    for key in APPROVAL_KEYS:
        entry = raw.get(key)
        if isinstance(entry, dict) and entry.get("granted") is True:
            envelope[key] = {
                "granted": True,
                "at": str(entry.get("at") or ""),
                "by": str(entry.get("by") or "user"),
                "scope": str(entry.get("scope") or "mission"),
            }
            if entry.get("via"):
                envelope[key]["via"] = str(entry["via"])
        else:
            envelope[key] = {"granted": False}
    return envelope


def grant_keys(envelope: dict, keys: Iterable[str], *, by: str = "user",
               scope: str = "mission", via: str | None = None) -> dict:
    """Record a grant with its ``at``/``by``/``scope`` stamp. Mutates and returns ``envelope``."""
    at = _utc_now()
    for key in keys:
        entry = {"granted": True, "at": at, "by": by, "scope": scope}
        if via:
            entry["via"] = via
        envelope[key] = entry
    return envelope


def revoke_keys(envelope: dict, keys: Iterable[str]) -> dict:
    """Withdraw a grant. A revoked key returns to exactly ``{"granted": false}``."""
    for key in keys:
        envelope[key] = {"granted": False}
    return envelope


def is_granted(envelope: dict, key: str) -> bool:
    """Whether action ``key`` is inside the envelope right now."""
    entry = envelope.get(key)
    return bool(isinstance(entry, dict) and entry.get("granted") is True)


def envelope_lines(envelope: dict) -> list[str]:
    """The human-readable envelope, exactly the six keys, granted or not."""
    lines: list[str] = []
    for key in APPROVAL_KEYS:
        entry = envelope.get(key) or {}
        if entry.get("granted"):
            detail = (f"at={entry.get('at', '')} by={entry.get('by', 'user')} "
                      f"scope={entry.get('scope', 'mission')}")
            if entry.get("via"):
                detail += f" via={entry['via']}"
            lines.append(f"  {key:<12} granted      {detail}")
        else:
            lines.append(f"  {key:<12} NOT granted")
    return lines


def envelope_text(envelope: dict) -> str:
    """The envelope as a printable block, headed so it is unmistakable in ``status``."""
    return "\n".join(["approvals:", *envelope_lines(envelope)])


# --- git helpers -------------------------------------------------------------
def _git(worktree: Path, *args: str) -> tuple[int, str, str]:
    """Run one git command in ``worktree``. Returns ``(rc, stdout, stderr)`` — never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 128, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_test_cmd(test_cmd: list[str], worktree: Path,
                  unit_path: str | None = None) -> tuple[int, str]:
    """Re-run the lane's OWN ``test_cmd`` in its worktree: ``(rc, output tail)``.

    The command comes from the ledger, never from the worker, and this is the same acceptance
    re-run ``probe`` performs — so ``settle --accept`` cannot be talked past a red test. The
    **environment** is the lane's own recorded launch PATH when it has one (the rule lives once, in
    ``probe._test_cmd_env``), so the CLI's re-run reproduces the PATH the worker ran under instead
    of the conductor's; with no recording the ambient environment is inherited, as before.
    """
    cmd = [str(part) for part in test_cmd]
    if not cmd:
        return 1, "lane has no test_cmd: nothing can be re-run to accept it"
    if not worktree.is_dir():
        return 127, f"worktree is not a directory: {worktree}"
    try:
        proc = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True,
                              timeout=TEST_TIMEOUT,
                              env=probe_mod._test_cmd_env(unit_path))
    except subprocess.TimeoutExpired:
        return 124, f"test_cmd timed out after {TEST_TIMEOUT:.0f}s"
    except (OSError, ValueError) as exc:
        return 127, f"test_cmd could not be run: {exc}"
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, tail[-TAIL_CHARS:]


def _git_head(worktree: Path) -> str | None:
    """The current commit of ``worktree``, or ``None`` when it is not a repo."""
    rc, out, _ = _git(worktree, "rev-parse", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


def _current_branch(path: Path) -> str | None:
    """The checked-out branch name, or ``None`` when detached/unknown (never a guess)."""
    rc, out, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    name = out.strip()
    return name if rc == 0 and name and name != "HEAD" else None


def _origin_url(worktree: Path) -> str | None:
    """The lane's configured ``origin`` URL, if any."""
    rc, out, _ = _git(worktree, "config", "--get", "remote.origin.url")
    return out.strip() if rc == 0 and out.strip() else None


def _same_repo(candidate: str, origin: str | None) -> bool:
    """Whether ``candidate`` names the same repository as ``origin`` (so it is not 'another repo')."""
    if origin is None:
        return False
    if candidate == origin:
        return True
    try:
        return Path(candidate).expanduser().resolve() == Path(origin).expanduser().resolve()
    except OSError:
        return False


def _git_common_root(worktree: Path) -> Path | None:
    """The primary worktree of the repository ``worktree`` belongs to."""
    rc, out, _ = _git(worktree, "rev-parse", "--git-common-dir")
    raw = out.strip()
    if rc != 0 or not raw:
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = (worktree / common)
    return common.resolve().parent


# --- git side effects (performed ONLY inside the envelope) -------------------
def is_evidence_path(rel: str) -> bool:
    """Whether ``rel`` is lane evidence (a sidecar, a cache, a ``.pyc``) rather than product.

    Matched on path *components*, not on a prefix: ``pkg/.oprun/result.x.json`` and
    ``pkg/__pycache__/mod.cpython-312.pyc`` are excluded exactly like the top-level ones.
    """
    parts = [part for part in str(rel).replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part in EVIDENCE_DIRNAMES for part in parts):
        return True
    return bool(parts) and parts[-1].endswith(EVIDENCE_SUFFIXES)


def _status_paths(worktree: Path) -> tuple[list[str] | None, str]:
    """Every changed path git reports — raw, unquoted, renames resolved to the new name.

    ``--porcelain -z`` is the only parseable form: NUL-separated entries, never quoted, so a path
    with a space or a newline cannot split one entry into two. A rename/copy entry is followed by
    the ORIGINAL path as its own record; that record is skipped, because the new name is the path
    that has to be staged. Paths are relative to the repo root, as git reports them.
    """
    rc, out, err = _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
    if rc != 0:
        return None, f"git status failed: {(err or out).strip()}"
    fields = out.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[:1] in ("R", "C"):
            index += 1                 # the next record is the rename's original path
        paths.append(entry[3:])
    return paths, ""


def _staged_paths(worktree: Path) -> tuple[list[str] | None, str]:
    """What the index holds right now, relative to the repo root (``diff --cached --name-only``)."""
    rc, out, err = _git(worktree, "diff", "--cached", "--name-only", "-z")
    if rc != 0:
        return None, f"git diff --cached failed: {(err or out).strip()}"
    return [path for path in out.split("\0") if path], ""


#: ``git add`` takes the paths in batches, so a lane that touched tens of thousands of files cannot
#: blow past the OS argument limit and turn a staging step into an "argument list too long" failure.
ADD_BATCH = 500


def _do_commit(worktree: Path, lane: str, dispatch_id: str) -> dict:
    """Commit the lane's work: **explicit paths, and never a byte of lane evidence.**

    ``git add -A`` staged ``<worktree>/.oprun/`` (this lane's own sidecars) and ``__pycache__/``
    into the product commit. On a mission repo with no ignore rule that shipped the evidence into
    the very history it is the review record for. So the paths come from git rather than from a
    guess: evidence paths are dropped from the list, anything a worker had already staged is
    unstaged again, and the commit is only made once the index is verified to hold no evidence. If
    the index cannot be cleaned the commit is refused outright — the sidecar dir shipping is worse
    than a failed settle.
    """
    candidates, problem = _status_paths(worktree)
    if candidates is None:
        return {"ok": False, "created": False, "sha": None, "detail": problem}

    staged_now, problem = _staged_paths(worktree)
    if staged_now is None:
        return {"ok": False, "created": False, "sha": None, "detail": problem}
    leaked = sorted(path for path in staged_now if is_evidence_path(path))
    if leaked:
        rc, out, err = _git(worktree, "reset", "-q", "--", *leaked)
        if rc != 0:
            return {"ok": False, "created": False, "sha": None, "excluded": leaked,
                    "detail": (f"refusing to commit: evidence paths were staged and could not be "
                               f"unstaged: {leaked} ({(err or out).strip()})")}

    to_stage = [path for path in candidates if not is_evidence_path(path)]
    excluded = sorted(path for path in candidates if is_evidence_path(path))
    for start in range(0, len(to_stage), ADD_BATCH):
        rc_add, _, err_add = _git(worktree, "add", "-A", "--",
                                  *to_stage[start:start + ADD_BATCH])
        if rc_add != 0:
            return {"ok": False, "created": False, "sha": None, "excluded": excluded,
                    "detail": f"git add failed: {err_add.strip()}"}

    staged, problem = _staged_paths(worktree)
    if staged is None:
        return {"ok": False, "created": False, "sha": None, "detail": problem}
    leaked = sorted(path for path in staged if is_evidence_path(path))
    if leaked:
        return {"ok": False, "created": False, "sha": None, "staged": sorted(staged),
                "excluded": excluded,
                "detail": f"refusing to commit: lane evidence is in the index: {leaked}"}
    if not staged:
        return {"ok": True, "created": False, "sha": None, "staged": [], "excluded": excluded,
                "detail": "nothing to commit"}
    message = f"oprun({lane}): {dispatch_id}"
    rc, out, err = _git(worktree, "commit", "-m", message)
    if rc != 0:
        return {"ok": False, "created": False, "sha": None, "staged": sorted(staged),
                "excluded": excluded,
                "detail": f"git commit failed: {(out + err).strip()[-TAIL_CHARS:]}"}
    return {"ok": True, "created": True, "sha": _git_head(worktree), "message": message,
            "staged": sorted(staged), "excluded": excluded, "detail": "committed"}


def _do_push(worktree: Path, remote: str, branch: str, *, force: bool = False,
             repo_url: str | None = None) -> dict:
    """Push ``branch`` to ``remote`` (or to an explicit ``repo_url``). Force only when asked."""
    target = repo_url or remote
    args = ["push"]
    if force:
        args.append("--force")
    args += [target, f"refs/heads/{branch}"]
    rc, out, err = _git(worktree, *args)
    return {
        "ok": rc == 0,
        "remote": remote,
        "target": target,
        "ref": f"refs/heads/{branch}",
        "sha": _git_head(worktree),
        "force": bool(force),
        "detail": (out + err).strip()[-TAIL_CHARS:],
    }


def _do_merge(worktree: Path, branch: str, into: str | None = None) -> dict:
    """Merge the lane's branch into the recorded target — and into nothing else.

    ``into`` is the branch the lane recorded (``dispatch --into`` / ``settle --into``). When the
    primary worktree does not have *that* branch checked out, the merge is refused by name and
    nothing is touched: merging into whatever happens to be checked out is how a lane's work lands
    on the wrong branch "by luck". With no target recorded, the previous behaviour stands (merge
    into the primary worktree's checked-out branch).

    ``--ff-only`` first (the usual case for a lane branched off the base); a real merge commit
    only when the histories genuinely diverged. Never forced, never destructive.
    """
    primary = _git_common_root(worktree)
    if primary is None:
        return {"ok": False, "skipped": "cannot resolve the repository root from the lane worktree"}
    target = _current_branch(primary)
    if target is None:
        return {"ok": False, "skipped": f"the primary worktree {primary} is in a detached HEAD"}
    if into and target != into:
        return {"ok": False, "refused": True, "into": into, "checked_out": target,
                "primary": str(primary),
                "detail": (f"refusing to merge {branch!r} into {into!r}: the primary worktree "
                           f"{primary} has {target!r} checked out")}
    if target == branch:
        return {"ok": True, "skipped": f"the lane branch {branch!r} is already checked out in {primary}",
                "into": target, "recorded_into": into}
    rc, out, err = _git(primary, "merge", "--ff-only", branch)
    detail = (out + err).strip()[-TAIL_CHARS:]
    if rc != 0:
        rc2, out2, err2 = _git(primary, "merge", "--no-edit", branch)
        detail = (out2 + err2).strip()[-TAIL_CHARS:]
        if rc2 != 0:
            return {"ok": False, "into": target, "recorded_into": into, "primary": str(primary),
                    "detail": detail}
    return {"ok": True, "into": target, "recorded_into": into, "primary": str(primary),
            "sha": _git_head(primary), "detail": detail}


def _do_tag(worktree: Path, remote: str, tag: str) -> dict:
    """Create and push a tag. Publishing: outside the default envelope, hence ``publish``."""
    rc, out, err = _git(worktree, "tag", tag)
    if rc != 0:
        return {"ok": False, "tag": tag, "detail": (out + err).strip()[-TAIL_CHARS:]}
    rc2, out2, err2 = _git(worktree, "push", remote, tag)
    return {"ok": rc2 == 0, "tag": tag, "sha": _git_head(worktree),
            "detail": (out2 + err2).strip()[-TAIL_CHARS:]}


def _do_release(worktree: Path, tag: str) -> dict:
    """Create a GitHub release for ``tag`` with ``gh``. Publishing, hence the ``publish`` grant."""
    if shutil.which("gh") is None:
        return {"ok": False, "tag": tag, "detail": "gh is not installed; cannot create a release"}
    try:
        proc = subprocess.run(
            ["gh", "release", "create", tag, "--title", tag, "--notes", "oprun release"],
            cwd=str(worktree), capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "tag": tag, "detail": f"gh release failed: {exc}"}
    return {"ok": proc.returncode == 0, "tag": tag,
            "detail": ((proc.stdout or "") + (proc.stderr or "")).strip()[-TAIL_CHARS:]}


def _do_delete_branch(worktree: Path, remote: str, branch: str) -> dict:
    """Delete a branch on the remote. Destructive: never inside the default envelope."""
    rc, out, err = _git(worktree, "push", remote, "--delete", branch)
    return {"ok": rc == 0, "branch": branch, "detail": (out + err).strip()[-TAIL_CHARS:]}


def _risky_request(args: argparse.Namespace, envelope: dict, worktree: Path) -> list[str]:
    """Every risky action this settle asks for that the envelope does not grant.

    There is **no implicit widening**: granting ``push`` authorises an ordinary push and nothing
    else. Each entry below needs its own fresh grant, and the refusal happens before any
    mutation, so a refused settle leaves the worktree and the remote untouched.
    """
    problems: list[str] = []

    def need(what: str, key: str) -> None:
        if not is_granted(envelope, key):
            problems.append(
                f"refusing {what}: the envelope does not grant {key!r} "
                f"(run: oprun approve --grant {key})"
            )

    if getattr(args, "force", False):
        need("a force-push (--force)", "destructive")
    if getattr(args, "rewrite_history", False):
        need("a rewritten-history push (--rewrite-history)", "destructive")
    if getattr(args, "delete_branch", None):
        need("a branch delete (--delete-branch)", "destructive")
    if getattr(args, "tag", None):
        need("a tag (--tag)", "publish")
    if getattr(args, "release", False):
        need("a release (--release)", "publish")
    if getattr(args, "release", False) and not getattr(args, "tag", None):
        problems.append("refusing a release: --release needs --tag NAME")
    repo = getattr(args, "repo", None)
    if repo and not _same_repo(repo, _origin_url(worktree)):
        need(f"a push to another repo ({repo})", "publish")
    return problems


# --- worker brief + argv -----------------------------------------------------
def _read_template(name: str) -> str | None:
    """Read a file from the repo's ``templates/`` directory, or ``None`` when absent."""
    path = TEMPLATES_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


DEFAULT_WORKER_PREAMBLE = """You are an oprun worker, not the conductor. Do not load any orchestration
skill or spawn nested orchestrators. oprun is the only orchestration authority.

Lane: {{lane}}    dispatch: {{dispatch_id}}
Worktree: {{worktree}}    (work only here; touch no file you do not own)
Write this result file atomically (temp file, then rename) when you stop: {{sidecar}}
"""


def sidecar_path(worktree: Path, dispatch_id: str) -> Path:
    """``<worktree>/.oprun/result.<dispatch_id>.json`` — one artifact per dispatch, no sharing."""
    return (Path(worktree) / SIDECAR_DIRNAME
            / SIDECAR_TEMPLATE.format(dispatch_id=dispatch_id))


def render_worker_brief(*, lane: str, dispatch_id: str, worktree: Path, test_cmd: list[str],
                        task: str) -> str:
    """The injectable worker preamble plus the task, with this dispatch's sidecar path filled in."""
    template = _read_template("worker-brief.md") or DEFAULT_WORKER_PREAMBLE
    sidecar = sidecar_path(worktree, dispatch_id)
    substitutions = {
        "{{lane}}": lane,
        "{{dispatch_id}}": dispatch_id,
        "{{worktree}}": str(worktree),
        "{{sidecar}}": str(sidecar),
        "{{test_cmd}}": " ".join(str(part) for part in test_cmd),
    }
    text = template
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return f"{text.rstrip()}\n\nTask:\n{task.strip()}\n"


def canonical_harness(harness_id: str) -> str:
    """The registry id for ``harness_id``: aliases resolve, unknown ids are a hard error.

    Delegates to the registry, so dispatch, ``settle`` and ``advance`` resolve a spelling the same
    way and two spellings of one CLI can never be routed (or compared) as two harnesses.
    """
    return harnesses.canonical_id(harness_id)


def build_worker_argv(harness_id: str, *, model: str | None, brief_path: Path,
                      brief_text: str) -> list[str]:
    """The exact argv one unattended worker is launched with.

    Starts from the registry recipe (``harnesses.argv_for``), which already enforces the model
    pin, then substitutes the recipe's literal placeholders and appends the brief for harnesses
    that take a positional prompt. ``hermes`` takes its brief by ``--query-file`` instead.
    """
    harness_id = canonical_harness(harness_id)
    argv = list(harnesses.argv_for(harness_id, model_pin=model))
    recipe_values = {
        "<profile>": DEFAULT_PROFILE,
        "<brief>": str(brief_path),
        "<low|medium|high>": DEFAULT_AUTONOMY,
    }
    argv = [recipe_values.get(part, part) for part in argv]
    if harness_id != "hermes":
        argv.append(brief_text)
    return argv


def _resolve_binary(binary: str) -> str | None:
    """The runnable path for a harness binary, or ``None`` when this host has it nowhere."""
    if Path(binary).is_absolute():
        return binary if Path(binary).is_file() else None
    return shutil.which(binary)


def _resolve_test_program(test_cmd: list[str]) -> str | None:
    """The runnable path of ``test_cmd[0]`` on THIS host, or ``None`` when it cannot be run.

    The acceptance command is re-run by the controller, not by the worker, so it has to be runnable
    *here*. A bare tool name (``npx``, ``node``, ``npm``) is resolved through this process's PATH; an
    explicit path is accepted only when it is a real executable file. A directory, a missing file, a
    typo, or a tool that only exists in the worker's environment all return ``None``.

    Checked at DISPATCH time on purpose (issue #1): a lane whose command cannot be started would
    otherwise burn a worker and be discovered only at settlement, where "could not run" (rc 127) was
    reported as if the lane's tests had failed — parking the lane and striking the circuit breaker
    for something the lane never did.
    """
    if not test_cmd:
        return None
    program = str(test_cmd[0])
    if not program:
        return None
    if os.sep in program or (os.altsep and os.altsep in program):
        target = Path(program).expanduser()
        return str(target) if target.is_file() and os.access(target, os.X_OK) else None
    return shutil.which(program)


# --- identity: the designation vs what the worker reported -------------------
def _current_sidecar(worktree: Path, dispatch_id: str) -> tuple[dict | None, str]:
    """The lane's sidecar for its CURRENT dispatch: ``(document, problem)``.

    ``(None, "")`` — there is no sidecar at all, so no identity was reported and there is nothing
    to contradict. The check never invents a claim on the worker's behalf: whether an
    artifact-less lane may be accepted at all is decided by the controller's own re-run of
    ``test_cmd``, elsewhere.

    ``(None, why)`` — a file exists at the exact path but is not a readable JSON object. That is
    an *unusable* artifact, not an absent one, and an unusable artifact is never accepted.

    ``(document, "")`` — the reported identity, verbatim.
    """
    path = sidecar_path(worktree, dispatch_id)
    if not path.is_file():
        return None, ""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"sidecar {path} is unreadable: {exc}"
    if not isinstance(document, dict):
        return None, f"sidecar {path} is not a JSON object"
    return document, ""


def _identity_fields(lane: dict, worktree: Path, dispatch_id: str,
                     document: dict | None) -> dict:
    """The identity pair for the settled evidence, verbatim and audit-ready.

    ``harness`` is the lane's REGISTRY id and the sidecar's spelling lands only in
    ``harness_reported``: a lane's harness comes from the registry, never from the worker. Both
    raw model strings are kept whichever way the check went — that pair is what makes "the pin was
    actually applied" checkable after the fact.
    """
    reported = document or {}
    return {
        "sidecar": str(sidecar_path(worktree, dispatch_id)),
        "harness": lane.get("harness"),
        "harness_reported": reported.get("harness"),
        "model_requested": lane.get("model_requested"),
        "model_reported": reported.get("model"),
    }


# --- verbs: init -------------------------------------------------------------
def _mission_md(*, mission: str, repo: Path, ledger: Path, created_at: str) -> str:
    """The mission file ``init`` writes next to the ledger (from ``templates/mission.md``)."""
    template = _read_template("mission.md")
    if template is None:
        return (f"# Mission\n\n{mission}\n\n"
                f"- repo: {repo}\n- ledger: {ledger}\n- created: {created_at}\n")
    substitutions = {
        "{{mission}}": mission,
        "{{repo}}": str(repo),
        "{{ledger}}": str(ledger),
        "{{created_at}}": created_at,
    }
    text = template
    for key, value in substitutions.items():
        text = text.replace(key, value)
    return text


def cmd_init(args: argparse.Namespace) -> int:
    """Create the ledger and the mission directory. Launches nothing, settles nothing."""
    repo = Path(args.repo).expanduser().resolve()
    ledger_path = _ledger_path(args, repo=repo)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    grants = _split_keys(args.approve)
    _validate_keys(grants)

    existing = _read_doc(ledger_path)
    created_at = str(existing.get("created_at") or _utc_now())
    envelope = normalize_envelope(existing.get("approvals"))
    if grants:
        grant_keys(envelope, grants, by=args.by, scope=args.scope)
    mission = args.mission if args.mission is not None else str(existing.get("mission") or "")

    document = {
        "schema_version": SCHEMA_VERSION,
        "mission": mission,
        "repo": str(repo),
        "created_at": created_at,
        "max_parallel": int(args.max_parallel if args.max_parallel is not None
                            else existing.get("max_parallel", 4)),
        "failure_limit": int(args.failure_limit if args.failure_limit is not None
                             else existing.get("failure_limit", 3)),
        "approvals": envelope,
        "lanes": existing.get("lanes") if isinstance(existing.get("lanes"), dict) else {},
    }
    _write_document(ledger_path, document)

    mission_file = ledger_path.parent / "mission.md"
    mission_file.write_text(
        _mission_md(mission=mission, repo=repo, ledger=ledger_path, created_at=created_at),
        encoding="utf-8",
    )

    print(f"oprun init: {'updated' if existing else 'created'} {ledger_path}")
    print(f"  mission:      {mission or '(none)'}")
    print(f"  repo:         {repo}")
    print(f"  mission.md:   {mission_file}")
    print(f"  max_parallel: {document['max_parallel']}   failure_limit: {document['failure_limit']}")
    print(f"  lanes:        {len(document['lanes'])}")
    print(envelope_text(envelope))
    print("next: oprun dispatch <lane> --harness <id> --test-cmd \"…\" --prompt \"…\"")
    return EXIT_OK


# --- verbs: dispatch ---------------------------------------------------------
def cmd_dispatch(args: argparse.Namespace) -> int:
    """Register ONE lane and launch it detached (``systemd-run --user``), then print its id.

    Registration happens first because the brief the worker receives must name **this**
    dispatch's sidecar (``result.<dispatch_id>.json``). The ledger's dependency gate, depth
    ceiling and parallelism cap all fire inside ``Ledger.dispatch`` — a refused dispatch never
    reaches a launch.
    """
    ledger_path = _require_ledger(_ledger_path(args))
    mission_dir = ledger_path.parent
    lane = args.lane
    harness_id = canonical_harness(args.harness)
    if not harnesses.routable(harness_id, args.model):
        raise CLIError(
            f"harness {harness_id!r} needs an explicit --model pin (its default is not usable "
            f"here); an unpinned lane would burn a dispatch to learn nothing",
            EXIT_REFUSED,
        )

    test_cmd = shlex.split(args.test_cmd or "")
    if not test_cmd:
        raise CLIError("--test-cmd must name the command that decides this lane's acceptance")
    # Preflight, BEFORE anything is registered or launched: the controller has to be able to START
    # this command, and it runs it here, not in the worker's environment. An unresolvable program is
    # refused loudly now instead of surfacing at settle as rc 127 — which used to be reported as a
    # failed test, parking the lane and striking the breaker (issue #1).
    if _resolve_test_program(test_cmd) is None:
        raise CLIError(
            f"--test-cmd program {test_cmd[0]!r} cannot be run on this host: it is not on PATH and "
            f"is not an executable file. The controller's acceptance re-run would fail to start it "
            f"(rc 127: an environment fault, not a red test), so this dispatch is refused before "
            f"it registers a lane or burns a worker. Pass an absolute path, or install the tool.",
            EXIT_REFUSED,
        )

    if args.brief and args.prompt:
        raise CLIError("--brief and --prompt are alternatives; pass one")
    if args.brief:
        brief_source = Path(args.brief).expanduser()
        if not brief_source.is_file():
            raise CLIError(f"--brief not found: {brief_source}")
        task = brief_source.read_text(encoding="utf-8")
    elif args.prompt:
        task = args.prompt
    else:
        raise CLIError("one of --brief PATH or --prompt TEXT is required")

    worktree = (Path(args.worktree).expanduser().resolve() if args.worktree
                else _repo_for(ledger_path))
    if not worktree.is_dir():
        raise CLIError(f"--worktree is not a directory: {worktree}")

    led = _load_ledger(ledger_path)
    led.init_lane(
        lane,
        harness=harness_id,
        worktree=str(worktree),
        test_cmd=test_cmd,
        model=args.model,
        depends_on=list(args.depends_on or []),
        depth=args.depth,
        # Where this lane's work is meant to land, recorded **now** (at dispatch) so settle can
        # refuse a primary worktree that is on some other branch instead of merging by luck.
        merge_into=_branch_name(args.into, flag="--into") if args.into else None,
    )
    dispatch_id = led.dispatch(lane)   # dependency gate / depth / cap live here

    brief_text = render_worker_brief(lane=lane, dispatch_id=dispatch_id, worktree=worktree,
                                    test_cmd=test_cmd, task=task)
    brief_dir = mission_dir / "briefs"
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / f"{lane}.{dispatch_id}.md"
    brief_path.write_text(brief_text, encoding="utf-8")

    binary = _resolve_binary(harnesses.get(harness_id).binary)
    if binary is None:
        detail = f"harness binary {harnesses.get(harness_id).binary!r} is not installed on this host"
        led.settle(lane, dispatch_id, False, reason=detail,
                   evidence={"controller": "oprun-dispatch", "dispatch_id": dispatch_id,
                             "launch": {"started": False, "detail": detail}, "checked_at": _utc_now()})
        raise CLIError(f"lane {lane!r} not launched: {detail}")

    argv = build_worker_argv(harness_id, model=args.model, brief_path=brief_path,
                             brief_text=brief_text)
    argv[0] = binary
    unit = launch.unit_name(lane)
    result = launch.launch(unit, argv, workdir=worktree, env=None)
    if not result.get("started"):
        led.settle(lane, dispatch_id, False, reason=f"launch failed: {result.get('detail')}",
                   evidence={"controller": "oprun-dispatch", "dispatch_id": dispatch_id,
                             "launch": result, "checked_at": _utc_now()})
        raise CLIError(f"lane {lane!r} not launched: {result.get('detail')}")

    # Record the PATH this unit was pinned with on the lane's own record — the value ``launch()``
    # itself reports, never a recomputed one — so the controller's acceptance re-run re-runs the
    # lane's ``test_cmd`` under the same PATH its worker had. Without it the worker's PATH and the
    # controller's PATH are different variables by construction and the acceptance test can fail
    # ENOENT for a tool the worker used happily (issue #1's residual asymmetry).
    _record_unit_path(ledger_path, lane, str(result.get("path") or ""))

    print(dispatch_id)
    print(f"oprun dispatch: lane {lane!r} -> unit {unit} (detached, "
          f"cgroup user@1000.service/app.slice)", file=sys.stderr)
    print(f"  brief:    {brief_path}", file=sys.stderr)
    print(f"  worktree: {worktree}", file=sys.stderr)
    print(f"  harness:  {harness_id}"
          + (f"  model: {args.model}" if args.model else ""), file=sys.stderr)
    return EXIT_OK


# --- verbs: probe ------------------------------------------------------------
def _dispatch_age_seconds(lane: dict) -> float | None:
    """Seconds since the lane's current dispatch, read from its own history."""
    for entry in reversed(lane.get("history") or []):
        if entry.get("to") == DISPATCHED and isinstance(entry.get("at"), (int, float)):
            return max(0.0, time.time() - float(entry["at"]))
    return None


def _unit_active(lane_id: str, lane: dict) -> bool:
    """systemd's answer to *is the process still alive* — lifetime only, never lane state."""
    unit = str(lane.get("unit") or launch.unit_name(lane_id))
    try:
        return bool(launch.unit_status(unit)["active"])
    except Exception:                      # no user bus / no systemd: not evidence of liveness
        return False


def cmd_probe(args: argparse.Namespace) -> int:
    """Print exactly one word from ``probe.VERDICTS`` (or the full dict with ``--json``)."""
    ledger_path = _require_ledger(_ledger_path(args))
    ledger = _load_ledger(ledger_path)
    lane = dict(ledger.lane(args.lane))    # KeyError -> clean 'unknown lane'
    lane.setdefault("lane_id", args.lane)

    deadline = time.monotonic() + max(0.0, float(args.timeout))
    waited_out = False
    while True:
        age = _dispatch_age_seconds(lane)
        result = probe_mod.probe(
            lane,
            unit_active=_unit_active(args.lane, lane),
            timeout_exceeded=waited_out or (age is not None and age > float(args.timeout)),
            sidecar_dir=None,
        )
        if not args.wait or result["verdict"] != "pending":
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            waited_out = True
            continue
        time.sleep(min(probe_mod.WAIT_POLL_SECONDS, remaining))
        lane = dict(ledger.lane(args.lane))
        lane.setdefault("lane_id", args.lane)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["verdict"])
        print(f"{args.lane}: {result['reason']}", file=sys.stderr)
    return EXIT_OK


# --- verbs: settle -----------------------------------------------------------
def _ask_once(lane: str, missing: list[str]) -> str:
    """Ask **exactly once**, on one line, and take the answer. Closed stdin means no."""
    joined = ",".join(missing)
    sys.stderr.write(
        f"oprun settle: lane {lane!r} needs {joined} but the approval envelope does not grant "
        f"it. Approve now? [y/N] "
    )
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        return "no"
    finally:
        sys.stderr.write("\n")      # close the one-line ask so following output is not glued on
        sys.stderr.flush()
    return "yes" if line.strip().lower() in ("y", "yes") else "no"


def _planned_actions(worktree: Path, branch: str | None) -> list[str]:
    """Which of commit/push/merge this settle would actually use, so the ask names real actions."""
    planned = ["commit"]
    if _origin_url(worktree):
        planned.append("push")
    primary = _git_common_root(worktree)
    if primary is not None:
        target = _current_branch(primary)
        if target and branch and target != branch:
            planned.append("merge")
    return planned


# --- the recorded merge target ----------------------------------------------
#: Characters git itself refuses in a branch name. A target that cannot be a branch name is a typo,
#: and a typo must never become a merge target quietly.
_ILLEGAL_BRANCH_CHARS = (" ", "~", "^", ":", "?", "*", "[", "\\", '"')


def _branch_name(value: str | None, *, flag: str) -> str:
    """Validate a ``--into`` value as a branch name, or refuse it as a typo (never a guess)."""
    name = (value or "").strip()
    if not name:
        raise CLIError(f"{flag} needs a branch name", EXIT_USAGE)
    if (name in ("HEAD", "@", "-") or name.startswith("-") or ".." in name or "@{" in name
            or name.endswith("/") or name.endswith(".lock")
            or any(ch in name for ch in _ILLEGAL_BRANCH_CHARS)):
        raise CLIError(f"{flag} is not a branch name: {value!r}", EXIT_USAGE)
    return name


def _merge_target(lane: dict, lane_id: str, requested: str | None) -> str | None:
    """The branch this settle must merge into: recorded at dispatch, or stated here.

    ``--into`` at settle may repeat what ``dispatch --into`` recorded, or set the target for a lane
    dispatched before ``--into`` existed. A *different* branch is refused before any mutation:
    retargeting work that was already reviewed against another branch is a new dispatch, not a
    quiet retarget. With no target recorded anywhere, ``None`` means the legacy behaviour (merge
    into whatever the primary worktree has checked out).
    """
    recorded = lane.get("merge_into") or None
    want = _branch_name(requested, flag="--into") if requested else None
    if recorded and want and recorded != want:
        raise CLIError(
            f"refusing --into {want!r}: lane {lane_id!r} was dispatched with --into {recorded!r}; "
            f"re-dispatch the lane to retarget it",
            EXIT_USAGE,
        )
    return recorded or want


def _record_merge_target(ledger_path: Path, lane_id: str, target: str) -> None:
    """Persist a target first stated at settle, so the next reader sees the same intent."""
    def mutate(doc: dict) -> None:
        lanes = doc.get("lanes") if isinstance(doc.get("lanes"), dict) else {}
        if lane_id in lanes:
            lanes[lane_id]["merge_into"] = target
    _update_document(ledger_path, mutate)


def _merge_target_problem(worktree: Path, target: str) -> str | None:
    """Why ``target`` cannot be merged into right now, naming both branches. ``None`` when it can.

    The check runs *before* any mutation: a settle that would commit and push and then merge into
    the wrong branch is the failure this names, and it must not be discovered afterwards.
    """
    primary = _git_common_root(worktree)
    if primary is None:
        return f"cannot resolve the repository root from the lane worktree {worktree}"
    checked_out = _current_branch(primary)
    if checked_out == target:
        return None
    return (f"refusing to merge: the lane's target is {target!r} but the primary worktree "
            f"{primary} has {checked_out or 'a detached HEAD'} checked out; check out "
            f"{target!r} and settle again")


def _settle_needs_review(args: argparse.Namespace, led: Ledger, lane: dict,
                         dispatch_id: str, worktree: Path) -> int:
    """Park the lane for a human. Records evidence; **performs no git mutation at all**."""
    document, _problem = _current_sidecar(worktree, dispatch_id)
    # Advisory only, and recorded here as well: a lane parked by hand can still be carrying a harness
    # self-report that disagrees with the registry, which is a fact the next reader wants. A lane with
    # no sidecar at all reported nothing, so there is nothing to advise about.
    warning = "" if document is None else harnesses.identity_warning(
        harness=lane.get("harness"), model_requested=lane.get("model_requested"),
        harness_reported=document.get("harness"), model_reported=document.get("model"))
    evidence = {
        "controller": "oprun-settle",
        "dispatch_id": dispatch_id,
        "verdict": "needs_review",
        "reason": args.needs_review,
        "worktree": str(worktree),
        "test_result": "not_run",
        "checked_at": _utc_now(),
        **_identity_fields(lane, worktree, dispatch_id, document),
        "identity_warning": warning,
        # The one shared builder: `commit` is the lane's own commit or None, never the commit the
        # lane was cut from.
        **git_evidence(worktree, lane.get("base_commit")),
    }
    result = led.settle(args.lane, dispatch_id, False, evidence=evidence, reason=args.needs_review)
    if not result.get("accepted"):
        raise CLIError(f"ledger refused the settlement: {result.get('reason')}", EXIT_ILLEGAL)
    print(f"oprun settle: lane {args.lane!r} -> {result['status']} (needs_review)")
    print(f"  reason: {args.needs_review}")
    return EXIT_OK


def _settle_refused(args: argparse.Namespace, led: Ledger, dispatch_id: str, worktree: Path,
                    reason: str, identity: dict, *, identity_warning: str = "") -> int:
    """Park a lane the controller REFUSED to accept. **Performs no git mutation at all.**

    A refused ``--accept`` is a settlement, not a state the operator has to fix by hand: the
    reason is recorded on the lane, the lane can never be read as ``completed`` (there is no
    ``success`` in its history), and the CLI still exits non-zero so a scripted caller cannot
    mistake the refusal for an acceptance.

    ``identity_warning`` is the advisory half of the same comparison — a harness self-report that
    disagrees with the registry, say — and is carried onto the park so the reviewer sees the whole
    observation, not just the fatal part of it.
    """
    evidence = {
        "controller": "oprun-settle",
        "dispatch_id": dispatch_id,
        "verdict": "needs_review",
        "reason": reason,
        "worktree": str(worktree),
        "test_result": "not_run",
        "checked_at": _utc_now(),
        **identity,
        "identity_warning": identity_warning,
        **git_evidence(worktree, led.lane(args.lane).get("base_commit")),
    }
    result = led.settle(args.lane, dispatch_id, False, evidence=evidence, reason=reason)
    if not result.get("accepted"):
        raise CLIError(f"ledger refused the settlement: {result.get('reason')}", EXIT_ILLEGAL)
    if identity_warning:
        print(f"  identity warning (advisory): {identity_warning}")
    print(f"oprun settle: lane {args.lane!r} -> {result['status']} (needs_review)")
    print(f"  reason: {reason}")
    print(f"oprun settle: refusing --accept for {args.lane!r}: the lane is parked for review",
          file=sys.stderr)
    return EXIT_ERROR


def cmd_settle(args: argparse.Namespace) -> int:
    """Accept a lane on evidence, or park it. Commit/push/merge happen only inside the envelope."""
    ledger_path = _require_ledger(_ledger_path(args))
    led = _load_ledger(ledger_path)
    lane = dict(led.lane(args.lane))
    worktree = Path(str(lane.get("worktree") or ".")).expanduser()
    dispatch_id = lane.get("dispatch_id")
    if lane.get("status") != DISPATCHED or not dispatch_id:
        raise CLIError(
            f"lane {args.lane!r} is {lane.get('status')!r}, not dispatched: "
            f"only an in-flight lane can be settled",
            EXIT_ILLEGAL,
        )

    if args.needs_review is not None:
        if any((args.force, args.rewrite_history, args.delete_branch, args.tag, args.release,
                args.repo, args.into)):
            raise CLIError("--needs-review accepts no git target or side-effect flags", EXIT_USAGE)
        if not args.needs_review.strip():
            raise CLIError("--needs-review needs a reason: it is what the next reader gets")
        return _settle_needs_review(args, led, lane, str(dispatch_id), worktree)

    envelope = normalize_envelope(led.data.get("approvals"))

    # 1. No implicit widening. Refuse BEFORE any mutation, so a refused settle is inert.
    problems = _risky_request(args, envelope, worktree)
    if problems:
        raise CLIError("; ".join(problems), EXIT_REFUSED)

    # 1b. The merge target is fixed here — once, before anything moves. It comes from the lane's
    #     own record (``dispatch --into``), or from ``--into`` on this settle; a lane dispatched
    #     with one target refuses a different one, and a target stated for the first time here is
    #     recorded so the next reader sees the same intent.
    merge_target = _merge_target(lane, args.lane, getattr(args, "into", None))
    if merge_target and not lane.get("merge_into"):
        _record_merge_target(ledger_path, args.lane, merge_target)
        lane["merge_into"] = merge_target

    # 2. Identity: the ledger's designation vs what the worker reported. This runs before the test
    #    re-run and before any git side effect, so a substituted lane is never committed, pushed or
    #    merged either. The MODEL is the guard and a mismatch PARKS the lane (needs_review) — with one
    #    measured exception: on a harness that declares ``pin_verifiable=False`` (codex: the
    #    ``exec --json`` stream carries no model id at all) the pin provably reaches the CLI but
    #    nothing coming back can corroborate it, so a reported difference is the advisory
    #    ``identity_warning`` instead of a false-negative park. The HARNESS is never enforced: oprun
    #    chose the binary from the registry, so a disagreeing self-report is recorded and warned
    #    about, nothing more.
    document, problem = _current_sidecar(worktree, str(dispatch_id))
    if problem:
        return _settle_refused(args, led, str(dispatch_id), worktree, problem,
                               _identity_fields(lane, worktree, str(dispatch_id), None))
    warning = ""
    if document is not None:
        warning = harnesses.identity_warning(harness=lane.get("harness"),
                                             model_requested=lane.get("model_requested"),
                                             harness_reported=document.get("harness"),
                                             model_reported=document.get("model"))
        reason = harnesses.identity_reason(harness=lane.get("harness"),
                                           model_requested=lane.get("model_requested"),
                                           harness_reported=document.get("harness"),
                                           model_reported=document.get("model"))
        if reason:
            return _settle_refused(args, led, str(dispatch_id), worktree, reason,
                                   _identity_fields(lane, worktree, str(dispatch_id), document),
                                   identity_warning=warning)

    # 3. The controller's own re-run of the lane's test. Red test => no accept.
    rc, tail = _run_test_cmd(lane.get("test_cmd") or [], worktree, lane.get(UNIT_PATH_FIELD))
    if rc != 0:
        raise CLIError(
            f"lane {args.lane!r} test_cmd exited {rc}; refusing --accept "
            f"(use --needs-review). output tail: {tail[-200:]!r}",
            EXIT_ERROR,
        )

    branch = _current_branch(worktree)
    planned = _planned_actions(worktree, branch)

    # 3b. The merge target, printed BEFORE any mutation: a lane records where its work lands, so
    #     the operator sees the branch this settle is aimed at before anything moves.
    if branch and merge_target:
        print(f"  merge target: {merge_target}")

    # 4. Anything the envelope does not grant is asked about exactly once, and recorded.
    missing = [action for action in planned if not is_granted(envelope, action)]
    approval_prompt = None
    if missing:
        answer = _ask_once(args.lane, missing)
        approval_prompt = {"asked": missing, "answer": answer, "at": _utc_now()}
        if answer == "yes":
            grant_keys(envelope, missing, by=args.by, scope=args.scope, via="settle-prompt")
        _update_document(ledger_path, lambda doc: doc.update({"approvals": envelope}))
        led = _load_ledger(ledger_path)

    allowed = {action for action in GIT_ACTIONS if is_granted(envelope, action)}

    # 4b. The recorded target is enforced against the REAL checkout, before any of this settle's git
    #     side effects: a merge the envelope allows but the primary worktree cannot receive is
    #     refused here, naming the target and the branch actually checked out. Nothing below this
    #     line runs, so no commit and no push precede the refusal either.
    if "merge" in allowed and branch and merge_target:
        problem = _merge_target_problem(worktree, merge_target)
        if problem:
            raise CLIError(problem, EXIT_REFUSED)

    # 5. Git side effects, in order: commit -> push -> merge -> (tag / release).
    commit_info: dict | None = None
    push_info: dict | None = None
    merge_info: dict | None = None
    tag_info: dict | None = None
    release_info: dict | None = None
    delete_info: dict | None = None
    failures: list[str] = []

    if "commit" in allowed:
        commit_info = _do_commit(worktree, args.lane, str(dispatch_id))
        if not commit_info.get("ok"):
            failures.append(f"commit: {commit_info.get('detail')}")
    if "push" in allowed:
        remote = "origin"
        push_info = _do_push(worktree, remote, branch, force=bool(args.force or args.rewrite_history),
                             repo_url=args.repo) if branch else {
            "ok": False, "skipped": "the lane worktree is in a detached HEAD"}
        if not push_info.get("ok"):
            failures.append(f"push: {push_info.get('detail')}")
    if "merge" in allowed and branch:
        merge_info = _do_merge(worktree, branch, into=merge_target)
        if not merge_info.get("ok"):
            failures.append(f"merge: {merge_info.get('detail')}")
    if args.tag:
        tag_info = _do_tag(worktree, "origin", args.tag)
        if not tag_info.get("ok"):
            failures.append(f"tag: {tag_info.get('detail')}")
    if args.release and args.tag:
        release_info = _do_release(worktree, args.tag)
        if not release_info.get("ok"):
            failures.append(f"release: {release_info.get('detail')}")
    if args.delete_branch:
        delete_info = _do_delete_branch(worktree, "origin", args.delete_branch)
        if not delete_info.get("ok"):
            failures.append(f"delete-branch: {delete_info.get('detail')}")

    # 6. Evidence: the SHAs actually produced, the identity actually observed, and nothing invented.
    #    ``git_info`` comes from the ONE shared builder, anchored on the lane's starting commit: it
    #    names a commit only when HEAD has moved off that anchor (the lane committed its own work)
    #    and there is no uncommitted work left to describe. Never the base SHA.
    git_info = git_evidence(worktree, lane.get("base_commit"))
    evidence = {
        "controller": "oprun-settle",
        "dispatch_id": str(dispatch_id),
        "verdict": "accepted",
        "lane": args.lane,
        "branch": branch,
        "worktree": str(worktree),
        "test_cmd": [str(part) for part in (lane.get("test_cmd") or [])],
        "test_exit_code": rc,
        "test_result": "pass",
        "test_output_tail": tail,
        **_identity_fields(lane, worktree, str(dispatch_id), document),
        # Advisory only, on the accepted path too: an accepted lane can still carry a disagreeing
        # harness self-report, and the reviewer should not have to open the sidecar to find out.
        "identity_warning": warning,
        "commit": (commit_info or {}).get("sha") or git_info.get("commit") or None,
        # What the commit actually recorded, and what was kept out of it. A reviewer can read the
        # sidecar dir shipping question off the ledger instead of re-running `git show --stat`.
        "commit_paths": (commit_info or {}).get("staged"),
        "commit_excluded": (commit_info or {}).get("excluded"),
        "uncommitted": git_info.get("uncommitted"),
        "hashes": git_info.get("hashes") or {},
        # The branch this settle was allowed to merge into (recorded at dispatch / settle), so a
        # reviewer can see the target without re-reading the CLI invocation.
        "merge_target": merge_target,
        "push": push_info,
        "merge": merge_info,
        "tag": tag_info,
        "release": release_info,
        "delete_branch": delete_info,
        "side_effect_failures": failures,
        "approvals": {key: is_granted(envelope, key) for key in APPROVAL_KEYS},
        "approval_prompt": approval_prompt,
        "checked_at": _utc_now(),
    }
    result = led.settle(args.lane, str(dispatch_id), True, evidence=evidence,
                        reason="accepted on evidence: controller test rc=0")
    if not result.get("accepted"):
        raise CLIError(f"ledger refused the settlement: {result.get('reason')}", EXIT_ILLEGAL)

    print(f"oprun settle: lane {args.lane!r} -> {result['status']} (accepted, dispatch {dispatch_id})")
    print(f"  test:   rc={rc}")
    if warning:
        print(f"  identity warning (advisory): {warning}")
    if commit_info is not None:
        print(f"  commit: {commit_info.get('sha') or '(nothing to commit)'}")
    if push_info is not None:
        print(f"  push:   {push_info.get('target')} {push_info.get('ref')} "
              f"-> {push_info.get('sha')} ok={push_info.get('ok')}")
    if merge_info is not None:
        print(f"  merge:  into {merge_info.get('into') or merge_info.get('skipped')} "
              f"-> {merge_info.get('sha') or 'n/a'}")
    if tag_info is not None:
        print(f"  tag:    {tag_info.get('tag')} ok={tag_info.get('ok')}")
    if approval_prompt is not None:
        print(f"  approval prompt: asked {approval_prompt['asked']} -> {approval_prompt['answer']}")
    if failures:
        for failure in failures:
            print(f"  FAILED: {failure}", file=sys.stderr)
        return EXIT_SIDE_EFFECT
    return EXIT_OK


# --- verbs: status -----------------------------------------------------------
def _next_action(lanes: dict) -> str:
    """The single next thing a conductor should do, or ``none`` when the mission is settled."""
    if not lanes:
        return "none"
    for lane_id, lane in lanes.items():
        if lane.get("status") == BLOCKED:
            return f"review {lane_id} (blocked)"
    for lane_id, lane in lanes.items():
        if lane.get("status") == FAILED:
            return f"settle {lane_id} --needs-review REASON"
    for lane_id, lane in lanes.items():
        if lane.get("status") == DISPATCHED:
            return f"probe {lane_id}"
    for lane_id, lane in lanes.items():
        if lane.get("status") == PENDING:
            return f"dispatch {lane_id}"
    return "none"


def cmd_status(args: argparse.Namespace) -> int:
    """Lanes + counts + ``nextAction`` + **the active approval envelope**.

    Each lane is reported in its DISPLAY state (``ledger.display_state``): a lane settled as a
    non-acceptance reads ``parked`` — waiting for a decision — instead of ``failed``, which reads as
    "the mission broke". The stored state, the fences and ``nextAction`` are untouched.
    """
    ledger_path = _require_ledger(_ledger_path(args))
    doc = _read_doc(ledger_path)
    lanes = doc.get("lanes") if isinstance(doc.get("lanes"), dict) else {}
    envelope = normalize_envelope(doc.get("approvals"))
    display = {lane_id: display_state(lane) for lane_id, lane in lanes.items()}
    counts: dict[str, int] = {}
    for state in display.values():
        counts[state] = counts.get(state, 0) + 1
    next_action = _next_action(lanes)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "mission": doc.get("mission", ""),
        "repo": doc.get("repo"),
        "ledger": str(ledger_path),
        "max_parallel": doc.get("max_parallel", 4),
        "failure_limit": doc.get("failure_limit", 3),
        "counts": counts,
        "display": display,
        "lanes": lanes,
        "nextAction": next_action,
        "approvals": envelope,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK

    print(f"mission:    {doc.get('mission') or '(none)'}")
    print(f"ledger:     {ledger_path}")
    print(f"lanes:      {len(lanes)}   " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for lane_id, lane in lanes.items():
        state = display[lane_id]
        line = (f"  {lane_id:<20} {state:<10} harness={lane.get('harness')}"
                f"  dispatch={lane.get('dispatch_id')}")
        if state == PARKED:
            # The word alone is not enough for a reader: say why the lane is waiting. The stored
            # status stays visible in ``status --json`` (``lanes.<id>.status``) and in the ledger
            # file; the text view shows the one word a human acts on.
            line += f"  reason={lane.get('needs_review_reason')!r}"
        if lane.get("merge_into"):
            line += f"  merge_into={lane['merge_into']}"
        print(line)
    print(envelope_text(envelope))
    print(f"nextAction: {next_action}")
    return EXIT_OK


# --- verbs: approve ----------------------------------------------------------
def cmd_approve(args: argparse.Namespace) -> int:
    """Widen or withdraw the standing envelope, recording ``at``/``by``, then print it."""
    ledger_path = _require_ledger(_ledger_path(args))
    grant = _split_keys(args.grant)
    revoke = _split_keys(args.revoke)
    if not grant and not revoke:
        raise CLIError("nothing to do: pass --grant and/or --revoke")
    _validate_keys([*grant, *revoke])

    def mutate(doc: dict) -> None:
        envelope = normalize_envelope(doc.get("approvals"))
        if revoke:
            revoke_keys(envelope, revoke)
        if grant:
            grant_keys(envelope, grant, by=args.by, scope=args.scope)
        doc["approvals"] = envelope
        doc["approval_history"] = [
            *(doc.get("approval_history") or []),
            {"at": _utc_now(), "by": args.by, "grant": grant, "revoke": revoke},
        ]

    doc = _update_document(ledger_path, mutate)
    envelope = normalize_envelope(doc.get("approvals"))
    if args.json:
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        if grant:
            print(f"oprun approve: granted {', '.join(grant)} (by {args.by}, scope {args.scope})")
        if revoke:
            print(f"oprun approve: revoked {', '.join(revoke)}")
        print(envelope_text(envelope))
    return EXIT_OK


# --- parser ------------------------------------------------------------------
def _add_ledger_option(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """``--ledger`` is accepted before or after the verb; the subparser's default is SUPPRESS so
    a value given before the verb is not overwritten."""
    parser.add_argument("--ledger", type=Path, metavar="PATH",
                        default=argparse.SUPPRESS if suppress else None,
                        help=f"path to the mission ledger (default: <repo>/{LEDGER_TAIL})")


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface: six verbs, each with its own ``--help``."""
    parser = argparse.ArgumentParser(
        prog="oprun",
        description="oprun v0.2 — one JSON ledger, one CLI, evidence-based acceptance.",
    )
    _add_ledger_option(parser)
    verbs = parser.add_subparsers(title="verbs", dest="verb", metavar="VERB", required=True)

    init = verbs.add_parser("init", help="create the ledger + mission dir (launches nothing)")
    init.add_argument("repo", help="the mission repo (a path; created if missing)")
    init.add_argument("--mission", default=None, help="one line naming the mission")
    init.add_argument("--approve", action="append", default=[], metavar="KEYS",
                      help=f"grant up front, comma-separated or repeated (of: "
                           f"{','.join(APPROVAL_KEYS)})")
    init.add_argument("--max-parallel", type=int, default=None, metavar="N",
                      help="refuse a dispatch beyond N concurrently-dispatched lanes (default 4)")
    init.add_argument("--failure-limit", type=int, default=None, metavar="N",
                      help="consecutive failures before the circuit breaker parks a lane (default 3)")
    init.add_argument("--by", default="user", help="who is granting (recorded in the ledger)")
    init.add_argument("--scope", default="mission", help="the grant's scope (default: mission)")
    _add_ledger_option(init, suppress=True)
    init.set_defaults(func=cmd_init)

    dispatch = verbs.add_parser("dispatch", help="register + launch ONE lane detached")
    dispatch.add_argument("lane", help="the lane's name")
    dispatch.add_argument("--harness", required=True, metavar="ID",
                          help=f"registry id (of: {','.join(harnesses.CANONICAL_IDS)})")
    dispatch.add_argument("--model", default=None, metavar="MODEL", help="model pin, if any")
    dispatch.add_argument("--worktree", default=None, metavar="PATH",
                          help="the lane's worktree (default: the mission repo)")
    dispatch.add_argument("--test-cmd", required=True, metavar="CMD",
                          help="the acceptance command the controller re-runs")
    dispatch.add_argument("--brief", default=None, metavar="PATH", help="a task brief file")
    dispatch.add_argument("--prompt", default=None, metavar="TEXT", help="the task, inline")
    dispatch.add_argument("--depends-on", action="append", default=[], metavar="LANE",
                          help="a parent lane that must be completed first (repeatable)")
    dispatch.add_argument("--into", default=None, metavar="BRANCH",
                          help="the branch this lane's work must land on (recorded; settle refuses "
                               "a primary worktree checked out elsewhere)")
    dispatch.add_argument("--depth", type=int, default=0, metavar="N",
                          help="nesting depth (0 = root; a nested lane is refused past max_depth)")
    _add_ledger_option(dispatch, suppress=True)
    dispatch.set_defaults(func=cmd_dispatch)

    probe = verbs.add_parser("probe", help="one deterministic verdict word: no model involved")
    probe.add_argument("lane", help="the lane to probe")
    probe.add_argument("--json", action="store_true", help="print the full result document")
    probe.add_argument("--wait", action="store_true", help="poll until the verdict is not pending")
    probe.add_argument("--timeout", type=float, default=900.0, metavar="SEC",
                       help="seconds before an artifact-less lane is stalled (default 900)")
    _add_ledger_option(probe, suppress=True)
    probe.set_defaults(func=cmd_probe)

    settle = verbs.add_parser("settle", help="accept on evidence, or park for review")
    settle.add_argument("lane", help="the lane to settle")
    outcome = settle.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--accept", action="store_true",
                         help="re-run the test; commit/push/merge only inside the envelope")
    outcome.add_argument("--needs-review", default=None, metavar="REASON",
                         help="park the lane for a human (no git mutation)")
    settle.add_argument("--force", action="store_true",
                        help="force-push: needs a fresh --grant destructive")
    settle.add_argument("--rewrite-history", "--rebase", dest="rewrite_history",
                        action="store_true",
                        help="push rewritten history: needs a fresh --grant destructive")
    settle.add_argument("--delete-branch", default=None, metavar="NAME",
                        help="delete a remote branch: needs a fresh --grant destructive")
    settle.add_argument("--tag", default=None, metavar="NAME",
                        help="create + push a tag: needs a fresh --grant publish")
    settle.add_argument("--release", action="store_true",
                        help="create a release for --tag: needs a fresh --grant publish")
    settle.add_argument("--repo", default=None, metavar="URL",
                        help="push to another repo: needs a fresh --grant publish")
    settle.add_argument("--into", default=None, metavar="BRANCH",
                        help="the branch to merge into: must match the lane's recorded --into; "
                             "refused when the primary worktree is checked out elsewhere")
    settle.add_argument("--by", default="user", help="who answered the approval prompt")
    settle.add_argument("--scope", default="mission", help="the grant's scope (default: mission)")
    _add_ledger_option(settle, suppress=True)
    settle.set_defaults(func=cmd_settle)

    status = verbs.add_parser("status", help="lanes, counts, nextAction, and the envelope")
    status.add_argument("--json", action="store_true", help="print one machine-readable object")
    _add_ledger_option(status, suppress=True)
    status.set_defaults(func=cmd_status)

    approve = verbs.add_parser("approve", help="widen or withdraw the approval envelope")
    approve.add_argument("--grant", action="append", default=[], metavar="KEYS",
                         help=f"grant, comma-separated or repeated (of: {','.join(APPROVAL_KEYS)})")
    approve.add_argument("--revoke", action="append", default=[], metavar="KEYS",
                         help="withdraw, comma-separated or repeated")
    approve.add_argument("--by", default="user", help="who is granting (recorded in the ledger)")
    approve.add_argument("--scope", default="mission", help="the grant's scope (default: mission)")
    approve.add_argument("--json", action="store_true", help="print the envelope as JSON")
    _add_ledger_option(approve, suppress=True)
    approve.set_defaults(func=cmd_approve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse, run one verb, and translate every failure into a one-line message + exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:                       # unreachable with required subparsers
        parser.print_help()
        return EXIT_USAGE
    try:
        return handler(args)
    except CLIError as exc:
        print(f"oprun {args.verb}: {exc}", file=sys.stderr)
        return exc.code
    except (IllegalTransition, ParallelismExceeded, DepthExceeded) as exc:
        print(f"oprun {args.verb}: refused by the ledger: {exc}", file=sys.stderr)
        return EXIT_ILLEGAL
    except KeyError as exc:
        print(f"oprun {args.verb}: unknown lane or harness: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"oprun {args.verb}: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:                   # a bug is still one line, never a traceback
        if os.environ.get("OPRUN_DEBUG"):
            raise
        print(f"oprun {args.verb}: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
