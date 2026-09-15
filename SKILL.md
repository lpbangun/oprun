---
name: oprun
description: Use when a long-horizon software mission runs via oprun.
version: 0.2.5
author: Logani Bangun (lpbangun), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [orchestration, long-horizon, ledger, witness, systemd, approvals]
    related_skills: [hermes-agent, coding-agent-providers, consult-codevisor]
---

# oprun

**This chat is the conductor.** It routes work and settles it on evidence. It **never implements**,
and it holds no product edits.

`oprun` is **one JSON ledger + one CLI + this skill**:

- ledger: `<repo>/.tmp/oprun/state.json` — the only source of truth
- CLI: `python3 scripts/oprun.py <init|dispatch|probe|settle|status|approve>`
- sidecar: `<worktree>/.oprun/result.<dispatch_id>.json` — what a worker produced

## When to Use

User says oprun, or asks this session to run a multi-step software mission to completion.

Do **not** use for one bounded edit you can do here in the turn, or strategy chat with no run.

## Do not load any other orchestration skill

oprun is the only orchestration authority for the mission. Do not load `conductor`, `kanban-*`,
`herdr`, `agent-loop-engineering`, or any other orchestration/delegation skill while conducting an
oprun mission, and do not hand a worker a brief that tells it to. If a worker reports having loaded
one, stop and rebrief it.

## Bans (each is a keep-out, not a preference)

- **No daemon and no second scheduler** — including a cron/timer heartbeat that dispatches or
  settles. `advance` is a bounded process that exits; that is the whole difference.
- **No `hermes kanban` in any form** — not as a store, not as a per-mission mode switch, not
  "just for locking". Two stores means two resume stories and a conductor guessing which mission it
  is in.
- **No PTY / Herdr lane**, and no `references/herdr.md`. A terminal status is not a witness:
  measured `done` in 1 of 7 real runs; `idle` meant working, finished, and blocked-on-a-prompt
  identically. Whoever wants to steer a run live should open a terminal — that need is not a lane.
- **No model anywhere in the completion path** — not in `probe`, not in `settle`, not "cheap
  triage" of run output. The witness is deterministic.
- **No weakened tests, checks, or timeouts** to make something green. If a check is wrong, fix the
  check in a commit that says why.
- **No implicit approval widening**, no invented human gates, no per-action ask-loop.
- **No second writer**: a lane never writes the ledger; two mutating owners never share a worktree.

## The contract

**Acceptance is evidence, never report.** A lane is `done` only when *all* of these hold:

1. its sidecar exists at `<worktree>/.oprun/result.<dispatch_id>.json` for the ledger's **current**
   dispatch;
2. `task_id` and `dispatch_id` in the sidecar match that lane and that dispatch;
3. `status == "success"`;
4. the controller re-runs the lane's own `test_cmd` and it exits 0;
5. every evidence path the sidecar names resolves on disk; or the sidecar contains an explicit non-empty waiver in its evidence object when no artifact path is produced.

A missing evidence object, blank/non-string file entries, or a blank waiver is unresolved evidence. It is a typed `evidence_unresolved` park, never acceptance. A reviewer-unavailable marker is the separate typed `review_unavailable` park. Anything else is `needs_input`, `stalled`, `pending` or `failed` — never `done`. A dead unit with a
valid sidecar can be done; a live unit with no sidecar is not. **systemd answers process lifetime,
never lane state.**

**The approval envelope is decided once and never widened implicitly.** `init --approve
commit,push,merge` records the envelope; inside it, `settle --accept` commits/pushes/merges with no
prompt. Outside it — force-push, tag, release, history rewrite, branch delete, another repo —
is refused and needs a fresh `approve --grant`. `destructive` is never defaultable. `status`
prints the envelope so the user can always see what the conductor may do.

**Routing is the user's.** `references/routing.md` is a table the user owns and may override; the
registry in `scripts/harnesses.py` only states what each CLI's unattended recipe and witness are.
One measured constraint is not overridable: `opencode` is not trusted to report completion.

## How to Run

```bash
CLI=python3 <this-skill>/scripts/oprun.py
LEDGER="$REPO/.tmp/oprun/state.json"

$CLI init "$REPO" --mission "…" --approve commit,push,merge     # no launch
$CLI dispatch <lane> --harness <id> [--model PIN] \
      --worktree "$WT/<lane>" --test-cmd "CMD" --prompt "…" [--depends-on other] \
      [--test-timeout SEC]                                      # a lane's acceptance budget
$CLI probe <lane> [--wait --timeout 900]                        # one word; --json for detail
$CLI settle <lane> --accept | --needs-review "…"
$CLI status [--json]                                            # lanes + nextAction + envelope
$CLI approve --grant commit,push,merge | --revoke push
```

Unattended, with the envelope already granted:

```bash
systemd-run --user --unit oprun-advance --collect -- \
  python3 <this-skill>/scripts/advance.py --ledger "$LEDGER" --timeout 600
```

`dispatch` launches through `scripts/launch.py` (`systemd-run --user`, detached) — never a shell
`&`, `nohup`, `tmux` or a pane. See `references/hosting.md`.

## Procedure

1. **Intake, nothing launched.** Read the repo, the request, and any existing ledger. Five-line
   plan: objective, repo, lane→harness (from `references/routing.md`), worktree per lane, and the
   envelope you will ask for — written out as a run proposal (`templates/run-proposal.md`) so the
   user approves the **run** once, not each dispatch. Wait for the user's go-ahead if they did not
   already give it.
2. **`init` once**, with the envelope and caps: `--approve …`, `--max-parallel N`,
   `--failure-limit N`. Completion: the ledger parses and `status` prints the intended envelope.
3. **Write lanes coarse and complete.** One dispatch = one whole bounded unit, testable by a
   `test_cmd` the controller can re-run. Prefer N independent lanes over a chain: parallel lanes
   are lid-safe, a chain needs turns.
4. **Dispatch.** Never edit the lane's product files yourself. A worker only ever writes its
   sidecar; the ledger is written by the conductor.
5. **Probe, then settle — on cadence.** Accept only on `probe`'s `done`. `needs_input`/`stalled` →
   look at the evidence and decide; `failed` → the ledger parked it. Never settle a lane you have
   not probed. Probe after **every** other conductor step and after every user turn — see
   *Wake-up* below; an `infra` verdict is debugged, not parked.
6. **Gates.** `needs_review`, a `blocked` lane, scope change, or anything outside the envelope:
   one line to the user, then wait. Everything inside the envelope: act, do not re-litigate.
7. **Finish.** `status` must show `nextAction: none` and every owned unit gone
   (`systemctl --user list-units 'oprun-*'`).

## Wake-up: how a finished lane is noticed

A lane runs under `systemd-run --user`, **outside every channel this chat listens on**. Nothing pings
the conductor when a worker exits: a finished lane sits there, sidecar and all, until someone asks —
which is exactly how a run stalls unnoticed. The fix is discipline, not a daemon: **probe on
cadence.**

1. **Probe after every other conductor step.** After `init`, after each `dispatch`, after each
   `settle`, after any user turn inside the run, and before you answer the user, run `probe <lane>`
   for every lane the ledger holds. The verdict is one deterministic word
   (`done|infra|over_budget|evidence_unresolved|review_unavailable|pending|needs_input|stalled|failed`) — cheap enough to spend, and the
   only thing that speaks for a lane. `status`, `systemctl` and pane text are context, never the
   verdict. Probing an already-`done` lane is free, so cost is never a reason not to look.
2. **Wait bounded, never open.** `probe <lane> --wait --timeout 900` polls until the verdict is no
   longer `pending`, then returns; `--timeout` is the budget, and `pending` at the deadline means
   *not yet*, not *never*. Loop it in bounded turns instead of holding a turn open. `sleep 600;
   probe` is the anti-pattern this rule exists to prevent: it neither proves progress nor notices it
   early.
3. **The completion hook is opt-in, and there is no daemon behind it.** No heartbeat, no timer that
   dispatches, no resident poller. Two artifacts carry the signal, and the conductor reads them on
   its own cadence:
   - **`advance`'s exit summary** — its last line is
     `oprun-advance: accepted=… needs_review=… stalled=… timed_out=… all_terminal=… final={…}`
     (`--json` puts that object on stdout, one line per lane on stderr). A *returned* `advance` is
     itself the poll: the counts and `all_terminal` say what settled and what stayed parked.
     Launch it detached **once** next to the lanes; do not re-launch it on a timer.
   - **the sidecar's `finished_at`** — every worker stamps its own sidecar with ISO-8601 UTC when it
     stops, so `<worktree>/.oprun/result.<dispatch_id>.json` appearing with a `finished_at`, for the
     ledger's **current** `dispatch_id`, is the coarse "something ended" signal the conductor polls
     when it has no other reason to look. It is not acceptance: `finished_at` says the worker
     *stopped*; `probe` says whether the lane is *done*.

**infra-127: debug, do not park.** An `infra` verdict — rc 127, or the acceptance command never
started — means the **environment** failed, not the lane; no test was proven red, so it must never be
settled as a failure or charged to the circuit breaker.

- **Re-probe with a full `PATH`.** A unit gets no login shell and can inherit a bare PATH; 127
  usually means the program (or a venv `bin`) was not resolvable under the unit's PATH. Probe from a
  login shell, or re-dispatch with the lane's `env` PATH pinned.
- **Read the sidecar.** If a valid sidecar exists for the current dispatch, the worker did its job
  and the failure is in the controller's own re-run — fix that, do not blame the lane.
- **Retry before parking.** One bounded manual retry, then let `advance` do its own bounded
  `INFRA_RETRY` up to the ledger's `infra_limit`. A **genuine** red test still parks immediately: a
  non-zero rc with no `infra_reason` is the lane failing, and that never gets a free retry.

**over_budget: escalate, do not re-probe forever.** An `over_budget` verdict means the acceptance
command ran past the *acceptance budget the lane recorded at dispatch* (`--test-timeout`, default
900s). Nothing was proven red and nothing in the environment failed, so it is neither a red test nor
an `infra` — and re-probing the same lane changes nothing. Two moves, both honest: re-dispatch with a
budget the lane can actually meet (`dispatch --test-timeout SEC`), or park it for review with the
reason. `advance` parks it by itself (`blocked`, reason naming `over_budget`, failure streak
untouched, no `infra` retry spent).

## Pitfalls

- **`status`/pane text is not a verdict.** Only `probe` is. A worker saying "done" is a claim.
- **Settling without probing.** `settle --accept` re-runs the test but does not re-validate the
  sidecar's fences; probe first.
- **Launching in the chat's session cgroup.** Any detach that is not `launch.launch()` reintroduces
  the lid-close failure. One forgotten detach is enough.
- **Widening by habit.** `push` does not mean force-push, and it does not mean tag or another
  repo. Each needs its own grant, recorded.
- **Asking per action.** If the envelope covers it, act. Asking every commit is the failure the
  envelope exists to prevent.
- **A lane that needs the conductor between steps.** Advance will not retry a broken lane; make
  lanes that either finish or park.
- **`--amend` before a push is fine; a force-push is not.** Rewriting *published* history needs the
  `destructive` grant.
- **Verifying that a command exited, not what it recorded.** A green run says nothing about the fields
  it wrote. Assert the semantic property — e.g. that `evidence.commit` identifies *this* lane's work
  and is **not** the base SHA. A suite that only checked "the runner exited 0" passed 12/12 over a
  ledger field that was false, and a reviewer zeroed the whole build for it.
- **Assuming a fix reached every call site.** When a helper exists twice, the fix lands in one copy and
  the other keeps the bug — especially when the stale copy lives in a module that never imports the
  fixed one. `grep` the **function name**, not just its call sites.
- **Waiting open, or never looking.** A finished lane is invisible until someone probes it, and
  `sleep N; probe` is neither a wait nor a check. Probe after every step; use `--wait --timeout` for
  a bounded wait. The bug this encodes is a lane that finished and sat unnoticed until the user
  asked.
- **Settling an `infra` verdict as if the lane failed.** rc 127 means the environment could not
  *start* the acceptance command — re-probe with a full PATH, read the sidecar, retry once before
  parking. A genuine red test (non-zero rc, no `infra_reason`) still parks immediately.
- **`git add -A` on a lane's worktree.** `.oprun/` sidecars and `__pycache__/` are lane evidence, not
  product. Stage explicit paths when integrating a lane's work, or the sidecar dir ships.
- **A lane whose unit is already running.** `dispatch` refuses (non-zero) when `oprun-<lane>` is
  already active, and prints the one command that frees the name:
  `systemctl --user stop oprun-<lane>`. It never stops a live worker itself and never settles the
  lane as a failure just because the name is occupied — the running worker may still be about to
  write evidence a human wants — and the lane is left ready to be dispatched. There is deliberately
  no `oprun stop` / `oprun reap` verb: native systemd is the smallest correct escape.
- **One timeout clock for everything.** A lane's *acceptance budget* (`dispatch --test-timeout`,
  default 900s, recorded on the lane and read by every acceptance re-run) is how long the
  controller's re-run of `--test-cmd` may take; `probe`/`advance --timeout` is only the *staleness*
  clock (how long a lane may produce no artifact) and never shrinks a command that has already
  started. A command that outruns the lane's recorded budget is `over_budget`: it parks for review,
  spends no `infra` retry and touches no failure streak. Fix the budget, not the verdict.
- **Expecting `advance` to integrate.** It settles the *ledger* on evidence and performs no git side
  effects — commit/push/merge live in the CLI's `settle --accept`. An advance-accepted lane therefore
  carries `commit=null, uncommitted=true, hashes=<non-empty>` until someone settles it.
- **Reading `journalctl -u <unit>` as if the unit name were per-run.** It is not: reuse a name like
  `oprun-bench-advance` and the journal accumulates every earlier run. Filter by the ledger path in
  the `Started …` line, and remember `--wait --pipe` routes the runner's payload to the caller, so
  the ACCEPTED/NEEDS_REVIEW lines live in your captured stdout, not the journal.
- **Waiting for a long lane from a background terminal call.** The agent harness wraps a background
  command in a wall clock (`timeout N`), so a long `probe --wait` started that way is SIGTERM'd
  (rc 143) and takes no verdict. Poll in bounded foreground calls, or run the waiter detached:
  `systemd-run --user --unit <name> --collect -- …`. A long external reviewer belongs in a unit too,
  never in `&`.
- **Keeping a mission's evidence in `/tmp`.** On this host `/tmp` is wiped at **every boot**
  (`systemd-tmpfiles` carries `D /tmp`), which destroys recorded evidence mid-mission — one reboot
  cost a whole benchmark bundle. Keep scratch and evidence under `$HOME` (e.g. `~/oprun-evidence/`),
  and treat a brief's `$SCRATCH/bench` as a path to relocate, not a destination.
- **Assuming the installed skill is the repo.** A Hermes profile install
  (`<profile>/skills/autonomous-ai-agents/oprun/`) is a *snapshot*: its `SKILL.md`, `scripts/` and
  `templates/` drift from the repo as the product moves, and a stale install is worse than a stale
  doc — How-to-Run points at `<this-skill>/scripts/oprun.py`, so the conductor runs the **old
  semantics** (a 300s advance ceiling, no `over_budget`, no collision refusal) while the repo has the
  fix. Re-sync with `scripts/install-skill.sh` (see `--check`) after any change to the completion
  path, and confirm every path the text references actually exists — `templates/run-proposal.md` was
  referenced for a while but absent from the install.

## Verification

- [ ] This session did not implement product code.
- [ ] No daemon, no scheduler, no kanban, no PTY/Herdr lane, no model in `probe`/`settle`/`advance`.
- [ ] Every acceptance came from `probe`/`advance` evidence, with the controller's own test re-run.
- [ ] **Evidence fields were asserted, not just the exit code** — in particular no lane's `commit` is
      the base SHA, and uncommitted lanes carry content hashes instead.
- [ ] No test or check was weakened to go green.
- [ ] **The conductor probed on cadence** — after every step and every user turn — and every wait was
      bounded (`probe --wait --timeout`), not an open sleep. No lane finished unnoticed.
- [ ] Any `infra` (rc 127) verdict was debugged — full PATH, sidecar read, bounded retry — and never
      settled as a lane failure; genuine red tests parked at once.
- [ ] The run was approved once against the proposal (`templates/run-proposal.md`); no per-dispatch
      approval prompt was invented.
- [ ] Every git side effect stayed inside the recorded envelope; refusals named the missing grant.
- [ ] Integration staged explicit paths; no `.oprun/` or `__pycache__/` in the product tree.
- [ ] `status` ends at `nextAction: none` with zero live owned units.
