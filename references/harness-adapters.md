# Harness adapters — operator reference

Per-CLI recipes for **unattended** runs, and the evidence that says a run finished. Measured
on this box 2026-09-11. Registry: `scripts/harnesses.py`; routing is only as safe as these facts.

## The rule that matters

**`subtype` alone is never success. Exit code plus `is_error` are authoritative.**

A result event was measured carrying `subtype:"success"` *while* its `is_error` was `true` and the
process exited 1. A controller that greps for `"success"` accepts a failed run. Always:

1. process **exit code** (0 = success) — the primary signal;
2. `is_error` in the result event — the corroborating signal;
3. the **sidecar artifact** the lane wrote — what acceptance actually stands on.

A terminal event without an exit code is a hint. An exit code without a terminal event is still
evidence. Prose from the agent is neither.

## cursor-agent

    cursor-agent -p --output-format json --yolo --trust

- **Success looks like:** one `{"type":"result", ...}` line whose `is_error` is `false`, plus
  exit 0. `subtype` reads `success` or `error` and corroborates.
- **Exit code:** authoritative, in step with `is_error`.
- **Best at:** heavy and batched authoring across many files.
- **Known failure mode:** `-p` runs with **full write + shell access** — `--yolo` skips the
  approval prompt and `--trust` skips the workspace-trust prompt. The worktree is the only
  boundary; a bad recipe edits the wrong tree. It also **auto-routes its own model**, so the
  model it reports is the model that ran: record it and compare against the pin. A mismatch
  means the lane needs review, not a silent pass.

## codex

    codex exec --json -s workspace-write

- **Success looks like:** a `turn.completed` event; failure is `turn.failed`.
- **Exit code:** authoritative; a non-zero exit with no terminal event is a crash, not a stall.
- **Best at:** one bounded slice of implementation plus its tests.
- **Known failure mode:** there is **no `--max-turns` flag** — nothing caps the run from
  inside. **Wrap it in an external timeout** and escalate when the deadline passes without
  `turn.completed`. An uncapped run that never terminates looks identical to a long think.

## droid

    droid exec -o json --auto <low|medium|high>

- **Success looks like:** a `{"type":"result", ...}` event.
- **Exit code:** the **documented authoritative signal** — treat it as the verdict and the
  result event as corroboration. Pick the `--auto` level deliberately; it is what makes the
  run unattended.
- **Best at:** long local loops, often on a GLM-family model.
- **Known failure mode:** omit `--auto` and it prompts for approval and sits idle. Idle is not
  progress and must never be waited on.

## hermes

    HOME=/home/logani hermes -p <profile> chat --query-file <brief>

- **Success looks like:** exit 0, plus the **session id** it prints — the handle for
  reattaching, relaying, or inspecting the run afterwards.
- **Exit code:** authoritative. With no machine-readable output format, the exit code carries
  more weight here than for any harness that emits a result event.
- **Best at:** work that needs MCP servers, browser tools, or the skills library.
- **Known failure mode:** run it without `HOME=/home/logani` and the profile resolves
  somewhere else (wrong config, wrong skills). The brief must be a file — `--query-file` —
  so an unattended lane is one process per unit, with nothing typed into a prompt.

## pi

    HOME=/home/logani /home/logani/.hermes/node/bin/pi -p

- **Success looks like:** exit 0, plus a sidecar artifact — with no terminal event, the
  artifact is what acceptance rests on.
- **Exit code:** authoritative and the only terminator.
- **Best at:** long-horizon looped work driven by a brief.
- **Known failure mode:** it is **not on `PATH`** — the absolute path is required, and a
  recipe that spells it `pi` fails to launch. Like hermes, it needs `HOME=/home/logani`.

## opencode

    opencode run --format json

- **Success looks like:** a `step_finish` event. **This is the weak link.**
- **Exit code:** authoritative — the only signal that survives the missing terminator.
- **Best at:** repo recon and mapping, where a dropped terminator costs a re-run, not a
  wrong acceptance.
- **Known failure mode:** it is **known to drop its terminator** (opencode #26855, #31435).
  A run can finish correctly and still never print `step_finish`. **Never trust its stdout
  alone:** accept on exit code plus a sidecar artifact, and read a missing terminator as an
  unreliable witness rather than a failure.

## Routing constraints

One fact changes routing, and it is encoded in the registry rather than left to judgement:

1. **`opencode` cannot be trusted to report completion.** `witness_strength == "none"`: its
   terminator is dropped, so nothing in its stdout is a verdict. Route it only where a
   re-run is cheap, and accept it on exit code plus a sidecar artifact.

Everything else in the registry carries `witness_strength == "strong"`: process exit, a
machine-readable terminal event, and a sidecar artifact the controller can re-read.
