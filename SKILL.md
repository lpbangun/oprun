---
name: oprun
description: Use when a long-horizon software mission runs via oprun.
version: 0.2.1
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
5. every evidence path the sidecar names resolves on disk.

Anything else is `needs_input`, `stalled`, `pending` or `failed` — never `done`. A dead unit with a
valid sidecar can be done; a live unit with no sidecar is not. **systemd answers process lifetime,
never lane state.**

**The approval envelope is decided once and never widened implicitly.** `init --approve
commit,push,merge` records the envelope; inside it, `settle --accept` commits/pushes/merges with no
prompt. Outside it — force-push, tag, release, history rewrite, branch delete, another repo —
is refused and needs a fresh `approve --grant`. `destructive` is never defaultable. `status`
prints the envelope so the user can always see what the conductor may do.

**Routing is the user's.** `references/routing.md` is a table the user owns and may override; the
registry in `scripts/harnesses.py` only states what each CLI's unattended recipe and witness are.
Two measured constraints are not overridable: `claude` is not routable without an explicit
`--model`, and `opencode` is not trusted to report completion.

## How to Run

```bash
CLI=python3 <this-skill>/scripts/oprun.py
LEDGER="$REPO/.tmp/oprun/state.json"

$CLI init "$REPO" --mission "…" --approve commit,push,merge     # no launch
$CLI dispatch <lane> --harness <id> [--model PIN] \
      --worktree "$WT/<lane>" --test-cmd "CMD" --prompt "…" [--depends-on other]
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
   envelope you will ask for. Wait for the user's go-ahead if they did not already give it.
2. **`init` once**, with the envelope and caps: `--approve …`, `--max-parallel N`,
   `--failure-limit N`. Completion: the ledger parses and `status` prints the intended envelope.
3. **Write lanes coarse and complete.** One dispatch = one whole bounded unit, testable by a
   `test_cmd` the controller can re-run. Prefer N independent lanes over a chain: parallel lanes
   are lid-safe, a chain needs turns.
4. **Dispatch.** Never edit the lane's product files yourself. A worker only ever writes its
   sidecar; the ledger is written by the conductor.
5. **Probe, then settle.** Accept only on `probe`'s `done`. `needs_input`/`stalled` → look at the
   evidence and decide; `failed` → the ledger parked it. Never settle a lane you have not probed.
6. **Gates.** `needs_review`, a `blocked` lane, scope change, or anything outside the envelope:
   one line to the user, then wait. Everything inside the envelope: act, do not re-litigate.
7. **Finish.** `status` must show `nextAction: none` and every owned unit gone
   (`systemctl --user list-units 'oprun-*'`).

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
- **`git add -A` on a lane's worktree.** `.oprun/` sidecars and `__pycache__/` are lane evidence, not
  product. Stage explicit paths when integrating a lane's work, or the sidecar dir ships.

## Verification

- [ ] This session did not implement product code.
- [ ] No daemon, no scheduler, no kanban, no PTY/Herdr lane, no model in `probe`/`settle`/`advance`.
- [ ] Every acceptance came from `probe`/`advance` evidence, with the controller's own test re-run.
- [ ] **Evidence fields were asserted, not just the exit code** — in particular no lane's `commit` is
      the base SHA, and uncommitted lanes carry content hashes instead.
- [ ] No test or check was weakened to go green.
- [ ] Every git side effect stayed inside the recorded envelope; refusals named the missing grant.
- [ ] Integration staged explicit paths; no `.oprun/` or `__pycache__/` in the product tree.
- [ ] `status` ends at `nextAction: none` with zero live owned units.
