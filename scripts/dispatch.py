#!/usr/bin/env python3
"""Launch one bounded Hermes coder/codevisor worker and wait until it exits."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REAL_HOME = "/home/logani"
HERMES = Path(REAL_HOME) / ".local/bin/hermes"
ALLOWED_PROFILES = {"coder", "codevisor"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", required=True, choices=sorted(ALLOWED_PROFILES))
    p.add_argument("--provider", required=True)
    p.add_argument("-m", "--model", required=True)
    p.add_argument("--reasoning", default="medium")
    p.add_argument("--brief", required=True, type=Path)
    p.add_argument("--workdir", required=True, type=Path)
    p.add_argument("--max-turns", type=int, required=True)
    p.add_argument("--run-budget", type=int, required=True)
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    brief = args.brief.expanduser().resolve()
    workdir = args.workdir.expanduser().resolve()
    if not brief.is_file():
        print(f"oprun-dispatch: brief not found: {brief}", file=sys.stderr)
        return 2
    if not workdir.is_dir():
        print(f"oprun-dispatch: workdir not found: {workdir}", file=sys.stderr)
        return 2
    if not HERMES.is_file():
        print(f"oprun-dispatch: hermes not found: {HERMES}", file=sys.stderr)
        return 2

    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(HERMES),
        "-p",
        args.profile,
        "chat",
        "--query-file",
        str(brief),
        "--provider",
        args.provider,
        "-m",
        args.model,
        "--reasoning",
        args.reasoning,
        "--max-turns",
        str(args.max_turns),
        "--run-budget",
        str(args.run_budget),
        "--pass-session-id",
        "--in",
        str(workdir),
    ]
    env = os.environ.copy()
    env["HOME"] = REAL_HOME
    started = time.time()
    session = None
    log_path = args.log.expanduser().resolve()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("argv: " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        pid = proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if session is None:
                if line.startswith("session_id:"):
                    session = line.split(":", 1)[1].strip()
                elif line.startswith("Session:"):
                    session = line.split(":", 1)[1].strip().split()[0]
        rc = proc.wait()

    sidecar = {
        "ok": rc == 0,
        "exitCode": rc,
        "session": session,
        "pid": pid,
        "profile": args.profile,
        "provider": args.provider,
        "model": args.model,
        "reasoning": args.reasoning,
        "brief": str(brief),
        "workdir": str(workdir),
        "log": str(log_path),
        "elapsedSeconds": round(time.time() - started, 1),
        "pendingChildren": False,
    }
    args.out.expanduser().resolve().write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(sidecar))
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    raise SystemExit(main())
