# oprun v0.2 — worker set for the implementation

Who does what, on which model, verified live. Every designation is explicit and user-overridable;
nothing is inferred at dispatch time. This is the routing table from §7.3 of the proposal, instantiated
for this specific build.

## Ground rules

1. **The gate is now frozen and non-negotiable:** `docs/BENCHMARK-v0.2.md` (OPRUN-V0.2-BENCH-1,
   Codevisor `grok-4.6`/`xai-oauth`, session `20260911_211952_723103`). Ship iff
   **`SCORE >= 9.0` AND `K=1` AND `FLOOR=17/17`**. No implementer may edit the rubric.

   How the score actually moves — so nobody optimises the wrong thing:

   ```
   K=0 (any keep-out fires)            -> 0        (no averaging, no partial)
   FLOOR < 17 (any AC unproven)        -> min(6.0, 6.0*FLOOR/17)
   FLOOR 17 but any MUST criterion 0   -> cap 7.0  (FAILS the gate)
   FLOOR 17, MUST all 1, empty README  -> 8.8      (FAILS the gate)
   FLOOR 17, MUST all 1, C9=0.5        -> 9.5      (ships)
   ```

   All nine MUST criteria must be 1: **C1** witness/probe · **C2** ledger+fencing+breaker+flock ·
   **C3** two-vendor parallel isolation · **C4** detached launch+teardown · **C5** unattended advance ·
   **C6** evidence integrity · **C7** approval envelope · **C8** resume+stall escalation.
   **Quality cannot pad a missing live proof** — unit tests alone cap out around 2.5.

2. **Evidence must be re-run against this tree.** `/tmp/canary3` and `/tmp/oprun5` are *oracles for
   expected behaviour*, never shipping evidence. Their prior "green" run is explicitly the failure mode
   the rubric guards: identical base SHAs, split harness names, and a model-string false positive.
3. **Independence is structural.** No unit is verified by the model family that wrote it. Recorded per
   lane as `model_requested` + `model_reported`; a **normalized** mismatch ⇒ `needs_review`.
4. **One mutating owner per file set.** Parallel lanes are **file-disjoint by construction** — see the
   ownership map. Two writers never share a worktree.
5. **Witness per lane:** sidecar artifact + OS exit code + the controller re-runs that lane's own
   `test_cmd`. A worker's summary is never acceptance.
6. **Launch detached:** every lane via `systemd-run --user` (exit codes, journald, survives disconnect).
7. **No self-graded work.** If the implementer's model family is `deepseek`, a `deepseek` lane may not
   be the verifier for it.

## Verified routes (all tested live on this box, 2026-09-11)

| Route | Provider | Model | Verified | Best at |
|---|---|---|---|---|
| **A. Light/medium implementation** | `commandcode` | `deepseek/deepseek-v4.1-flash` | ✅ returned `OK` | bounded Python, tests, careful small modules — this session's own model |
| **B. Heavy / batched, multi-file** | `cursor-agent` CLI | auto-routes (**record resolved**) | ✅ headless, exit 0, sidecar | large diffs, long autonomous edits |
| **C. Independent technical verification** | `openai-codex` | `gpt-5.6-sol` | ✅ returned `CODEX_OK` | *different family* from A — the independent check |
| **D. Benchmark author + final judge** | `xai-oauth` | `grok-4.6` (codevisor profile) | ✅ this consult | freezing the bar, PASS/FAIL judgement |
| **E. Escalation / heavy reasoning, same family as A** | `commandcode` | `deepseek/deepseek-v4-pro` or `Zai/GLM-5.1` | ✅ available | genuinely hard subproblems |
| **F. Recon / mapping (in-context subagent)** | inherits conductor (`commandcode`) | `deepseek/deepseek-v4.1-flash` | ✅ | reading + analysis only; **never** acceptance |

Alternative verifiers if C is unavailable: `xai-oauth/grok-4.5`, `opencode-go/glm-5.3` — anything
**not** the author's family.

## Phase plan

```
Phase 0  BENCHMARK FREEZE        Codevisor (D)          → docs/BENCHMARK-v0.2.md        [in flight]
Phase 1  CORE LEDGER (serial)    A                      → ledger.py + tests
         └ everything depends on this; do not parallelise
Phase 2  PARALLEL SLICES                                                    ← after Phase 1
         ├ slice-1 probe.py         A   (file-disjoint)
         ├ slice-2 advance.py+launch.py  A or B (file-disjoint)
         └ slice-3 harnesses.py     A   (file-disjoint)
Phase 3  CLI + DOCS
         ├ oprun.py                A
         └ SKILL.md / README / references   A or B
Phase 4  VERIFICATION (independent)
         ├ technical verify        C      → per-unit correctness + criteria 1-17
         ├ adversarial tests       C      → try to break fencing/breaker/concurrency
         └ final judgement         D      → score against the frozen benchmark
Phase 5  HARDEN
         └ fix findings, re-run Phase 4 (loop until ≥9/10 or a block is reported)
```

**Phase 1 is deliberately serialised.** `probe.py`, `advance.py` and the CLI all import `ledger.py`;
building them against an unstable core guarantees rework. Everything after it is parallel.

## Ownership map — file-disjoint lanes

**Layout is repo-root** (Codevisor-confirmed, session `20260911_212303_c3f6e7`): `scripts/`, `tests/`,
`references/`, `templates/` sit beside the existing `scripts/dispatch.py`. **Do not create a nested
`oprun/` directory.**

| Lane | Owns (exclusive) | May not touch |
|---|---|---|
| `core-ledger` | `scripts/ledger.py`, `tests/test_ledger.py` | everything else |
| `slice-probe` | `scripts/probe.py`, `tests/test_probe.py` | `ledger.py`, others |
| `slice-advance` | `scripts/advance.py`, `scripts/launch.py`, `tests/test_launch.py` | `ledger.py`, others |
| `slice-harnesses` | `scripts/harnesses.py`, `references/harness-adapters.md` | `ledger.py`, others |
| `cli-docs` | `scripts/oprun.py`, `SKILL.md`, `README.md`, `CHANGELOG.md`, `references/{routing,hosting}.md`, `templates/*` | `ledger.py`, others |

Shared files (`scripts/__init__.py`, `pyproject.toml`, CI config) have a **single** owner: `cli-docs`.

## Mandatory content per worker brief

Every brief must carry these, or the lane is misconfigured:

- **Dispatch id** and the exact sidecar path: `<worktree>/.oprun/result.<dispatch_id>.json`
- **The completion schema** (the 11 keys) and the instruction to write it atomically, then stop
- **The lane's own `test_cmd`** — never "run the suite" (canary finding: a lane in a perfectly isolated
  worktree still fails if its acceptance command includes a sibling lane's tests)
- **The three inherited fixes** from proposal §15: no base SHA as lane evidence; normalize model
  comparison; harness identity comes from the registry, not the worker
- **Forbidden:** committing, pushing, spawning subagents, loading orchestration skills, editing outside
  its owned files
- **Reference implementations to port, not reinvent:**
  `/tmp/canary3/oprun_ledger.py` + `test_ledger.py` (10/10 green),
  `/tmp/oprun5/oprun_ledger.py` + `advance.py` (flock, parallel-safe)

## What the worker set must NOT become

- **No kanban, no daemon, no second scheduler.** `state.json` is the only source of truth.
- **No model in the completion path.** `probe` is deterministic; no LLM verdicts on "done".
- **No PTY/Herdr lane.**
- **No subagent as an acceptance authority** — subagents do recon and analysis only.
- **No weakening of a test or check to reach the score.** A benchmark met by relaxing a criterion is a
  failed build, and Codevisor's judgement is the backstop for exactly that.

## Dogfood opportunity (optional, after Phase 3)

Once `ledger.py` + `advance.py` exist, run **Phase 5 itself** through oprun — the build then proves the
design on its own hardening pass. If oprun cannot drive its own build, that is a defect in oprun, and
worth learning before release.

## Override

These designations are a starting point, not a cage. Say "use X for Y" and the table changes; nothing
here is hardwired, and no substitution happens silently — every lane records the model it was asked
for and the model it reported.
