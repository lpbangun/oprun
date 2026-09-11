# OPRUN-V0.2-BENCH-1 — frozen acceptance benchmark

**Frozen by Codevisor (`grok-4.6` / `xai-oauth` / medium), session `20260911_211952_723103`, 2026-09-11.**
**No implementer may edit this rubric.** A later freeze is a new Codevisor consult.

**Path reconciliation (session `20260911_212303_c3f6e7`, same model):** the original ruling wrote
`$REPO/oprun/scripts/oprun.py`. Confirmed verdict: **keep the repo-root layout** — the leading `oprun/`
in the contract's tree is the *label for the repo*, not a nested directory. Every criterion path
follows the same rule (strip the label): `$REPO/scripts/…`, `$REPO/tests/…`, `$REPO/README.md`.
**Do not create `$REPO/oprun/`.** No criterion requires it.

> Ship gate: **SCORE >= 9.0 AND K=1 AND FLOOR=17/17**

Notation
  REPO = ~/projects/oprun-worktrees/v0.2
  CLI  = python3 $REPO/scripts/oprun.py      (repo root; no nested package dir)
  LED  = $SCRATCH/.tmp/oprun/state.json   (scratch git repo; never the product tree)
  WT   = isolated git worktrees under $SCRATCH

Partial credit is 0 unless a criterion explicitly allows 0.5. "Looks done" is not a pass.

---

## 1. Scoring method

K = 0 if any keep-out in §4 fires, else 1.
FLOOR = count of AC 1–17 whose verification in §2 passed (each 0 or 1).
p_i in {0, 0.5, 1} per criterion; w_i as listed (sum w = 10).
RAW = sum w_i * p_i

```
If K=0:                 SCORE = 0
Elif FLOOR < 17:        SCORE = min(6.0, 6.0 * FLOOR/17)
Elif any MUST p_i = 0:  SCORE = min(7.0, RAW)
Else:                   SCORE = RAW
```

Ship iff SCORE >= 9.0. That is impossible unless K=1, FLOOR=17/17, and every MUST criterion is 1.
Quality cannot pad a missing live proof.

**What a half-build earns (do not argue this):**

```
ledger unit tests only, reference ported          FLOOR ~4/17  -> ~1.4
+ probe unit tests, no live harness               FLOOR ~7/17  -> ~2.5
+ one harness happy-path, no negatives            FLOOR ~10/17 -> ~3.5
+ two-vendor parallel, no advance, no integrity   FLOOR <17, MUST miss -> <= 6.0
FLOOR 17/17 but one MUST at 0                     cap 7.0
FLOOR 17/17, MUST all 1, empty README (C10=0)     8.8  FAIL GATE
FLOOR 17/17, MUST all 1, C10=1, C9=0.5            9.5  SHIP
everything                                        10.0
```

Never: average "feels like 9", never drop a test, never substitute a mock for a named live command.

---

## 2. Weighted criteria (MUST marked)

Each p_i=1 only if EVERY command exits 0 and EVERY observable holds.

### C1  w=1.2  **MUST**  Witness / probe  (AC 2,3,6 + canary subtype trap)
- **Static:** `rg -n "openai|anthropic|litellm|requests\.(get|post)|subprocess.*hermes|chat\.completions" $REPO/scripts/probe.py` → no matches
- **Unit:** `python3 -m pytest $REPO/tests/test_probe.py -v`
  required cases (fail the file if any missing):
  accept only when sidecar exists AND `task_id`+`dispatch_id` == ledger CURRENT AND
  `sidecar.status==success` AND controller `test_cmd` exits 0 AND every evidence path exists;
  reject: no sidecar; stale dispatch_id; sibling-lane dispatch_id; sidecar success + test_cmd rc!=0;
  sidecar failed + tests green (needs_review, never done); subtype success + is_error true + rc 1
  (Claude 403 shape); timeout with no artifact → stalled|needs_input not pending-forever;
  `systemctl is-active=active` with no sidecar → not done; inactive unit + valid sidecar + tests 0 → done
- **Live:** `$CLI probe <lane>` on a real `cursor-agent -p` run that wrote a sidecar; verdict from probe stdout, not the agent.

### C2  w=1.2  **MUST**  Ledger: transitions, fencing, breaker, flock, nest, deps  (AC 4,5,14,15)
- `python3 -m pytest $REPO/tests/test_ledger.py -v`
- Must include (port `/tmp/canary3/test_ledger.py`, do not delete cases): legal/illegal `apply`;
  stale dispatch rejected; duplicate rejected; cross-lane token rejected; trip at 3 consecutive
  failures → blocked; blocked cannot retry/dispatch; success resets streak; reload preserves fencing.
- Added: `depends_on` parent not completed → dispatch raises; depth>1 → `nested_worker_depth_exceeded`;
  `parent_dispatch` set on every lane; workers have no `Ledger.write` API imported from worker-brief;
  `max_parallel` exceeded → refuse.
- **Concurrency** (port `/tmp/oprun5/concurrency-proof.py` against shipped `ledger.py`):
  `python3 $REPO/tests/test_concurrency.py`
  Expect: 6 processes × 25 dispatch/settle, LOST UPDATES none, every lane terminal, file valid JSON,
  duplicates fenced. Exit 1 on any lost update.

### C3  w=1.1  **MUST**  Two-vendor parallel isolation  (AC 1,13 + per-lane test_cmd)
- Scratch repo, frozen tests written BEFORE dispatch, workers forbidden to touch `tests/`.
```
$CLI init $SCRATCH --mission "bench-c3"
$CLI dispatch alpha --harness cursor-agent --model <pin> --worktree $WT/alpha --test-cmd "..."
$CLI dispatch beta  --harness openai-codex --model gpt-5.6-sol --worktree $WT/beta --test-cmd "..."
```
- Both live; then controller re-runs EACH lane's own `test_cmd` (not the sibling suite).
- **Observables:** two vendor names in LED; worktrees distinct; `git diff --stat` shows no cross-tree
  paths; sha256 of `tests/` unchanged vs pre-dispatch; each accepted on its `test_cmd` rc=0; LED has no lost lane.

### C4  w=1.0  **MUST**  Detached launch + teardown  (AC 7)
- After dispatch, for each unit:
  `systemctl --user show <unit> -p FragmentPath -p ExecMainStatus -p Result -p ControlGroup`
  - `ControlGroup` matches `user@1000.service/app.slice/` and does **NOT** match `session-*.scope`
  - compare to `cat /proc/self/cgroup` of the conductor
- `$CLI settle ...` then `systemctl --user list-units 'oprun-*' --state=active` → zero owned live units
- `launch.py` contains `systemd-run --user`; `rg "nohup|tmux|herdr|screen " $REPO/scripts/launch.py` → no matches

### C5  w=1.1  **MUST**  Unattended advance  (AC 8,16)
- Pre-approve envelope. Dispatch N>=2 independent lanes. Launch advance detached:
```
systemd-run --user --unit oprun-bench-advance --wait --pipe -- \
  python3 $REPO/scripts/advance.py --ledger $LED --timeout 240
```
- Conductor session sends **ZERO** further verbs until reattach.
- **Observables** from `journalctl --user -u oprun-bench-advance`, not the agents: each lane ACCEPTED or
  parked blocked/needs_review; LED mission complete or `nextAction none` if all accepted; failing lane
  (injected) is blocked/needs_review and is NOT retried past breaker.
- `advance.py` is a bounded process that exits; `rg "while True"` without timeout/max-steps fails this criterion.

### C6  w=1.3  **MUST**  Evidence integrity  (AC 17 + §15 three defects)
- **Uncommitted lane:** `jq '.lanes[].evidence | {commit,uncommitted,hashes}' $LED`
  `uncommitted==true`; `hashes` non-empty; `commit` is NOT the shared base SHA of all lanes
  (the /tmp/oprun5 identical `38883f2` failure).
- **Model:** requested vs reported compared normalized (casefold, strip `[-_ .]`); raw strings both stored;
  only normalized mismatch → needs_review.
  Fixture: requested=`cursor-grok-4.6` reported=`"Cursor Grok 4.6"` MUST accept;
  requested=`gpt-5.6-sol` reported=`grok-4.6` MUST needs_review.
- **Harness:** `ledger.harness` == registry id set at dispatch; sidecar harness lands in `harness_reported`
  only; two spellings `cursor` vs `cursor-agent` do not split identity.
- `python3 -m pytest $REPO/tests/test_evidence.py -v` covering the three fixtures.

### C7  w=0.8  **MUST**  Approval envelope  (AC 11,12)
- `$CLI init $SCRATCH --mission m --approve commit,push,merge`
- `jq .approvals $LED` shows granted true for those three; `deploy,publish,destructive` granted false.
- With envelope: `settle --accept` performs commit/push/merge with **no prompt** (scripted, stdin closed);
  SHAs recorded in evidence.
- `$CLI status` prints the envelope.
- Envelope absent: the same settle asks **exactly once** (one line), not a loop.
- `push` granted: force-push, tag, release, history rewrite, branch delete, other-repo all refused
  without fresh grant.
- `$CLI init $SCRATCH --mission m` (no `--approve`) then `jq .approvals.destructive.granted == false`
- Cannot pass by skipping the live git commit/push; mock is p_i=0.

### C8  w=0.8  **MUST**  Resume + stall escalation  (AC 6,9)
- Dispatch a live lane. `kill -9` the conductor pid. New shell:
  `$CLI status --json` ; `$CLI probe <lane>`
  LED unchanged except probe's allowed fields; no lost `dispatch_id`.
- Lane with no sidecar past `--timeout`: probe in `{needs_input,stalled}`; must not remain pending;
  must not auto-wait forever.

### C9  w=0.8  Tests green, none weakened  (AC 10)
- `python3 -m pytest $REPO/tests/ -v` exit 0
- git log / diff of `tests/`: no assertion deleted, no `pytest.mark.skip` added to make green,
  no timeout raised to hide stalls.
- Port check: every test name in `/tmp/canary3/test_ledger.py` has a successor in `tests/test_ledger.py`.

### C10 w=0.7  Skill / CLI / docs / registry
- Files exist exactly as the proposal tree (`dispatch.py` optional keep from v0.1; **`herdr.md` MUST NOT exist**).
- `wc -l $REPO/README.md >= 40` and contains a copy-pasteable init/dispatch/probe/settle/status/approve/advance quickstart.
- `$CLI --help` lists: `init dispatch probe settle status approve`
- probe stdout one of: `done pending needs_input stalled failed`
- `SKILL.md`: conductor never implements; bans daemon, kanban, PTY/Herdr, model-in-probe; settlement = evidence.
- `references/routing.md` is a user-overridable table; `harnesses.py` registry includes the §8 recipes;
  **claude without `--model` is not routable**.
- 0.5 allowed only if CLI+skill+registry pass and README is complete but CHANGELOG is thin. **Empty README = 0.**

### AC mapping (FLOOR bits; each is the matching C-command above, not a speech)
```
 1 C3   2 C1   3 C1   4 C2   5 C2   6 C8   7 C4   8 C5
 9 C8  10 C9  11 C7  12 C7  13 C3  14 C2  15 C2  16 C5  17 C6
```

---

## 3. Admissible evidence

**Counts:**
- command exit code + full stdout/stderr saved under `$SCRATCH/bench/`
- jq/python parse of `$LED`
- `journalctl --user -u <unit> --no-pager`
- `systemctl --user show` / `cat /proc/<pid>/cgroup`
- `sha256sum` of `tests/` before vs after
- `git -C $WT/... status/diff`
- pytest JUnit or `-v` log

**Does not count:**
- agent self-report, `sidecar.status` alone, conductor narrative, "terminal looked done"
- `herdr agent_status` / `wait --status done` (measured 1/7)
- `systemctl is-active` as lane completion
- `subtype` without `is_error`+rc
- screenshots, prior /tmp/oprun5 runs reused as v0.2 evidence (those proved the design, not this tree)
- skipped tests, raised timeouts, commented assertions
- mocks of `systemd-run`, `flock`, or harness CLIs for MUST live items

**Reuse rule:** `/tmp/canary3` and `/tmp/oprun5` are **oracles for expected behavior**. Shipping
evidence must be re-run against `oprun/scripts/*.py` in this worktree.

---

## 4. Keep-outs (K=0, SCORE=0, do not ship — no averaging)

- Daemon or second scheduler; `hermes kanban` in any form (store, mode switch, dual-write, "just for lock").
- Any model in `probe`/`settle`/`advance` completion path (imports, API, "cheap triage").
- PTY/Herdr lane, `herdr.md`, pane close as settle, `herdr wait` as witness.
- Weakened test or check to go green.
- Implicit approval widening; destructive or force-push defaultable; invented human gates;
  per-commit ask-loop that undoes the envelope.
- Conductor product edits; two mutating owners on one worktree; lane process writing the ledger.
- Probe treating systemd lifetime as lane state.
- `evidence.commit` = base HEAD on an uncommitted lane.
- Naive model-string compare that false-positives `"cursor-grok-4.6"` vs `"Cursor Grok 4.6"` (guard will be muted).
- Harness id taken from the worker.
- Launch in the chat session cgroup (`nohup`/`tmux`/`herdr` as the detach story).
- Empty README (v0.1 failure, repeated).
- Scope beyond one JSON ledger + one CLI + one skill.

---

## Trap

The `/tmp/oprun5` parallel run already "looked green" with identical base SHAs, split harness names, and
a model-string false positive. A build that replays that happy path without the three §15 fixtures will
feel like 10 and is a **0 on C6, which caps the score at 7**. The failure mode is the controller relaxing
checks because the agents did the work. Do not let `advance` become a daemon to paper over
resume-on-reattach. Do not add kanban when `flock` already held 6×25.
