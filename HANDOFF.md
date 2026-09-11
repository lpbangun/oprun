# HANDOFF — oprun v0.2

You are in the **oprun v0.2** workspace. Read this first, then `docs/PROPOSAL-v0.2.md` (rev 2).

## Where we are

| | |
|---|---|
| Repo | `github.com/lpbangun/oprun` (public, MIT) |
| This worktree | `~/projects/oprun-worktrees/v0.2`, branch `v0.2` off `origin/main` |
| Parity | `v0.2` == `origin/main` == `f2d33c2` (v0.1) |
| State | **v0.1 only.** v0.2 is designed and evidence-backed but **not written**. |
| Changed so far | `HANDOFF.md`, `docs/PROPOSAL-v0.2.md` — nothing else |

## What v0.2 must fix

v0.1's one fatal gap: **an orchestrator could not tell whether a non-Hermes harness had finished.**
Its worker set was also frozen to Pi/Hermes while real runs used Cursor.

## The rules that govern everything here

1. **A lane is accepted on evidence it produced** — never on what an agent said, never on what a
   terminal looked like. Measured: terminal status reported `done` for 1 of 7 real runs; the worker's
   own sidecar was correct 5/5.
2. **`state.json` is the only source of truth.** No kanban, not even as a per-mission mode switch.
   systemd = process lifetime, sidecar = worker evidence, ledger = lane status. Those are
   complementary; they are **not** two authorities over the same question. (Full reasoning, including
   when kanban *would* win, in `docs/PROPOSAL-v0.2.md` §13.)
3. **Two kinds of worker only:** a **CLI lane** (default, strong witness) and an **in-context
   subagent** (recon/small edits, self-report only, **never** authority for acceptance).
   **There is no PTY/Herdr lane.** It was cut, with evidence.
4. **Launch every unattended lane detached** — `systemd-run --user`, which lands in
   `user@1000.service/app.slice`, outside the chat's login-session scope. That is what survives the
   desktop disconnecting. Never launch a worker in the chat's own session cgroup.
5. **Approvals are an envelope, not a per-action dialogue.** Take `--approve commit,push,merge` (and
   `deploy` when it is genuinely needed) at `init`; then **act inside it without asking.** Asking per
   commit/push defeats the point, and push/merge are often required for work to be verifiable at all.
   Never widen implicitly: force-push, tags, releases, history rewrites and destructive ops need a
   fresh grant even when `push` is granted. `destructive` is never defaultable.
6. **Unattended progress is not free.** The conductor only acts on a turn, so a mission that needs
   several sequential lanes will stall after one if the lid closes. Make lanes **coarse and complete**
   (default), or opt into a **cron heartbeat** running `oprun probe --sweep` — and if you do, that is
   a deliberate decision, not an accident.

## First steps for this session

1. Read, in this order: `docs/BENCHMARK-v0.2.md` (the frozen gate — **do not edit it**),
   `docs/PROPOSAL-v0.2.md` (the contract), `docs/WORKER-SET-v0.2.md` (who does what, on which model).
2. Re-verify the environment assumptions in §8 of the proposal and §4 of the benchmark still hold.
3. Start with the smallest load-bearing piece: **`scripts/ledger.py` + `tests/test_ledger.py`** (Phase 1,
   deliberately serialised — everything imports it). A working reference with 10/10 green tests is at
   `/tmp/canary3/oprun_ledger.py`; the parallel-safe version with `flock` is at `/tmp/oprun5/oprun_ledger.py`.
   **Port and harden — do not reinvent.**
4. Then `scripts/probe.py` (the witness), `advance.py` + `launch.py`, `harnesses.py`, the CLI, and
   **`SKILL.md` last** (it documents what actually works).
5. Ship gate: **`SCORE >= 9.0` AND `K=1` AND `FLOOR=17/17`** per the frozen benchmark. Unit tests alone
   score ~2.5 — the score is in the live, detached, two-vendor runs.
6. Layout is **repo root**: `scripts/`, `tests/`, `references/`, `templates/` beside the existing
   `scripts/dispatch.py`. **Do not create a nested `oprun/` directory.**

## Constraints

- **Do not** introduce a daemon, a second scheduler, `hermes kanban`, or dual-write of any kind.
- **Do not** let a model decide whether a lane is done. No model in `probe` at all.
- **Do not** weaken a test or a check to make something pass.
- One mutating owner per worktree. The conductor holds no product edits.
- Ask before: merge, push, publish, destructive, or scope expansion. Nothing else needs asking.
- Record the model **requested** and the model **reported** for every lane; mismatch ⇒ `needs_review`.

## Environment notes

- The conductor runs **on this VPS**; Hermes Desktop reaches it over **SSH**
  (`SSH_CONNECTION=100.93.6.125 … 100.81.6.117 22`). Closing the laptop ends that SSH session.
- **Closing the lid does not kill launched work.** `KillUserProcesses=false` and `Linger=yes`, and
  detached `systemd-run --user` units live outside the session scope entirely. Verified: a
  `session-1244.scope` in `closing` state still hosts a process with **53 days** of runtime.
- **But nothing *advances* while you are away** — the conductor only acts on a turn. Lanes finish and
  sidecars land; settling and the next dispatch wait for you. That is resume-on-reattach.
- `HOME=/home/logani` for `hermes`, `herdr`, `pi`.
- `pi` is **not on `PATH`** — use `/home/logani/.hermes/node/bin/pi`.
- A chat conductor has `HERMES_DELEGATED_CHILD_CONTEXT=1`; `hermes kanban` writes need
  `env -u HERMES_DELEGATED_CHILD_CONTEXT`. This is a workaround, **not architecture** — we do not use it.
- `claude` is installed but its **default model 403s** here; it needs an explicit `--model` pin to be a lane.
- `herdr integration status` shows `cursor: current (v1)` / `pi: current (v5)` — installed during
  research, and they do **not** make `cursor-agent` report `done`. Herdr is not a v0.2 surface at all.
