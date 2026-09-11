# Hosting — `systemd-run --user`, the durability mechanism

Every unattended lane is launched as a **detached transient systemd user unit**
(`scripts/launch.py`). That is the whole detach story: there is no `nohup`, no `tmux`, no `screen`,
no Herdr, and no shell `&` anywhere in `launch.py` or in `oprun.py`.

## Why a unit and not a background shell job

A unit started with `systemd-run --user` lands in
`/user.slice/user-1000.slice/user@1000.service/app.slice/<unit>.service` — **outside** the login /
SSH session's own cgroup (`session-*.scope`). Measured on this box:

```
$ systemd-run --user --collect --unit oprun-doccheck -- bash -c 'echo "$(cat /proc/self/cgroup)"'
0::/user.slice/user-1000.slice/user@1000.service/app.slice/oprun-doccheck.service
```

A backgrounded shell job lives in the *launching* session's scope instead, so it dies when that
session is reaped. The cgroup is the difference, and it is the reason "closing the laptop" is safe:

| Setting | Measured value | Meaning |
|---|---|---|
| `Linger` (user `logani`) | `yes` | the user's systemd manager persists after logout, so `--user` units survive too |
| `KillUserProcesses` | `no` (logind default, `#`-commented in `/etc/systemd/logind.conf`) | ending a session does not reap its processes |
| sessions in `closing` state | present for **1 month 22 days** | session-end does not kill what it hosts here |

So: work already launched **survives** the lid closing; it is outside the session scope entirely.
Launching a worker in the chat's own session cgroup — `nohup`, `tmux`, `&`, a Herdr pane — is
banned, because that reintroduces exactly the failure mode this replaced.

## Launch, exit status and teardown

`launch.build_systemd_run_argv` always passes:

- `--unit=<oprun-<lane>>` — one unit per lane, so `systemctl --user show oprun-alpha` answers for
  that lane alone;
- `--collect` — a transient unit is removed the moment it exits, so units never accumulate
  (`systemctl --user list-units 'oprun-*'` returns to clean once lanes are settled);
- `--working-directory=<worktree>` — the lane's worktree, not the conductor's cwd;
- `--setenv=HOME=/home/logani` — pinned, and not overridable through `env`, so a worker never
  inherits a sandboxed HOME (`hermes` and `pi` resolve their profile from it).

`launch.launch()` reports **systemd-run's own exit status**, not an inference. The lane's real
result is read afterwards from systemd, one property per call
(`systemctl --user show <unit> -p ExecMainStatus --value`):

```
$ systemd-run --user --unit oprun-doccheck2 --wait --pipe -- bash -c 'exit 7'
wait_pipe_rc=7
$ systemctl --user show oprun-doccheck2 -p ExecMainStatus -p Result --value
ExecMainStatus=7
Result=exit-code
```

`launch.unit_known()` exists because systemd answers happily for a unit that never existed, with
*default* values (`Result=success`, `ExecMainStatus=0`) that read like the successful run of a unit
that was never created. Both signals are therefore checked: `LoadState` and `FragmentPath`. An
unknown unit is never evidence about a lane.

Teardown is `systemctl --user stop <unit>` (`launch.stop`), best effort: settlement has already
recorded the lane's evidence by the time teardown runs, and a missing unit is not an error.

## journald is the audit trail

Each unit's stdout/stderr goes to the journal, including the harness's own terminal event:

```
$ journalctl --user -u oprun-doccheck --no-pager
Started oprun-doccheck.service - /usr/bin/bash -c "sleep 1; exit 7".
oprun-doccheck.service: Main process exited, code=exited, status=7/NOTRUNNING
oprun-doccheck.service: Failed with result 'exit-code'.
```

That is how a conductor answers "what did the worker actually print" without trusting the worker.

## The honest limit: nothing advances unattended

Durability is **process lifetime**, not progress. A dispatched lane finishes, its sidecar lands,
and then the mission sits there — the conductor only acts on a turn, so nothing settles that lane
or dispatches the next one while you are away.

That is what `advance` is for: a **bounded process** (not a daemon — it returns when the lanes are
decided or the deadline passes) that you launch detached next to the lanes:

```bash
systemd-run --user --unit oprun-advance --collect -- \
  python3 scripts/advance.py --ledger "$REPO/.tmp/oprun/state.json" --timeout 600
```

It waits for the DISPATCHED lanes, validates each sidecar against the ledger's **current**
`dispatch_id`, **re-runs each lane's own `test_cmd`**, and settles the ledger with evidence — with
zero conductor turns. It holds no state of its own, reads the same `state.json`, and exits.

What it deliberately does **not** do: retry a broken lane (the circuit breaker parks it —
`blocked`/`needs_review` — and the mission waits for a human), or act outside the approval
envelope. Execution is unattended; correction is not.
