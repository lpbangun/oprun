# Run proposal — {{mission}}

One approval per **run**, not per dispatch. Fill this in at intake, `init` the envelope it names, and
the conductor then acts inside it without asking again. Each lane is one row; the flow below says
what runs in parallel and what is gated. (Blank fields are filled when the user says go.)

- repo: {{repo}}
- ledger: {{ledger}}
- proposed: {{created_at}}
- approved by: {{by}} on {{approved_at}}

## Envelope (per run)

| key | proposed | why |
|---|---|---|
| commit | yes | lane work lands as a commit the controller can cite |
| push | yes | the branch is shared; no force-push implied |
| merge | yes | integration inside the recorded envelope |
| deploy | no | — |
| publish | no | — |
| destructive | no | never defaultable |

`init --approve commit,push,merge` records exactly this row set. Nothing outside it happens without a
fresh `oprun approve --grant`, and each refusal names the missing grant.

## Lanes

One lane = one harness, one worktree, one owner, one `test_cmd`.

| lane | harness | model | depends on | worktree | test gate (controller re-run) |
|---|---|---|---|---|---|
| {{lane}} | {{harness}} | {{model}} | — | {{worktree}} | `{{test_cmd}}` |

## Caps

- max-parallel: {{max_parallel}}
- failure-limit: {{failure_limit}}

## Flow

- wave 1 (parallel): {{wave_1}}
- wave 2 (after wave 1 settles): {{wave_2}}

## Acceptance, per lane

Its sidecar for the ledger's **current** `dispatch_id`, reporting `status == "success"`, plus the
controller's own re-run of that lane's `test_cmd` exiting 0, with every evidence path resolving.
Nothing else. `probe` is the verdict; `status` is context.

## Wake-up

Probe on cadence after every conductor step and every user turn; `probe --wait --timeout` for bounded
waits. A finished lane is noticed from `advance`'s exit summary or the sidecar's `finished_at` — no
daemon, no heartbeat, no timer that dispatches.

## Gates for the conductor

- `needs_review` from `probe`/`advance` → a human decides; never retried blindly past the breaker.
- Anything outside the envelope → refused, and the refusal names the missing grant.
- Scope change → ask. Everything inside the envelope → act, do not re-litigate.
