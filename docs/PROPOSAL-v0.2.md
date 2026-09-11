# oprun v0.2 — proposed end product (rev 2)

Status: **proposal, not built.** Branch `v0.2` off `origin/main` (parity `f2d33c2`).
Rev 2 incorporates a Codevisor consult (grok-4.6/xai-oauth, session `20260911_202211_cdfab1`)
whose verdict removed a lane kind, a storage mode, and a triage clause from rev 1.

## 1. Thesis

v0.1 failed one way: **it could not tell whether a non-Hermes harness had finished.** Its worker set
was also frozen to Pi/Hermes while real runs used Cursor. Everything below closes those two gaps and
nothing else.

> v0.2 = **one JSON ledger + one small CLI + one skill**, where a conductor routes each unit of work
> to the harness best at it, and a lane is accepted only on **evidence it produced** — never on what
> an agent said, and never on what a terminal looked like.

## 2. What v0.2 is NOT

- **Not an Orca clone.** Three unrelated products share that name; we take mechanisms, not a system.
- **No daemon, no second scheduler.** The dispatch daemon is what produced the 50-day-blocked board.
- **No `hermes kanban` — not as a store, not as a per-mission mode switch.** *`state.json` is the only
  source of truth.* Kanban's sole unique claim was the dispatch daemon that auto-resumes after a
  crash — the same second scheduler already banned. A mode switch is dual-write with a config key:
  two code paths, two resume stories, and a conductor guessing which mission it is in. There is
  nothing left to opt into.
- **No PTY/Herdr lane** (see §7.2). Cut, not demoted to an exception.
- **No model deciding whether a lane is done**, and **no model triaging pane text.** The witness is
  deterministic. A prior "cheap model triages opaque pane text" clause existed only to prop up the
  PTY lane; it is deleted with it.
- **No invented human gates — and no per-action asking either.** Approvals are decided **once**, up
  front, as a scoped envelope (`--approve commit,push,merge`), then acted on without re-asking. Asking
  per commit/push is the *opposite* failure: it makes autonomy useless, and real work often needs push
  and merge to be verifiable at all. See §6.1. A gate is never invented; the user's envelope is.
- **Not a rewrite.** v0.1's good parts are kept: conductor holds no product edits, one mutating owner
  per worktree, evidence beats self-report, plain-file ledger.

## 3. Design — five nouns

| Noun | Meaning | Lives in |
|---|---|---|
| **Conductor** | A Hermes session. Routes. Never implements. | the chat |
| **Lane** | One unit of work: one harness, one worktree, one owner. | `state.json` |
| **Harness** | Adapter: unattended recipe + completion witness, per CLI. | `harnesses.py` |
| **Witness** | How completion is *proven*: sidecar + OS exit + controller re-run. | `probe.py` |
| **Ledger** | `$REPO/.tmp/oprun/state.json`. Greppable. No DB, no second authority. | `ledger.py` |

## 4. Hosting — the durability answer (this is what replaced Herdr)

The original reason to want Herdr was: *"closing the lid will stop the task."* **That threat model is
wrong, and the fix is `systemd-run`.**

Measured on the target box:

| Fact | Value |
|---|---|
| Conductor runs | **on the VPS** (QEMU guest, reparented to `PPID 1`); Hermes Desktop is a GUI client |
| Conductor's processes live in | `/user.slice/user-1000.slice/session-18514.scope` — a **login session scope** |
| A `systemd-run --user` unit lives in | `/user.slice/user-1000.slice/user@1000.service/app.slice/<unit>.service` — **outside the session scope** |
| Does it survive the session ending? | **Yes** — different cgroup; `Linger=yes`; `systemctl --user` is `running` |
| Exit code available? | **Yes** — `systemd-run --user --wait --pipe` returned 7 for an `exit 7` job, 0 for success |
| Audit trail | journald per unit, incl. the harness's own terminal event |

**Proven end-to-end:** a real `cursor-agent -p --yolo --trust` lane launched detached via
`systemd-run --user`, confirmed in the `user@1000.service` cgroup (≠ my session's), wrote its artifact
in ~18 s, and systemd reported `ExecMainStatus: 0 / Result: success` — read from systemd, **not from
the agent**. The controller then re-ran the frozen test: `OK`.

Note Codevisor's correction to my own earlier framing: *"the lid-close fear is a **session-scope**
problem, not a pane property."* Herdr's server is itself a 53-day daemon — a pane lives until closed.
Herdr was never what kept a job alive; it only made the job watchable.

**Deliberate consequence — resume-on-reattach.** The conductor process is still in a login session
scope. If it dies, workers keep running and sidecars still land, but nothing *settles* until a new
conductor reads the ledger. That is criterion 9, and it is enough. **Do not "fix" it by putting the
conductor in Herdr so it can be watched** — that reintroduces the stall.

**Rule:** every unattended lane is launched detached (`systemd-run --user`), never in the chat's own
session cgroup. One forgotten detach and the lid-close problem returns.

### 4.1 The lid question, answered precisely

The desktop reaches this VPS over **SSH** (`SSH_CONNECTION=100.93.6.125 … 100.81.6.117 22`), and the
conductor's agent process lives in that SSH session's scope (`session-18574.scope`,
`Remote=yes`, `Service=sshd`). So "closing the lid" ends an SSH session. What survives is decided by
logind, and the measured settings are:

| Setting | Value | Meaning |
|---|---|---|
| `KillUserProcesses` | **`false`** | **Ending a session does NOT kill its processes** — they are reparented and keep running |
| `Linger` (user) | **`yes`** | The user's systemd manager persists after logout, so `--user` units survive too |
| Kinds of cgroup | — | `systemd-run --user` units live in `user@1000.service/app.slice/<unit>`, **outside** the SSH session scope |

Corroborating evidence from the live box: `session-1244.scope` has been in `closing` state for
**1 month 22 days** while still hosting a process with **53 days** of runtime. Session-end does not
reap processes here.

**So, precisely:**

1. **Work already launched — survives.** A detached `systemd-run --user` lane is outside the session
   scope entirely, and `KillUserProcesses=false` protects the rest. Proven end-to-end: the lane ran,
   wrote its artifact, and systemd reported `ExecMainStatus: 0` with no client attached.
2. **The conductor process itself — survives.** Reparented to `PPID 1`, same reason.
3. **But nothing *advances* while you are away.** The conductor only acts on a turn. Ending a turn and
   closing the lid means the mission sits idle: lanes finish, sidecars land, and no further lane is
   dispatched and nothing is settled until you return and send a message. That is
   **resume-on-reattach**.

**Design consequence — this is the real lesson.** Because no turns happen unattended, **each lane must
be a complete unit of work.** If a mission needs five sequential lanes and each requires a conductor
turn, closing the lid stops the mission after lane one — not because anything died, but because
nothing progressed it. Two honest options:

- **(a) Coarse lanes (default):** one dispatch = the whole bounded unit, with a self-contained brief and
  a test command the controller can re-run on reattach. This is what `--goal`-style harnesses
  (Codex `/goal`, Claude `--max-turns`) are for. Lid-close is then harmless.
- **(b) Cron heartbeat (opt-in):** a `systemd` timer or Hermes cron running
  `oprun probe --sweep && oprun advance` so the mission continues unattended. This adds a scheduler
  and therefore needs an explicit decision — it is the one place a daemon is justified, and it is
  **not** a second source of truth (it reads the same ledger).

Do not paper over this with Herdr. Herdr does not create turns either.

### 4.2 Unattended progress — proven, and the mechanism is `oprun advance`

The user's requirement: *"I release tasks before I sleep and want progress unattended, given I already
approved the shape of the run."* That is achievable without a resident daemon.

**Three things make it work, and two of them matter more than the lanes themselves:**

1. **Parallel lanes are naturally lid-safe.** Dispatch N independent lanes before you sleep and they
   all run detached to completion. Nothing needs to advance them.
2. **`oprun advance` is the missing mechanism for everything else.** A **bounded** unit — not a
   daemon — that waits for dispatched lanes, validates each sidecar, **re-runs each lane's own test
   command**, and settles the ledger with evidence. It can be launched detached alongside the lanes,
   so a *sequential* mission progresses with no conductor turn at all. It holds no state of its own,
   runs from the same ledger, and exits.
3. **The conductor is then only needed for decisions**, not mechanics: unexpected failure, a
   `needs_review` verdict, a gate, or scope changes. Exactly the things a human should see.

**Honest limits of unattended progress:**

- **Execution is unattended. Correction is not.** If a lane fails, the circuit breaker parks it at
  `blocked` and the mission waits for you. Nothing retries a genuinely broken lane overnight.
- **Gates still stop it.** Anything outside the approval envelope halts the advance runner rather than
  asking the dark.
- **Pre-approve the shape, then sleep** is literally the design: `--approve commit,push,merge` (and
  `deploy` when needed) at `init`, then `oprun advance` runs the mission to the edge of its envelope.

**Proven end-to-end** (3 real `cursor-agent -p` lanes, separate worktrees, one frozen commit):

```
dispatched alpha|beta|gamma  -> all three units in user@1000.service/app.slice (≠ my session scope)
oprun-advance (detached)     -> [alpha] ACCEPTED exit=0 OK
                                [beta]  ACCEPTED exit=0 OK
                                [gamma] ACCEPTED exit=0 OK
final ledger                 -> {'completed': 3}   with model, commit, test evidence per lane
```

**Zero conductor turns occurred** between dispatch and a fully settled ledger. Each lane produced its
own module and each lane's independent test passed. Worktree isolation held: no lane touched another's
files.

## 5. End product — exact file tree

```
oprun/
├── SKILL.md                     # conductor contract: when to use, routing, bans, settlement
├── README.md                    # what it is + quickstart (v0.1 shipped this empty)
├── CHANGELOG.md
├── scripts/
│   ├── oprun.py                 # the CLI — the only entrypoint
│   ├── ledger.py                # pure (from,event)→to, dispatch fencing, circuit breaker
│   ├── harnesses.py             # registry: recipe + witness per CLI
│   ├── probe.py                 # deterministic witness; NO LLM
│   ├── advance.py               # bounded unattended settle loop (the "sleep on it" runner)
│   └── launch.py                # systemd-run --user wrapper (detached, exit-code capture)
│   └── dispatch.py              # v0.1's Hermes-worker launcher (kept)
├── templates/
│   ├── mission.md
│   ├── state.json               # schema-validated
│   └── worker-brief.md          # injected preamble incl. the completion protocol
├── references/
│   ├── routing.md               # unit type → harness + verifier (user-overridable)
│   ├── harness-adapters.md      # per-CLI recipes, witnesses, failure modes
│   └── hosting.md               # systemd-run detach, cgroups, journald, resume-on-reattach
└── tests/
    ├── test_ledger.py           # fencing, transitions, circuit breaker (10 green today)
    └── test_probe.py            # witness acceptance/rejection cases
```

Note: rev 1's `references/herdr.md` is **deleted**. Herdr is not a v0.2 surface.

## 6. The conductor's verbs

```bash
oprun init <repo> --mission "…" [--approve …]      # create ledger + mission, no launch
oprun dispatch <lane> --harness cursor --model …   # detached launch → prints dispatch_id
oprun probe <lane> [--wait] [--timeout 900]        # verdict; no LLM involved
oprun settle <lane> --accept | --needs-review …    # write ledger + evidence (no pane work)
oprun status [--json]
oprun approve <envelope>                           # widen/record the standing approval envelope
```

`probe` returns one of: `done` · `pending` · `needs_input` · `stalled` · `failed`.

### 6.1 Approval envelope — pre-approve instead of asking per action

Asking per commit/push is friction that makes autonomy useless: real work often **needs** push and
merge to be verifiable at all (a hosted check, a deploy, an integration test that only runs on the
remote). The fix is to decide the envelope **once, up front**, and then act inside it without asking.

```bash
oprun init <repo> --mission "…" --approve commit,push,merge
```

Recorded in the ledger as a first-class, auditable field:

```json
"approvals": {
  "commit":  {"granted": true,  "at": "…", "by": "user", "scope": "mission"},
  "push":    {"granted": true,  "at": "…", "by": "user", "scope": "mission"},
  "merge":   {"granted": true,  "at": "…", "by": "user", "scope": "mission"},
  "deploy":  {"granted": false},
  "publish": {"granted": false},
  "destructive": {"granted": false}
}
```

Rules:

- **Granted ⇒ never ask again for that mission.** `settle` may commit/push/merge directly and records
  the resulting SHAs as evidence. This is what makes `--approve commit,push,merge` worth typing.
- **Not granted ⇒ ask once, then record the answer** — a one-line ask, not a loop.
- **There is no implicit widening.** A later, riskier action (force-push, tag, release, branch delete,
  rewriting history, touching another repo) is always outside the envelope and needs a fresh grant,
  even if `push` is granted.
- **Never defaultable.** `destructive` and force-push are `false` unless explicitly typed.
- **Revocable and versioned.** Every grant carries `at`/`by`; `oprun approve` can widen or withdraw, and
  the ledger keeps the history. A grant is scoped to the mission that recorded it, never global.
- **The envelope is not silent.** `oprun status` prints the active envelope, so the user can always see
  what the conductor is currently allowed to do without asking.
- **Deploys count as a side effect, not a convenience.** `deploy` stays a separate grant from `push`
  even though they often travel together, because pushing a branch and shipping to production are
  different risks. `--approve commit,push,merge,deploy` is allowed — it just has to be said.

This replaces v0.1's "ask for merge/push/publish every time" with a scoped, recorded, non-widening
pre-authorization. The conductor's job is to *act* inside the envelope, not to re-litigate it.

**Acceptance rule:**

```
done  ⟺  sidecar exists
      ∧  sidecar.task_id + dispatch_id match the ledger's CURRENT dispatch
      ∧  sidecar.status == "success"
      ∧  the controller re-runs the lane's own test command and it exits 0
      ∧  every evidence path resolves
else → needs_review
```

**What probe must NOT do** (Codevisor): *"Probe never asks systemd whether the lane is `completed`.
A dead unit with a valid sidecar can be done; a live unit with no sidecar is not."*

- ledger = lane status
- systemd = process **lifetime** (and OS exit code), never lane state
- sidecar = worker evidence

Those three are complementary. `systemctl is-active` and ledger status are **not** two authorities
over the same question.

## 7. Routing

Routing is **user-specified and user-overridable** — "when to call Pi, when to call Cursor Agent" is a
table the user owns, never hardwired and never silently inferred. It ships in `references/routing.md`.

### 7.1 The distinction criterion

**Does the worker exit, and do we get an OS exit code plus a structured terminal event?**

Not capability — capability is measured equal (`cursor-agent -p` "has access to all tools, including
write and shell"; same class for the other headless recipes). Not "who the human is watching."
Whoever frames the axis as capability or visibility will build a worse system with more parts.

### 7.2 Two conductor-facing kinds — the PTY lane is cut

| Kind | Best for | Witness | Authority? |
|---|---|---|---|
| **CLI lane** *(default)* — `cursor-agent -p`, `codex exec`, `claude -p`, `droid exec`, `pi -p`, `hermes -p <profile> chat` | all real implementation; heavy/batched; long runs | **strong**: OS exit code + structured terminal event + sidecar + controller re-run. Launched detached via `systemd-run --user` | yes |
| **In-context subagent** (`delegate_task`) — a *different axis*: same process as the conductor | recon, mapping, parallel reads, small light edits | **weakest — self-report only.** Cannot write a lane sidecar | **never** — acceptance requires the controller to re-run that lane's tests |

**Cut from rev 1: the PTY / Herdr lane**, and with it `herdr.md`, any `herdr wait` in `probe`, pane
close as a load-bearing `settle` verb, and the pane-text triage clause. Reasons, all measured:

- `done` fired in **1 of 7** real runs; six completed and stayed `idle` forever.
- `idle` meant *working*, *finished*, and *blocked on an approval prompt* — three states, identical reporting.
- A `-p` run whose process genuinely **exited** never produced `done` (the pane was removed).
- The worker's own artifact was correct **5/5**.
- Installing Herdr's `cursor` integration did not fix it (that hook targets the Cursor IDE, not the CLI).

That is not a weak witness; it is a **non-witness**. And a PTY is a *mode*, not a product: Herdr and
tmux are hosts, and neither is evidence. If a human genuinely wants to steer a run live, **the human
opens a terminal** — encoding that need as a lane is what re-created the Absolute Learning stall.

### 7.3 Routing table

| Unit | Default harness | Verifier (different family) |
|---|---|---|
| Large multi-file refactor | Claude Code | Codex |
| Bounded one-slice patch + test | Codex `exec` | different harness re-runs tests |
| Heavy / batched authoring | Cursor `-p` | Hermes coder re-runs tests |
| Light impl + tests | Hermes coder | Codevisor |
| Architecture / PASS-FAIL judge | **not** the author's family | — |
| Repo recon / mapping | Gemini CLI / OpenCode, or a subagent | human or 2nd harness |
| Long loop until green | Codex `/goal` or Claude `--max-turns` | controller asserts + re-runs |
| Needs MCP / browser / skills | Hermes | 2nd instance |

Pins: every lane records the model **requested** and the model **reported**; mismatch ⇒ `needs_review`,
never a silent pass. (Measured: no harness echoes a canonical model id, so the sidecar carries it.)

## 8. Harness registry (measured 2026-09-11)

| Harness | Unattended recipe | Witness |
|---|---|---|
| cursor-agent | `-p --output-format json --yolo --trust` | `type:"result"`, `subtype` + `is_error` |
| codex | `exec --json -s workspace-write` | `turn.completed` |
| droid | `exec -o json --auto <level>` | `type:"result"` |
| claude | `-p --output-format json` | **needs a `--model` pin** (default model 403s on this box) |
| hermes | `-p <profile> chat --query-file` | exit code + session id |
| pi | `-p` (absolute path; **not on `PATH`**) | exit code |
| opencode | `run --format json` | `step_finish` — **known to drop it** (#26855) |

**Never trust `subtype` alone:** Claude emitted `subtype:"success"` *while* returning `is_error:true`
and exit 1. **OS exit code + `is_error` are authoritative.**

## 9. Acceptance criteria

v0.2 is done when all pass on real runs:

1. Two different-vendor harnesses run in parallel, isolated worktrees, from one ledger.
2. Each lane yields a valid sidecar; acceptance is evidence-based, not reported.
3. A deliberately failing lane lands in `needs_review` — never silently `done`.
4. A duplicate/late completion from a superseded dispatch is **rejected as stale**.
5. Three consecutive failures circuit-break the lane to `blocked`, and it cannot silently re-dispatch.
6. A lane whose artifact never appears past its timeout is classified `needs_input`/`stalled` and
   **escalated — never silently waited on.**
7. Every lane is launched **detached** (verified in the `user@1000.service` cgroup, not the chat's
   session scope), and settlement leaves zero live owned **units**.
8. `state.json` ends `complete` with `nextAction: none` — the v0.1 stall, fixed.
9. Kill the conductor mid-mission: a new session resumes from the ledger via `oprun status` / `probe`
   with no lost state (resume-on-reattach).
10. `tests/` green via real commands; no test weakened to pass.
11. **Approval envelope:** with `--approve commit,push,merge`, `settle --accept` commits/pushes/merges
    **without asking**, records the SHAs as evidence, and `oprun status` prints the active envelope.
    With the envelope absent, the same action asks exactly **once** — never in a loop.
12. **No implicit widening:** with `push` granted, a force-push or a tag still requires a fresh grant.
13. **Parallel lanes from one ledger:** N lanes in N worktrees run concurrently, each accepted on its
    own `test_cmd`, with no cross-contamination and no lost ledger updates.
14. **Concurrent-writer safety:** many simultaneous settles on one ledger lose nothing (fencing
    rejects duplicates; `flock` serializes writes; file always parses).
15. **Nested work is bounded:** a lane declaring depth > 1 raises `nested_worker_depth_exceeded`;
    every lane carries `parent_dispatch`, and no lane writes the ledger directly.
16. **Unattended progress:** with the envelope pre-approved, dispatching lanes plus a detached
    `oprun advance` reaches a settled ledger **with zero conductor turns**; a lane that fails parks at
    `blocked`/`needs_review` rather than being retried blindly.
17. **Evidence integrity:** a lane that did not commit reports `uncommitted: true` with content hashes —
    never a base SHA masquerading as the lane's work. Model comparison is normalized; harness identity
    comes from the registry, not the worker's self-report.

*(Rev 1's criteria 6–7 referenced idle panes and closing every pane; replaced.)*

## 10. Evidence base (all on this machine)

- `/tmp/oprun-audit/FINDINGS-local-forensics.md` — the measured v0.1 defects
- `/tmp/oprun-audit/OPRUN-V0.2-SPEC.md` — design + the A/B/C decision
- `/tmp/oprun-audit/codevisor-avc.out` — Path A/B/C verdict
- `/tmp/oprun-audit/codevisor-lanes.out` — **this revision's verdict** (lane taxonomy, cut Herdr, file-only)
- `/tmp/canary/CANARY-RESULTS.md` — two-vendor parallel headless run + witness probe
- `/tmp/canary3/HERDR-WITNESS-FINDINGS.md` — the 7-run Herdr measurement
- `/tmp/dura/arch-proof-report.txt` — detached systemd lane, exit-code witness, cgroup proof
- `/tmp/canary3/oprun_ledger.py` + `test_ledger.py` — fencing + circuit breaker, 10/10 green
- `~/orca-family-teardown.md`, `~/harness-routing-rubric.md`, `~/harness-adapter-patterns.md`,
  `~/orchestrator-landscape.md`

## 11. Open questions for the build session

1. Does `pi` (herdr integration v5) matter at all now that PTY lanes are cut? **Probably not** — `pi -p`
   is a CLI lane and needs no integration. Re-scope this question or drop it.
2. ~~Should `settle` auto-close panes?~~ **Resolved:** no pane work. `settle` writes the ledger and
   records evidence; teardown of detached units is `systemctl --user stop`.
3. Does the ledger need a `mission_id` for multi-mission repos, or one `state.json` per repo?

## 12. Interpretation note

The user asked that the skill "encapsulate everything." Rev 2 reads that as **the skill must be
self-contained and complete** — not that it should support both `state.json` and kanban. Codevisor
explicitly warned against a "both modes" facade, so rev 2 is file-only. If a two-store design was
actually intended, say so and this becomes a different document.

## 13. Why `state.json` and not `hermes kanban` — the honest version

Asked directly: *"why did we get rid of kanban, why is state.json better, and even if we allowed
kanban wouldn't it still not be better?"*

**The honest framing first: kanban is not a bad tool, and it is better than `state.json` at several
real things.** Pretending otherwise would be dishonest.

| Kanban genuinely wins | `state.json` genuinely wins |
|---|---|
| **Atomic claim across processes** — safe with several concurrent writers | One writer, no locking, **no concurrency bugs possible** |
| **Dependency gating with auto-promotion** of children | Dead simple, **greppable, diffable, git-visible** |
| **Durable audit trail** (`task_events`, `runs`) | Zero schema drift — it is just JSON you can read |
| **Dispatcher daemon auto-resumes lanes after a crash** | **No daemon** to leave running or to respawn a stuck worker |
| Per-task model pin, circuit breaker, watch/dashboard/notify — built in | Composes with `grep`/`jq` and every normal tool |
| Shared across profiles | **Writable from a chat shell with no env workaround** |

So why did v0.2 drop it? **Three specific reasons, and only one of them is about the tool:**

1. **Its main advantage solves a problem we designed away.** Atomic claims and locking exist for
   *concurrent writers*. oprun's model is **one conductor, explicitly owned, one mutating owner per
   worktree**. With a single writer there is nothing to lock. We would be paying kanban's complexity
   to solve a race we have structurally prevented.

2. **Its unique remaining claim — the daemon that auto-resumes after a crash — is the thing that
   caused the incident.** That same second scheduler produced the board with `blocked=5`, a ready card
   ~50 days old, and cards reading "awaiting next-day user approval". And **a daemon cannot tell
   `idle` from done either** — it does not solve the witness problem, which was the actual defect.

3. **`systemd-run` now supplies the durability kanban supplied**, without a scheduler holding state:
   detached units outside the session scope, real exit codes, journald audit. Durability moved to the
   layer that is *good* at durability (process lifetime), and the ledger stayed a file.

**Now the part that matters — would kanban still not be better even if we allowed it?**

**For v0.2's scope: no, it would still not be better, and it would be actively worse.** Not because
kanban is bad, but because **supporting both is the bug.** Codevisor's objection was never "kanban is
inferior" — it was *"the mode switch is the same bug with a config key: two code paths, two resume
stories, conductors guessing which mission they are in."* A conductor that must first work out *which
store is authoritative* is a conductor that will eventually write to one and read from the other, and
then someone will "fix" the mismatch by weakening a check. That is how you get your 09:48-frozen
`state.json` again, in a new form.

**But kanban would be better for a different product, and you should know the line.** The moment you
want:

- **unattended multi-lane fleets** that keep dispatching with no conductor turn,
- **automatic lane promotion** from dependency completion,
- or **several conductors / sessions** touching one mission,

…then kanban is genuinely the right substrate, because those are exactly the properties it has and a
file does not. That is not v0.2. That is **Path A — a promoted conductor**: a `kanban-orchestrator`
*profile* plus the dispatch daemon, running deliberately, instead of this chat.

So the decision is not "which store is superior." It is:

> **Do we want a chat-driven conductor on a file, or an unattended fleet on a board?**

v0.2 answers: chat-driven, on a file. If you later want the fleet, **switch all the way to kanban —
do not run both.** A deliberate one-way migration is fine; a hybrid is the trap.

*(One middle path exists and is worse than both: use kanban for its storage but never enable the
daemon. Then you have a SQLite board with none of the benefits and all of the ceremony — strictly
inferior to a JSON file.)*

## 14. Parallel lanes and nested work — does `state.json` still hold?

Asked directly: *"is `state.json` going to be fine if we don't run tasks sequentially — in some cases
it might be best to run tasks in parallel and in different worktrees, which might result in nested
work?"*

**Answer: yes — parallelism is not the problem. Writer count is.**

### 14.1 Parallelism is safe by construction

Kanban's headline advantage is **atomic claim across processes**, which matters when N independent
*processes* contend for work from a shared queue. oprun never has that shape: **claims are made by one
authority** (the conductor, or a single bounded `oprun advance`), so there is nothing to contend over.

What oprun does have is N lanes *executing* concurrently and N settles arriving. That is handled by
two properties already in the reference implementation:

1. **Lanes are independent records** — a dict keyed by lane id, with no shared mutable structure
   between lanes. Two lanes cannot corrupt each other's state.
2. **Writes are serialized with `flock`, and the file is re-read *under* the lock** before every
   mutation. The re-read is the load-bearing part: without it, concurrent writers perform lost updates
   (each writes a copy it read before its peer's write landed). With it, writes are
   read-modify-write-atomic-rename, and a peer's commit is never clobbered.

**Measured** (6 processes × 25 concurrent dispatch/settle cycles on one ledger, all truly parallel):

```
6 processes x 25 settle cycles each, all concurrent, in 0.46s
lanes in file        : ['l1'..'l6']        LOST UPDATES: none
l1..l6: status=completed  accepted=1  dup_rejected=24  consistent=True
file parses as valid JSON: True
VERDICT: PASS — concurrent writers are safe
```

Zero lost updates; the fencing layer correctly rejected the 24 duplicate settles per lane, and every
lane reached a terminal state. Git worktree isolation was independently confirmed in the parallel
run: no lane touched another lane's files.

### 14.2 What parallelism *does* require

- **A per-lane test selector.** Measured in the first canary: lanes in perfectly isolated worktrees
  still fail if a lane's acceptance command runs a suite containing *another* lane's tests. Every lane
  carries its own explicit `test_cmd`.
- **A dependency gate, not a convention.** A lane with `depends_on` must refuse to dispatch until its
  parents are `completed` (implemented and tested in the reference). Prose like "wait for alpha" is
  not a mechanism.
- **Evidence that survives uncommitted work** — see §15.1; this is the real gap the parallel run exposed.
- **A concurrency cap.** Parallelism is bounded by machine capacity, not by the ledger. Track a
  `max_parallel` in the ledger and refuse dispatch beyond it. (v0.1's lesson in the other direction:
  unbounded spawn loops "eat tokens overnight".)

### 14.3 Nested work — allowed, bounded

Nested work means a lane that itself spawns lanes. The ledger handles it with two additions:

- **`parent_dispatch`** on every lane, so lineage is explicit and evidence can be walked back to its
  origin. A nested lane's completion is fenced by its *own* dispatch id, exactly like a top-level one.
- **A hard depth limit of 1** (root = 0), raising `nested_worker_depth_exceeded` beyond it — the same
  bound Orca uses. Unbounded recursion is how you get a thousand lanes and a bill.

**Do not let a lane write the ledger directly.** A lane reports by writing its sidecar; only the
conductor / advance runner settles. That keeps the single-writer invariant true even with nested
fan-out, and it is why nested work does not change the answer to "is one JSON file enough?".

### 14.4 When this stops being true

The file is the right store while **claims are centralized**. It stops being right the moment two
*independent* schedulers must claim work from one shared pool — that is a queue, and a queue wants a
database. If that ever becomes the requirement, that is Path A (§13), and it is a deliberate one-way
migration, not a hybrid.

## 15. Findings from the parallel proof (fix these in the build)

Running three parallel lanes to a settled ledger exposed three defects in the reference
implementation. All three are cheap to fix and would have been invisible on a happy single-lane path.

### 15.1 `evidence.commit` is meaningless when the lane doesn't commit
Workers were correctly instructed not to commit, so `git rev-parse HEAD` returned the **base** commit
for all three lanes — identical SHAs — making the evidence useless as proof of what the lane actually
did. **Fix:** when there is no lane commit, record `uncommitted: true` plus a **content hash** of the
lane's diff (or per-file hashes). Never record a SHA that does not identify the lane's work.

### 15.2 Naive model comparison false-positives
`model_requested='cursor-grok-4.6'` vs `model_reported='Cursor Grok 4.6'` — a raw string compare flags
*every* lane as a substitution mismatch, so the guard gets muted and stops protecting anything.
**Fix:** compare on a **normalized** form (casefold, strip separators), keep and print both raw
strings, and only raise `needs_review` on a normalized mismatch.

### 15.3 Harness identity must come from the registry, not the worker
One lane self-reported `harness: "cursor"`, the others `harness: "cursor-agent"`, for the same CLI.
**Fix:** the ledger's harness field is authoritative and set at dispatch; the worker's self-report is
recorded alongside as `harness_reported` but never trusted for routing or evidence.

**Corollary worth stating plainly:** the three defects were in *our* code, not in the harnesses. Three
parallel agents did exactly what they were asked and their work was correct. The failure mode to worry
about in parallel runs is not the agents — it is a controller that stops checking carefully because
everything looks fine.
