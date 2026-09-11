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
| Bounded one-slice patch + its tests | `codex` | `exec --json` returns `turn.completed`; `-s workspace-write` lets it edit |
| Large multi-file refactor; heavy / batched authoring | `cursor-agent` | `-p --yolo --trust`; longest-context editing; auto-routes its own model |
| Light implementation + tests, Hermes-native tools | `hermes` | exit code + session id; needs `HOME=/home/logani` |
| Long looped work driven by a brief | `pi` | `-p`; absolute binary path, not on `PATH` |
| Repo recon / mapping (cheap re-runs) | `opencode` | see the warning below |
| Long local loop on a GLM-family model | `droid` | `exec -o json --auto <level>`; exit code is the verdict |
| Decompose, route, inspect evidence, talk to the user | **the conductor** | never implements; holds no product edits |
| Architecture / PASS–FAIL judgement | a *different* family than the author | not a writer |

One rule that is not yours to override, because the registry measures it:

1. **`opencode` cannot be trusted to report completion.** It is known to drop its terminal event
   (`step_finish`; issues #26855 and #31435), so `witness_strength` is `"none"`: a run can finish
   correctly and still look unfinished. Route it only where a re-run is cheap, and accept it on
   the OS exit code plus a sidecar artifact — never on its stdout alone.

**`subtype` alone is never success.** A result event carrying `subtype:"success"` was measured
*alongside* `is_error:true` and exit 1, which is why every harness that emits a result event
(`cursor-agent`, `droid`, `pi`) is parsed on the exit code and `is_error` agreeing — never on
`subtype`.

## Accepted spellings

`cursor` → `cursor-agent`. The ledger always records the **canonical**
registry id, so two spellings of one CLI can never split a harness's identity. Any other id is a
hard error that names the known ids.

## Model pins

Every lane records the model **requested** and the model the worker **reported**, compared
normalised (casefold, separators dropped) so `"Cursor Grok 4.6"` and `"cursor-grok-4.6"` do not
read as a substitution. A genuine mismatch is `needs_review` — never a silent pass.

**A pin is only compared where it can be checked.** `Harness.pin_verifiable` says whether anything
the CLI itself emits names the model that ran. Where it cannot — `codex` is the measured case
(`codex exec --json` emits `thread.started`/`turn.started`/`item.completed`/`turn.completed` and no
model id at all) — a reported mismatch is recorded as an `identity_warning` and the lane is decided
on its real evidence, because a difference there may be the CLI's naming rather than a substitution.
Only a measurement may lower this flag; the default is `True`.

Two rules do **not** bend to that flag: a pinned lane whose sidecar reports **no** model is
`needs_review` (the pin cannot be shown to hold, and this is checked *before* the escape), and a
lane with **no** pin has nothing to contradict, so whatever it reports is recorded as-is.

The **harness** is not a parking rule at all. oprun chooses the binary from the registry, so a
worker's opinion about which CLI it is carries no information: a `droid` lane whose sidecar
self-reported `cursor-agent` was accepted on its real evidence, with the disagreement recorded as an
`identity_warning`. The registry is authoritative.
