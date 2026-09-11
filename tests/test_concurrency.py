"""Concurrency proof for the shipped ledger — ports ``/tmp/oprun5/concurrency-proof.py``.

Six OS processes hammer ONE ledger file, 25 real dispatch/settle cycles each, while the
parent keeps re-reading that file. ``Ledger._locked`` re-reads the JSON under ``flock``
before every mutation; without that re-read each writer flushes its own stale snapshot over
a peer's committed change, and every assertion below fails loudly:

  * each worker asserts its own settle was ACCEPTED (a clobbered lane loses its DISPATCHED
    state, so the settle comes back stale/fenced),
  * the parent asserts no lane ever disappears from the file and that the file always parses,
  * the final file must carry all 25 attempts and 25 accepted tokens per lane, in order.

Run directly (``python3 tests/test_concurrency.py``) as well as under pytest: the exit code is
non-zero on any lost update, because the assertions are the check.
"""
from __future__ import annotations

import sys
from pathlib import Path

# conftest.py covers the pytest entrypoint; this covers the direct one
# (`python3 tests/test_concurrency.py`, the spelling the acceptance benchmark uses), where
# this module imports `ledger` before pytest has loaded any conftest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import json  # noqa: E402
import multiprocessing  # noqa: E402
import time  # noqa: E402
from queue import Empty  # noqa: E402
from typing import Any  # noqa: E402

from ledger import FAILED, TERMINAL, Ledger  # noqa: E402

LANES = ["l1", "l2", "l3", "l4", "l5", "l6"]
CYCLES = 25
# Six lanes can be mid-flight simultaneously, so the cap must admit the whole fan-out:
# otherwise this measures ParallelismExceeded instead of lost updates.
MAX_PARALLEL = len(LANES)
# The breaker must not trip mid-run — a parked lane stops mutating and the writer path under
# test stops being exercised — so the limit sits one failure above the cycle count.
FAILURE_LIMIT = CYCLES + 1
# Widens the dispatch -> settle window: this is exactly where a writer without
# reload-under-lock clobbers a peer's committed change.
WORKER_PAUSE = 0.001
# How long the parent waits for a single file read loop / worker join.
JOIN_TIMEOUT = 60.0


def _worker(lane_id: str, path: str, cycles: int, report: Any) -> None:
    """One process, one lane: ``cycles`` real cycles, then two fencing probes."""
    record: dict = {"lane": lane_id, "tokens": [], "dup": None, "stale": None,
                    "error": None}
    try:
        led = Ledger(path, failure_limit=FAILURE_LIMIT, max_parallel=MAX_PARALLEL)
        tokens: list[str] = []
        for _ in range(cycles):
            token = led.dispatch(lane_id)
            tokens.append(token)
            time.sleep(WORKER_PAUSE)
            res = led.settle(lane_id, token, ok=False, evidence={"by": lane_id},
                             reason="synthetic failure")
            # A rejected settle here means the lane's DISPATCHED state was lost.
            assert res["accepted"], f"{lane_id}: settle rejected during the free run: {res}"
            time.sleep(WORKER_PAUSE)
        # duplicate of the CURRENT token: fenced by exactly-once
        dup = led.settle(lane_id, tokens[-1], ok=False, evidence={"by": lane_id})
        # token from the first attempt: fenced as stale
        stale = led.settle(lane_id, tokens[0], ok=False, evidence={"by": lane_id})
        record.update({"tokens": tokens, "dup": dup, "stale": stale})
    except BaseException as exc:            # a worker never dies silently
        record["error"] = f"{type(exc).__name__}: {exc}"
        report.put(record)
        raise
    report.put(record)


def _read_doc(path: Path) -> tuple[dict | None, str | None]:
    """Read the ledger file; return ``(doc, error)``. Writers are atomic, so no torn reads."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                # JSONDecodeError, FileNotFoundError, ...
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(doc, dict) or "lanes" not in doc:
        return None, f"malformed document: {str(doc)[:80]}"
    return doc, None


def test_six_processes_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    led = Ledger(path, failure_limit=FAILURE_LIMIT, max_parallel=MAX_PARALLEL)
    for lane_id in LANES:
        led.init_lane(lane_id, harness="fake-harness", worktree=str(tmp_path / lane_id),
                      test_cmd=["true"])
    assert sorted(json.loads(path.read_text(encoding="utf-8"))["lanes"]) == sorted(LANES)

    report: Any = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=_worker, args=(lane_id, str(path), CYCLES, report))
             for lane_id in LANES]
    for proc in procs:
        proc.start()

    # --- watch the file for the whole run ----------------------------------
    snapshots = 0
    read_errors: list[str] = []
    lost_lanes: list[list[str]] = []
    while any(proc.is_alive() for proc in procs):
        doc, error = _read_doc(path)
        if error is not None:
            read_errors.append(error)
        else:
            snapshots += 1
            missing = [lane_id for lane_id in LANES if lane_id not in doc["lanes"]]
            if missing:
                lost_lanes.append(missing)
        time.sleep(WORKER_PAUSE / 2)

    for proc in procs:
        proc.join(timeout=JOIN_TIMEOUT)
    for proc in procs:
        assert not proc.is_alive(), "worker hung: a ledger lock may be stuck"
        assert proc.exitcode == 0, f"worker exited {proc.exitcode}"

    reports: list[dict] = []
    for _ in LANES:
        try:
            reports.append(report.get(timeout=5))
        except Empty:                        # pragma: no cover - only on a worker crash
            break

    # --- the file was never torn, and no lane ever vanished ----------------
    assert snapshots > 0, "the parent never observed a well-formed ledger mid-run"
    assert read_errors == [], f"the file did not always parse: {read_errors[:3]}"
    assert lost_lanes == [], f"lost lanes observed mid-run: {lost_lanes[:3]}"

    # --- every worker's own tokens survived, in order ----------------------
    assert len(reports) == len(LANES)
    assert sorted(entry["lane"] for entry in reports) == sorted(LANES)
    for entry in reports:
        assert entry["error"] is None, entry["error"]
        lane_id = entry["lane"]
        expected = [f"{lane_id}-d{n}" for n in range(1, CYCLES + 1)]
        assert entry["tokens"] == expected, f"{lane_id}: fencing tokens lost or renumbered"
        assert entry["dup"]["accepted"] is False, f"{lane_id}: duplicate settle accepted"
        assert "duplicate" in entry["dup"]["reason"], entry["dup"]
        assert entry["stale"]["accepted"] is False, f"{lane_id}: stale settle accepted"
        assert "stale" in entry["stale"]["reason"], entry["stale"]

    # --- the file on disk agrees: no lost lanes, every lane terminal -------
    final, error = _read_doc(path)
    assert error is None, error
    assert final is not None
    assert sorted(final["lanes"]) == sorted(LANES)
    for lane_id in LANES:
        lane = final["lanes"][lane_id]
        assert lane["status"] in TERMINAL, (lane_id, lane["status"])
        assert lane["status"] == FAILED, (lane_id, lane["status"])
        assert lane["attempt"] == CYCLES, \
            f"{lane_id}: {lane['attempt']} attempts, expected {CYCLES}"
        assert len(lane["accepted"]) == CYCLES, \
            f"{lane_id}: {len(lane['accepted'])} accepted tokens, expected {CYCLES}"
        assert lane["consecutive_failures"] == CYCLES, lane_id
        assert lane["dispatch_id"] == f"{lane_id}-d{CYCLES}"
        assert lane["evidence"] == {"by": lane_id}
        # exactly the two probes were rejected: nothing was silently dropped
        reasons = [entry["reason"] for entry in lane["rejected"]]
        assert len(reasons) == 2, (lane_id, reasons)
        assert any("duplicate" in reason for reason in reasons), (lane_id, reasons)
        assert any("stale" in reason for reason in reasons), (lane_id, reasons)

    # a fresh handle on the same file sees the same thing (state survives reload)
    assert Ledger(path).summary() == {FAILED: len(LANES)}


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
