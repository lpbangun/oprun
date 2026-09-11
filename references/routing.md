# Routing — unit → harness (yours to override)

**This table is the user's, not the code's.** The registry (`scripts/harnesses.py`) says what each
id *can* do — its unattended recipe and how its finish is witnessed. It does not say which unit
gets which harness. Override freely; the conductor picks a row per unit and records what it
actually used. Nothing here is hardwired and nothing is inferred silently.

## The distinction criterion

Not capability, and not "who the human is watching". One question:

> **Does the worker exit, and do we get an OS exit code plus (usually) a structured terminal event?**

That is what separates a lane that can be accepted from a run that merely looks idle.

## Default table

| Unit of work | Default harness (registry id) | Why |
|---|---|---|
| Large multi-file refactor | `claude` | strongest long-context editing — **needs `--model`** |
| Bounded one-slice patch + its tests | `codex` | `exec --json` returns `turn.completed`; `-s workspace-write` lets it edit |
| Heavy / batched authoring across many files | `cursor-agent` | `-p --yolo --trust`; auto-routes its own model |
| Light implementation + tests, Hermes-native tools | `hermes` | exit code + session id; needs `HOME=/home/logani` |
| Long looped work driven by a brief | `pi` | `-p`; absolute binary path, not on `PATH` |
| Repo recon / mapping (cheap re-runs) | `opencode` | see the warning below |
| Long local loop on a GLM-family model | `droid` | `exec -o json --auto <level>`; exit code is the verdict |
| Decompose, route, inspect evidence, talk to the user | **the conductor** | never implements; holds no product edits |
| Architecture / PASS–FAIL judgement | a *different* family than the author | not a writer |

Two rules that are not yours to override, because the registry measures them:

1. **`claude` is not routable without an explicit `--model`.** Its default model 403s on this box,
   so `routable("claude")` is `False` and `oprun dispatch --harness claude` is refused at dispatch
   time rather than launched to fail. Supply the pin and it becomes routable.
   Also: `subtype:"success"` was measured *alongside* `is_error:true` and exit 1 — never accept
   this harness on `subtype`.
2. **`opencode` cannot be trusted to report completion.** It is known to drop its terminal event
   (`step_finish`; issues #26855 and #31435), so `witness_strength` is `"none"`: a run can finish
   correctly and still look unfinished. Route it only where a re-run is cheap, and accept it on
   the OS exit code plus a sidecar artifact — never on its stdout alone.

## Accepted spellings

`cursor` → `cursor-agent` and `claude-code` → `claude`. The ledger always records the **canonical**
registry id, so two spellings of one CLI can never split a harness's identity. Any other id is a
hard error that names the known ids.

## Model pins

Every lane records the model **requested** and the model the worker **reported**, compared
normalised (casefold, separators dropped) so `"Cursor Grok 4.6"` and `"cursor-grok-4.6"` do not
read as a substitution. A genuine mismatch is `needs_review` — never a silent pass.
