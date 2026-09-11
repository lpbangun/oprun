# oprun

One JSON ledger, one CLI, one skill. A conductor routes each unit of work to a coding-agent CLI,
launches it **detached**, and accepts it only on evidence that lane produced.

Three facts decide everything else:

- **The conductor never implements.** It routes, probes, settles, and asks the user only for the
  decisions the approval envelope does not already cover.
- **A lane is accepted on evidence it produced** — its sidecar for the ledger's *current*
  `dispatch_id`, plus the controller's own re-run of that lane's own `test_cmd`. Never on what an
  agent said, and never on what a terminal looked like.
- **The witness is deterministic.** `probe` is pure: no model, no network, no daemon. A dead unit
  with a valid sidecar can be `done`; a live unit with no sidecar is not.

## Quickstart

Copy-paste. `$CLI` is the only entrypoint; everything else is a file.

```bash
CLI="$HOME/projects/oprun/scripts/oprun.py"     # or a checkout's ./scripts/oprun.py

# 1. init — create the ledger + mission dir and pre-approve the envelope. Launches nothing.
python3 "$CLI" init "$PWD" --mission "ship the parser rewrite" --approve commit,push,merge

# 2. dispatch — register + launch ONE lane detached. Prints the dispatch_id.
python3 "$CLI" dispatch parser \
  --harness cursor-agent \
  --model cursor-grok-4.6 \
  --worktree "$HOME/projects/myproj-worktrees/parser" \
  --test-cmd "python3 -m pytest tests/test_parser.py -q" \
  --prompt "Rewrite the tokenizer. Keep the public API. Do not touch tests/."

# 3. probe — one deterministic verdict word: no model involved.
python3 "$CLI" probe parser --wait --timeout 900
#   done | pending | needs_input | stalled | failed        (--json for the full document)

# 4. settle — accept on evidence; commit/push/merge only inside the envelope.
python3 "$CLI" settle parser --accept
python3 "$CLI" settle parser --needs-review "green tests but the diff touches unrelated files"

# 5. status — lanes, counts, nextAction, and the ACTIVE ENVELOPE.
python3 "$CLI" status            # --json for machines; the envelope is always printed

# 6. approve — widen or withdraw the envelope. Recorded (at/by/scope) and printed.
python3 "$CLI" approve --grant deploy
python3 "$CLI" approve --revoke push

# 7. advance — the bounded unattended settle loop. A process, never a daemon.
systemd-run --user --unit oprun-advance --collect -- \
  python3 "$(dirname "$CLI")/advance.py" --ledger "$PWD/.tmp/oprun/state.json" --timeout 600
```

## Ledger

`<repo>/.tmp/oprun/state.json` — overridable with `--ledger PATH` (before or after the verb).
`scripts/oprun.py` is the only writer; a worker reports by writing its **sidecar**
`<worktree>/.oprun/result.<dispatch_id>.json` and never touches the ledger. Schema and a filled
example: `templates/state.json`.

## Approval envelope

Decided once, at `init`, and then acted on without asking:

```jsonc
"approvals": {
  "commit":      {"granted": true,  "at": "…", "by": "user", "scope": "mission"},
  "push":        {"granted": true,  "at": "…", "by": "user", "scope": "mission"},
  "merge":       {"granted": true,  "at": "…", "by": "user", "scope": "mission"},
  "deploy":      {"granted": false},
  "publish":     {"granted": false},
  "destructive": {"granted": false}
}
```

- **No implicit widening.** With `push` granted, a force-push (`--force`), a tag (`--tag`), a
  release (`--release`), a rewritten-history push (`--rewrite-history`), a branch delete
  (`--delete-branch`) and a push to another repo (`--repo`) are each **refused** and each needs a
  fresh `approve --grant` (`destructive` for the first four, `publish` for tag/release/other-repo).
- **Not granted ⇒ ask exactly once**, on one line, then record the answer. Closed stdin counts as
  "no": nothing commits, and the lane still settles on its evidence.
- **`destructive` is never defaultable** — it is false unless it is typed.
- `status` always prints the envelope, so the operator can see what the conductor may do.

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | unknown lane/harness, missing ledger, failed pre-flight |
| 2 | usage (argparse) |
| 3 | illegal transition (dependency gate, wrong lane state, parallelism cap) |
| 4 | refused approval (unknown key, or an action outside the envelope) |
| 5 | a granted git side effect failed — the lane still settled, the failure is recorded |

## What this is not

No daemon, no second scheduler, no `hermes kanban` in any form, no PTY/Herdr lane, no model in the
completion path, no weakened tests, no database. One JSON ledger, one CLI, one skill.

## Hosting: why closing the laptop is safe

Every lane is launched with `systemd-run --user`, so it lands in
`user@1000.service/app.slice` — **outside** the login/SSH session's own scope. Closing the lid ends
an SSH session, not the cgroup a lane lives in (`Linger=yes`, `KillUserProcesses=false`), so a
dispatched lane keeps running and still writes its sidecar. What does **not** happen while you are
away is *advancing*: nothing settles a lane or dispatches the next one. That is exactly what
`advance` is for — a bounded process you launch detached next to the lanes. Details:
`references/hosting.md`.

## Routing and tests

- Unit→harness table (yours to override): `references/routing.md`. Per-CLI recipes and failure
  modes: `references/harness-adapters.md`.
- `python3 -m pytest tests/ -q` — ledger, probe, launch, concurrency, harness mapping, CLI and the
  approval envelope (real `git` in `tmp_path`, no mocks).
