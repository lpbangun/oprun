---
name: oprun
description: Use when long-horizon work runs via Pi or Hermes.
version: 0.1.0
author: Logani Bangun (lpbangun), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [orchestration, long-horizon, pi, hermes, herdr, codevisor]
    related_skills: [herdr, consult-codevisor, hermes-agent, coding-agent-providers]
---

# oprun

Thin long-horizon runner. **This chat is the conductor** (not a product writer). Workers are only **Pi**, **Hermes coder**, or **Hermes codevisor**. Ledger: `$REPO/.tmp/oprun/`. Herdr is a visible surface, not a second controller.

Not the `conductor` skill. Do not load `conductor`. Do not use Beads, OMP, Droid, Codex, `spawn-agent`, watchdogs, or a Hermes pane that orchestrates.

## When to Use

- User says oprun, `/oprun`, long-horizon, or asks this session to orchestrate multi-step software work with Pi and/or Hermes.
- Natural language is enough: rest of the turn is the mission text.

Don't use for: one bounded edit you can do here; strategy chat with no run.

## Prerequisites

- `HOME=/home/logani` for `hermes`, `herdr`, `pi`.
- Pi: `/home/logani/.hermes/node/bin/pi` (four tools + `pi-goal-x` / `pi-subagents`).
- Codevisor advise: skill `consult-codevisor`. Lane judge: `hermes -p codevisor` via `scripts/dispatch.py`.
- Worktrees: `~/projects/<repo>-worktrees/<slug>/`. Sync origin default before add.

## How to Run

`/oprun <mission>` or NL — instruction, not launch.
Existing `.tmp/oprun/state.json` → status/resume, never silent replace.

Hermes worker (waits until **process exit**):

```bash
HOME=/home/logani python3 <this-skill>/scripts/dispatch.py \
  --profile coder|codevisor \
  --provider "$PROVIDER" -m "$MODEL" --reasoning medium \
  --brief "$BRIEF" --workdir "$WORKTREE" \
  --max-turns N --run-budget SECONDS \
  --log "$LOG" --out "$SIDECAR"
```

Pi interactive: `herdr agent start pi --cwd "$WORKTREE" --workspace "$WP" --no-focus -- /home/logani/.hermes/node/bin/pi`, then `herdr pane run` a file-backed brief (or `/goal-direct` for a loop). Pi print: `cd "$WORKTREE" && HOME=/home/logani pi -p --no-session < brief`. Never treat pane text as proof.

## Harness (conductor picks the row)

Default **product writer = Pi**. Hermes coder is conductor + Hermes-native / exit-bounded jobs. Policy freeze is the allowed set; the conductor chooses per unit. Do not re-approve a routing table. Append `routeLog` after each real launch. New harness class or swap off this set → one-line ask.

| Use | Harness |
|---|---|
| Multi-step / overnight / until PASS or not-converged | **Pi goal** (`/goal` or `/goal-direct`) |
| One slice you may watch/steer | **Pi focused** (Herdr pane, file brief) |
| Tiny unattended patch that must exit | **Pi `-p`** (`cd` first; no `--cwd`) |
| Decompose, route, inspect evidence, talk to user | **This chat = conductor** (no product edits) |
| Needs Herdr/MCP/browser/skills, or parallel lane that must **exit** | **Hermes coder** (`dispatch.py`) |
| Architecture or PASS/FAIL | **Codevisor** (not a writer) |

Per unit: (1) product code → Pi (2) more than one verified slice / cap → Pi goal else Pi focused (3) Hermes tools or guaranteed exit without TUI → Hermes coder (4) advise/judge → Codevisor. Never two writers on one worktree. Never reuse a completed Pi session.

## Procedure

1. **Intake, nothing launched.** Read repo + any `state.json`. Five-line plan: objective, repo, harness per unit, herdr yes/no, merge/push = no. If the user did not already say start/go/do it, wait for `Start oprun`. Completion: plan on screen, no processes started.
2. **Write files.** `mission.md`, `state.json`, `briefs/<lane>.md`. Conductor `session`/`pid` from live process. `status=active`. Completion: files parse.
3. **Dispatch.** Conductor never edits product. Briefs: *do not load conductor; oprun is the only orchestration authority; implement or review only; write `$RESULT` with sha/commands/exit codes.* Completion: sidecar or Pi evidence path has integer `exitCode` or verified git SHA; `herdr pane get` matches expected agent if TUI.
4. **Supervise.** Inspect git/tests/result files. Update `lanes.*`, `routeLog`, `nextAction`. Parallel: `pending_children`; **no user-final** until every sidecar exists. Same-session resume. Completion: `status` is `pending_children` \| `active` \| `blocked` (named gate) \| `complete`.
5. **Gates.** Auto-advance inside the plan. Ask one line only for merge, push, publish, destructive, or scope expansion. Close finished Herdr panes you opened.

### state.json

`mission`, `status`, `conductor{session,pid,provider,model,soleOwner}`, `approvals{}`, `lanes{name:{status,worktree,branch,candidate,harness,evidence,exitCode,session}}`, `routeLog[]`, `nextAction`. Two pointers per lane (evidence path + exit code). No scorecards.

## Pitfalls

- **One-shot final.** Query-file exit is not conductor done. `pending_children` until every owned pid has a sidecar.
- **Conductor-skill back door.** If a worker `skill_view`s `conductor`, stop and rebrief.
- **spawn-agent.** Banned. Direct `hermes -p … --provider … -m …` or Pi only.
- **Herdr conductor pane.** Banned. systemd + `--pass-session-id` if this chat must outlive desktop.
- **Pi TUI idle ≠ done.** Require git/test evidence. Close completed panes.
- **Contributor muse.** Unattended contributor models can refuse. Do not flip training-tier flags; use `consult-codevisor` pool or a non-contributor pin.
- **Profile HOME.** `herdr`/`hermes`/`pi` need `HOME=/home/logani`.

## Verification

- [ ] This session did not load `conductor`.
- [ ] No OMP/Droid/Codex/`spawn-agent`/`bd`.
- [ ] Product sha from `git`; tests from real commands.
- [ ] Conductor wrote no product diff.
- [ ] User-final only with zero live owned children.
