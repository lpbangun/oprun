# Changelog

## v0.2.5 — the drift gate

**The bug:** this repo had no CI at all, so the failure class that bit twice — a Hermes profile
install drifting from the repo, and `SKILL.md` pointing at a file the bundle does not ship — was
only ever caught by hand, after the fact. A checker nobody runs is a checker that does not exist.

- **`.github/workflows/ci.yml` — tests plus a skill-bundle gate.** The `tests` job runs the suite on
  3.11 (the conductor's box). The `skill-bundle` job runs `bash -n`, the reference audit, and an
  installer round trip that asserts the drift detector can *actually fail* — exit 2 for a modified
  file **and** for a removed file. An always-green checker is worse than none: it licenses the very
  drift it claims to prevent. A final step asserts the critical artifacts reach an install.
- **`scripts/check-skill-references.py` — every bundle path `SKILL.md` references must exist.** It
  catches the "the doc tells you to run something the bundle does not ship" class
  (`templates/run-proposal.md` was referenced while absent). Paths the doc *bans* are named in
  `INTENTIONALLY_ABSENT` with a reason, rather than being silenced by a pattern in the checker — a
  silencing heuristic is how a checker stops telling the truth. Prefixed references only: a bare
  filename that resolves to a bundle file by definition exists, so checking those can never fail.

## v0.2.4 — the install can be refreshed

**The bug:** a Hermes profile install is a *snapshot*, and it had drifted from this repo in both
directions — the install carried five conductor pitfalls the repo lacked, while the repo carried the
wake-up section and the `over_budget` / `--test-timeout` semantics the install lacked, and the
install's `scripts/` were pre-ruling. Because `SKILL.md`'s How-to-Run points at
`<this-skill>/scripts/oprun.py`, a stale install runs the **old completion path** (a 300s advance
ceiling, no `over_budget`, no collision refusal) against a repo that already has the fix.

- **`SKILL.md` — the five pitfalls, upstreamed.** `advance` does not integrate, so an accepted lane
  carries `commit=null` until someone settles it; a reused unit name makes `journalctl -u` a
  multi-run log; a background terminal call is wall-clock wrapped, so a long `probe --wait` dies
  rc 143 with no verdict; `/tmp` is wiped at every boot, so evidence must live under `$HOME`; and
  *assuming the installed skill is the repo*, with the re-sync checklist.
- **`scripts/install-skill.sh` — refresh the snapshot, or fail on drift.** Syncs `SKILL.md`,
  `CHANGELOG.md`, `scripts/`, `references/` and `templates/` into a profile install (`--profile`) or
  any `--dest`; `--check` writes nothing and exits 2 when anything differs, so CI can catch an
  install that would otherwise run stale semantics. The file set is globbed rather than hard-coded,
  so a newly added module is installed instead of silently missing.

## v0.2.3 — wake-up discipline and the run proposal

**The bug:** a lane runs under `systemd-run --user`, outside every channel the conductor listens on,
so nothing announces that it finished — a completed lane sat unnoticed until the user asked. The fix
is conductor discipline, not a daemon.

- **`SKILL.md` — probe on cadence.** Probe after every other conductor step (`init`, each `dispatch`,
  each `settle`, every user turn) and wait only in bounded `probe --wait --timeout` loops; `sleep N;
  probe` is named as the anti-pattern. A new *Wake-up* section documents the two pollable signals —
  `advance`'s exit summary (`accepted=… all_terminal=… final={…}`) and the sidecar's `finished_at` —
  with no heartbeat and no timer that dispatches.
- **`SKILL.md` — infra-127 is the environment, not the lane.** An `infra` verdict means the
  acceptance command never *started* (rc 127): re-probe with a full PATH, read the sidecar, retry once
  before parking — never settle it as a lane failure. A genuine red test still parks immediately.
  Pitfalls and the Verification checklist carry both rules.
- **`templates/run-proposal.md`** — a run-proposal table (lanes, harness+model, test gate, depends,
  envelope, caps, flow) so the envelope is approved **once per run**, not per dispatch.
- **`README.md`** — the quickstart gains the propose-then-init step and a *Wake-up* section; the
  probe verdict list now includes `infra`.

## v0.2.2 — routing surface and verification scars

**Routing:** Claude Code is no longer routed. It is gone from the default table, the adapters
reference, and the skill's constraints, because the operator does not use it. The **registry entry
stays**: it is the only harness that exercises `requires_model_pin` (its default model 403s on this
box) and the only witness that degrades `none` → `strong` once a pin is supplied, and the frozen
benchmark scores both. Removing it would be a reviewed change, not a doc edit.

**Skill (v0.2.1):** the Pitfalls and Verification sections now encode the two failures this build
actually hit — verifying that a command *exited* rather than what it *recorded* (a 12/12 run passed
over an `evidence.commit` that was the base SHA, and a reviewer zeroed the whole build for it), and
assuming a fix reached every call site when a duplicated helper's stale copy lived in a module that
never imported the fixed one.

## v0.2 — the witness

**What v0.1 could not do:** tell whether a *non-Hermes* harness had finished. Its worker set was
frozen to Pi/Hermes while real runs used Cursor, and its completion signal was a terminal status
line — measured: `done` fired in **1 of 7** real runs, while the worker's own sidecar artifact was
correct **5 of 5**. So v0.1 asked the wrong witness, and its README shipped empty.

### Added

- **`scripts/oprun.py` — one CLI, six verbs** (`init`, `dispatch`, `probe`, `settle`, `status`,
  `approve`). Before, the conductor drove several scripts by hand; now there is one entrypoint with
  one set of exit codes.
- **The approval envelope** (v0.1 asked for merge/push/publish every single time, which made
  autonomy useless). Decided **once** at `init` (`--approve commit,push,merge`), recorded in the
  ledger with `at`/`by`/`scope`, printed by `status`, and then acted on **without asking**.
  `settle --accept` performs the granted commit/push/merge with stdin closed. Not granted ⇒ **ask
  exactly once**. Never widening implicitly: a force-push, tag, release, history rewrite, branch
  delete or another-repo push is refused even when `push` is granted. `destructive` is never
  defaultable.
- **The deterministic witness** (`scripts/probe.py`): a lane is `done` only when its sidecar exists
  for the ledger's *current* dispatch, reports `success`, and the controller's own re-run of that
  lane's own `test_cmd` exits 0 and every evidence path resolves. No model, no network, no daemon.
- **`scripts/advance.py`** — a bounded unattended settle loop, so an approved mission progresses
  while you sleep with **zero conductor turns**, and exits instead of idling as a daemon.
- **Detached launch** (`scripts/launch.py`): `systemd-run --user` units land in
  `user@1000.service/app.slice`, outside the login session's scope, with `--collect` and real
  exit status read back from systemd. This is what replaced Herdr.
- A **harness registry** (`scripts/harnesses.py`): per-CLI unattended recipe + completion witness,
  including the two measured traps — `claude` is not routable without an explicit `--model` (its
  default 403s here), and `opencode` is not trusted to report completion (it drops its terminator).
- **Documentation that matches the code**: a real quickstart in `README.md`, `references/routing.md`
  (the user-overridable table), `references/hosting.md` (the detach story and its honest limit),
  and this changelog.

### Changed

- **Evidence integrity.** A lane that did not commit now records `uncommitted: true` plus content
  hashes instead of the shared base SHA dressed up as its work. Model pins compare **normalised**
  (`"Cursor Grok 4.6"` == `"cursor-grok-4.6"`), so the guard is not muted by spelling. A harness id
  comes from the registry, never from the worker's self-report.
- **Parallel lanes from one ledger.** `flock` plus a re-read under the lock, a `depends_on`
  dependency gate, a `max_parallel` cap and a hard nesting depth ceiling — measured at 6 writers ×
  25 cycles with zero lost updates.
- **A lane fails into a parked state.** Three consecutive failures trip the circuit breaker to
  `blocked`; a `needs_review` verdict stays parked. Nothing retries a broken lane blindly.

### Removed (with evidence)

- **The PTY / Herdr lane, and `references/herdr.md`.** A terminal status is not a witness: it
  reported `done` in 1 of 7 runs and could not distinguish *working* from *finished* from *blocked
  on an approval prompt*. Herdr is a host, not evidence.
- **`hermes kanban`, in any form** — not as a store, not as a per-mission mode switch. One JSON
  ledger is the only source of truth, forever: two stores means two resume stories and a conductor
  guessing which mission it is in.
- **Any model in the completion path** — including the "cheap model triages pane text" clause,
  which existed only to prop up the cut PTY lane.

## v0.1

Hermes-only long-horizon runner: a conductor session plus bounded Hermes coder/codevisor workers,
a `state.json` ledger, and a sidecar per worker. Kept: the conductor holds no product edits, one
mutating owner per worktree, evidence beats self-report, plain-file ledger.
