# Mission — {{mission}}

- repo: {{repo}}
- ledger: {{ledger}}
- created: {{created_at}}

## Envelope

The approval envelope is recorded in the ledger and printed by `oprun status`. Nothing outside it
happens without a fresh `oprun approve --grant`. `destructive` is never defaultable.

## Lanes

One lane = one harness, one worktree, one owner, one `test_cmd`. A lane is accepted only on
evidence it produced: its sidecar for the ledger's **current** `dispatch_id`, plus the
controller's own re-run of its `test_cmd`.

## Gates for the conductor

- `needs_review` from `probe`/`advance` → a human decides; never retried blindly past the
  circuit breaker.
- Anything outside the envelope → refused, and the refusal names the missing grant.
- Scope change → ask. Everything inside the envelope → act, do not re-litigate.
