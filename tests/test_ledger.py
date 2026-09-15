"""Ports ``/tmp/canary3/test_ledger.py`` onto the shipped ledger, plus the v0.2 additions.

Every test name from the canary is kept verbatim (a later gate checks name parity); only the
call surface moved: canary3's ``complete()`` + ``trip_check()`` and its ``retry()`` helper are
the shipped ``settle()`` (which applies the breaker itself) and ``dispatch()``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# conftest.py covers the pytest entrypoint; this covers the direct one
# (`python3 tests/test_ledger.py`), where this module imports `ledger` before pytest has
# loaded any conftest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

from ledger import (  # noqa: E402
    BLOCKED,
    COMPLETED,
    DEPTH_EXCEEDED_TOKEN,
    DISPATCHED,
    FAILED,
    PARKED,
    PENDING,
    READY,
    TERMINAL,
    TRANSITIONS,
    DepthExceeded,
    IllegalTransition,
    Ledger,
    ParallelismExceeded,
    apply,
    display_state,
    normalize_model,
)


def _init(led: Ledger, lane_id: str, **overrides) -> dict:
    """Create a lane with the boring defaults every case needs."""
    kwargs: dict = {"harness": "fake-harness", "worktree": "/tmp/wt", "test_cmd": ["true"]}
    kwargs.update(overrides)
    return led.init_lane(lane_id, **kwargs)


class TestTransitionTable:
    def test_legal_pairs(self):
        assert apply(DISPATCHED, "success") == COMPLETED
        assert apply(DISPATCHED, "failure") == FAILED
        assert apply(READY, "dispatch") == DISPATCHED
        assert apply(PENDING, "ready") == READY
        assert apply(FAILED, "retry") == DISPATCHED
        assert apply(BLOCKED, "unblock") == READY
        assert apply(COMPLETED, "reopen") == READY
        assert apply(DISPATCHED, "evidence_unresolved") == READY
        assert apply(DISPATCHED, "review_unavailable") == READY
        assert TERMINAL == {COMPLETED, FAILED, BLOCKED}

    def test_illegal_pairs_raise_never_guess(self):
        for state, event in [(COMPLETED, "dispatch"), (COMPLETED, "success"),
                             (BLOCKED, "success"), ("nonsense", "success")]:
            with pytest.raises(IllegalTransition):
                apply(state, event)


class TestLedgerCase:
    """canary3's ``LedgerCase`` body, named ``Test*`` so pytest collects it."""

    @pytest.fixture()
    def path(self, tmp_path):
        return tmp_path / "state.json"

    @pytest.fixture()
    def led(self, path):
        return Ledger(path, failure_limit=3)

    def test_happy_path(self, led):
        _init(led, "lane-a")
        d1 = led.dispatch("lane-a")
        assert d1 == "lane-a-d1"
        res = led.settle("lane-a", d1, ok=True)
        assert res["accepted"]
        assert res["status"] == COMPLETED
        assert led.lane("lane-a")["status"] == COMPLETED

    # --- GAP B: dispatch fencing -------------------------------------------
    def test_stale_dispatch_cannot_settle_task(self, led):
        _init(led, "lane-a")
        d1 = led.dispatch("lane-a")
        # lane fails, controller retries -> new fencing token
        assert led.settle("lane-a", d1, ok=False, reason="boom")["accepted"]
        assert led.lane("lane-a")["status"] == FAILED   # 1 failure, below the limit
        d2 = led.dispatch("lane-a")
        assert d1 != d2
        # the OLD dispatch now reports success (late/duplicate delivery)
        res = led.settle("lane-a", d1, ok=True)
        assert not res["accepted"], "stale dispatch must be rejected"
        assert "stale dispatch" in res["reason"]
        # the current dispatch still settles correctly
        res2 = led.settle("lane-a", d2, ok=True)
        assert res2["accepted"]
        assert res2["status"] == COMPLETED

    def test_exactly_once_duplicate_rejected(self, led):
        _init(led, "lane-a")
        d1 = led.dispatch("lane-a")
        assert led.settle("lane-a", d1, ok=True)["accepted"]
        dup = led.settle("lane-a", d1, ok=True)
        assert not dup["accepted"]
        assert "duplicate" in dup["reason"]

    def test_wrong_dispatch_from_another_lane_is_fenced(self, led):
        _init(led, "lane-a")
        _init(led, "lane-b")
        led.dispatch("lane-a")
        db = led.dispatch("lane-b")
        res = led.settle("lane-a", db, ok=True)
        assert not res["accepted"], "lane-b's token must not settle lane-a"
        assert "stale dispatch" in res["reason"]
        # lane-b is untouched by the attempt
        assert led.lane("lane-b")["status"] == DISPATCHED

    # --- GAP C: circuit breaker --------------------------------------------
    def test_circuit_breaker_trips_at_limit(self, led):
        _init(led, "lane-cc")
        for i in range(1, 4):
            d = led.dispatch("lane-cc")
            res = led.settle("lane-cc", d, ok=False, reason="boom")
            assert res["accepted"]
            if i < 3:
                assert res["consecutive_failures"] == i
                assert led.lane("lane-cc")["status"] == FAILED, \
                    f"should not trip after {i} failures"
            else:
                assert led.lane("lane-cc")["status"] == BLOCKED, \
                    "must trip on the 3rd consecutive failure"
        assert led.lane("lane-cc")["status"] == BLOCKED

    def test_blocked_lane_does_not_silently_redispatch(self, led):
        _init(led, "lane-cc")
        for _ in range(3):
            d = led.dispatch("lane-cc")
            led.settle("lane-cc", d, ok=False, reason="boom")
        assert led.lane("lane-cc")["status"] == BLOCKED
        with pytest.raises(IllegalTransition):
            led.dispatch("lane-cc")

    def test_success_resets_failure_streak(self, led):
        _init(led, "lane-r")
        d = led.dispatch("lane-r")
        led.settle("lane-r", d, ok=False, reason="boom")
        assert led.lane("lane-r")["consecutive_failures"] == 1
        d = led.dispatch("lane-r")
        assert led.settle("lane-r", d, ok=True)["accepted"]
        assert led.lane("lane-r")["consecutive_failures"] == 0
        assert led.lane("lane-r")["status"] == COMPLETED

    # --- durability ---------------------------------------------------------
    def test_state_survives_reload(self, led, path):
        _init(led, "lane-a")
        d = led.dispatch("lane-a")
        assert led.settle("lane-a", d, ok=True)["accepted"]
        reloaded = Ledger(path)
        assert reloaded.lane("lane-a")["status"] == COMPLETED
        # fencing still enforced after reload
        res = reloaded.settle("lane-a", d, ok=True)
        assert not res["accepted"]

    # --- v0.2 additions -----------------------------------------------------
    def test_dependency_gate_blocks_dispatch_until_parent_completes(self, led):
        _init(led, "parent")
        _init(led, "child", depends_on=["parent"])
        with pytest.raises(IllegalTransition) as exc:
            led.dispatch("child")
        assert "parent" in str(exc.value)
        # nothing was mutated by the refused dispatch
        assert led.lane("child")["status"] == PENDING
        assert led.lane("child")["attempt"] == 0
        # once the parent is COMPLETED the child is dispatchable
        pd = led.dispatch("parent")
        assert led.settle("parent", pd, ok=True)["accepted"]
        assert led.dispatch("child") == "child-d1"

    def test_dependency_gate_also_fails_on_a_missing_parent(self, led):
        _init(led, "child", depends_on=["never-initialised"])
        with pytest.raises(IllegalTransition):
            led.dispatch("child")

    def test_nesting_beyond_max_depth_is_refused(self, path):
        led = Ledger(path, max_depth=1)
        _init(led, "root", depth=0)
        _init(led, "leaf", depth=1, parent_dispatch="root-d1")
        with pytest.raises(DepthExceeded) as exc:
            _init(led, "too-deep", depth=2, parent_dispatch="leaf-d1")
        assert DEPTH_EXCEEDED_TOKEN in str(exc.value)
        assert "nested_worker_depth_exceeded" in str(exc.value)
        # the refused lane was never created
        with pytest.raises(KeyError):
            led.lane("too-deep")

    def test_parent_dispatch_present_on_every_lane_record(self, led):
        _init(led, "root")
        _init(led, "child", parent_dispatch="root-d1")
        _init(led, "orphan")
        for lane_id in ("root", "child", "orphan"):
            record = led.lane(lane_id)
            assert "parent_dispatch" in record
            assert record["dispatch_id"] is None
            assert record["attempt"] == 0
            assert record["accepted"] == []
            assert record["rejected"] == []
            assert record["evidence"] == {}
            assert record["history"] == []
        assert led.lane("child")["parent_dispatch"] == "root-d1"
        assert led.lane("orphan")["parent_dispatch"] is None

    def test_max_parallel_dispatch_is_refused(self, path):
        led = Ledger(path, max_parallel=2)
        for lane_id in ("l0", "l1"):
            _init(led, lane_id)
            led.dispatch(lane_id)
        _init(led, "l2")
        with pytest.raises(ParallelismExceeded):
            led.dispatch("l2")
        assert led.lane("l2")["status"] == PENDING
        # the cap counts lanes dispatched RIGHT NOW: freeing a slot lets the next one in
        assert led.settle("l0", led.lane("l0")["dispatch_id"], ok=True)["accepted"]
        assert led.dispatch("l2") == "l2-d1"
        assert sorted(led.dispatched()) == ["l1", "l2"]

    def test_double_dispatch_of_a_live_lane_is_illegal(self, led):
        _init(led, "lane-a")
        d1 = led.dispatch("lane-a")
        with pytest.raises(IllegalTransition):
            led.dispatch("lane-a")
        # the live token is unchanged by the refused call
        assert led.lane("lane-a")["dispatch_id"] == d1
        assert led.lane("lane-a")["attempt"] == 1

    def test_normalize_model_equivalent_spellings(self):
        assert normalize_model("Cursor Grok 4.6") == normalize_model("cursor-grok-4.6")
        assert normalize_model("GPT_5.6.Sol") == normalize_model("gpt-5.6-sol")
        # genuinely different pins must NOT look equal
        assert normalize_model("gpt-5.6-sol") != normalize_model("grok-4.6")
        assert normalize_model("gpt-5.6-sol") != normalize_model("gpt-5.6-terra")
        assert normalize_model("") == ""
        assert normalize_model("---") == ""

    def test_settle_records_evidence_and_never_invents_commit(self, led):
        _init(led, "lane-e")
        _init(led, "lane-f")
        evidence = {"uncommitted": True, "hashes": {"scripts/ledger.py": "deadbeef"}}
        de = led.dispatch("lane-e")
        assert led.settle("lane-e", de, ok=True, evidence=evidence)["accepted"]
        stored = led.lane("lane-e")["evidence"]
        assert stored == evidence
        assert "commit" not in stored, "an uncommitted lane must not be given a commit"
        # the ledger keeps its own copy: a later mutation of the caller's dict is not a write
        evidence["hashes"]["scripts/ledger.py"] = "tampered"
        assert led.lane("lane-e")["evidence"]["hashes"] == {"scripts/ledger.py": "deadbeef"}
        # a commit the caller DOES report is stored verbatim
        df = led.dispatch("lane-f")
        given = {"commit": "abc123", "uncommitted": False}
        assert led.settle("lane-f", df, ok=True, evidence=given)["accepted"]
        assert led.lane("lane-f")["evidence"] == given

    def test_settle_without_evidence_records_an_empty_dict(self, led):
        _init(led, "lane-a")
        d = led.dispatch("lane-a")
        assert led.settle("lane-a", d, ok=True)["accepted"]
        assert led.lane("lane-a")["evidence"] == {}

    def test_typed_park_returns_ready_without_failure_streak(self, led):
        _init(led, "lane-park")
        d = led.dispatch("lane-park")
        result = led.settle("lane-park", d, ok=False, reason="missing artifact",
                            evidence={"verdict": "evidence_unresolved"},
                            park_kind="evidence_unresolved")
        lane = led.lane("lane-park")
        assert result["status"] == READY
        assert lane["status"] == READY
        assert lane["consecutive_failures"] == 0
        assert lane["evidence"]["park_kind"] == "evidence_unresolved"
        assert lane["history"][-1]["event"] == "evidence_unresolved"
        assert (lane["history"][-1]["from"], lane["history"][-1]["to"]) == (DISPATCHED, READY)
        base_commit = lane["base_commit"]
        assert led.dispatch("lane-park") == "lane-park-d2"
        assert led.lane("lane-park")["base_commit"] == base_commit

    def test_review_unavailable_park_is_typed_and_reenterable(self, led):
        _init(led, "lane-review")
        d = led.dispatch("lane-review")
        base_commit = led.lane("lane-review")["base_commit"]
        result = led.settle("lane-review", d, ok=False, reason="reviewer unavailable",
                            evidence={"verdict": "review_unavailable",
                                      "reviewer": {"available": False}},
                            park_kind="review_unavailable")
        assert result["status"] == READY
        assert led.lane("lane-review")["consecutive_failures"] == 0
        assert led.lane("lane-review")["evidence"]["park_kind"] == "review_unavailable"
        assert led.dispatch("lane-review") == "lane-review-d2"
        assert led.lane("lane-review")["base_commit"] == base_commit

    def test_rejected_settles_are_recorded_on_the_lane(self, led):
        _init(led, "lane-a")
        d1 = led.dispatch("lane-a")
        led.settle("lane-a", "lane-a-d99", ok=True)
        assert led.settle("lane-a", d1, ok=True)["accepted"]
        led.settle("lane-a", d1, ok=True)
        reasons = [entry["reason"] for entry in led.lane("lane-a")["rejected"]]
        assert len(reasons) == 2
        assert any("stale dispatch" in r for r in reasons)
        assert any("duplicate" in r for r in reasons)

    def test_history_records_every_transition(self, led):
        _init(led, "lane-h")
        d = led.dispatch("lane-h")
        led.settle("lane-h", d, ok=False, reason="boom")
        history = led.lane("lane-h")["history"]
        assert [(h["from"], h["event"], h["to"]) for h in history] == [
            (PENDING, "dispatch", DISPATCHED),
            (DISPATCHED, "failure", FAILED),
        ]

    def test_lane_summary_and_dispatched_views(self, led):
        _init(led, "lane-a")
        _init(led, "lane-b")
        assert led.dispatched() == []
        assert led.summary() == {PENDING: 2}
        d = led.dispatch("lane-a")
        assert led.dispatched() == ["lane-a"]
        assert led.summary() == {PENDING: 1, DISPATCHED: 1}
        assert led.settle("lane-a", d, ok=True)["accepted"]
        assert led.dispatched() == []
        assert led.summary() == {PENDING: 1, COMPLETED: 1}

    def test_lanes_are_independent(self, led):
        _init(led, "lane-a")
        _init(led, "lane-b")
        da = led.dispatch("lane-a")
        db = led.dispatch("lane-b")
        assert led.settle("lane-a", da, ok=True)["accepted"]
        assert led.lane("lane-a")["status"] == COMPLETED
        assert led.lane("lane-b")["status"] == DISPATCHED
        assert led.lane("lane-b")["dispatch_id"] == db
        assert led.settle("lane-b", db, ok=False, reason="boom")["accepted"]
        assert led.lane("lane-b")["status"] == FAILED

    def test_init_lane_is_idempotent(self, led):
        first = _init(led, "lane-a")
        again = _init(led, "lane-a", harness="other-harness")
        assert again["harness"] == "fake-harness"
        assert first == again

    def test_unknown_lane_is_an_error(self, led):
        with pytest.raises(KeyError):
            led.dispatch("ghost")
        with pytest.raises(KeyError):
            led.lane("ghost")

    def test_no_public_write_api(self, led):
        """A worker must not be able to write the ledger without a fenced transition."""
        assert not hasattr(led, "save")
        assert not hasattr(led, "write")


# --- the parked display state (issue #2) --------------------------------------
#: The evidence a ``settle --needs-review`` / refused ``--accept`` leaves: the verdict word is the
#: one thing that separates "waiting for a decision" from "this dispatch failed".
PARKED_EVIDENCE = {"controller": "oprun-settle", "verdict": "needs_review", "reason": "flaky test"}


class TestParkedDisplay:
    """``parked`` is a word a reader sees, never a state the machine holds."""

    @pytest.fixture()
    def led(self, tmp_path):
        return Ledger(tmp_path / "state.json", failure_limit=3)

    def test_needs_review_settle_displays_parked(self, led):
        _init(led, "lane-a")
        d1 = led.dispatch("lane-a")
        assert led.settle("lane-a", d1, ok=False, evidence=PARKED_EVIDENCE,
                          reason="flaky test")["accepted"]

        lane = led.lane("lane-a")
        # the stored state is untouched: still FAILED, still terminal, still fenced
        assert lane["status"] == FAILED
        assert lane["status"] in TERMINAL
        assert led.summary() == {FAILED: 1}
        assert display_state(lane) == PARKED
        # ...and `parked` is not reachable AS a state, so nothing can transition through it
        assert PARKED not in TERMINAL
        assert PARKED not in set(TRANSITIONS.values())
        with pytest.raises(IllegalTransition):
            apply(PARKED, "retry")
        with pytest.raises(IllegalTransition):
            apply(PARKED, "success")
        # the fence is untouched by the display word: a superseded token still cannot settle
        d2 = led.dispatch("lane-a")
        assert d2 == "lane-a-d2"
        stale = led.settle("lane-a", d1, ok=False, reason="late delivery")
        assert not stale["accepted"] and "stale dispatch" in stale["reason"]

    def test_display_state_keeps_failed_and_blocked_distinct(self, tmp_path):
        led = Ledger(tmp_path / "state.json", failure_limit=2)
        # a lane `advance` failed for its own reason (red test / unusable artifact): its verdict is
        # the witness's word, not `needs_review`, so it keeps the honest `failed`
        _init(led, "advance-failed")
        d = led.dispatch("advance-failed")
        assert led.settle("advance-failed", d, ok=False, reason="test rc=1",
                          evidence={"controller": "advance", "verdict": "failed",
                                    "rejected_because": "test rc=1"})["accepted"]
        assert display_state(led.lane("advance-failed")) == FAILED

        # the circuit breaker outranks the label: a lane parked twice is BLOCKED, not merely parked
        _init(led, "breaker")
        for _ in range(2):
            pending = led.dispatch("breaker")
            led.settle("breaker", pending, ok=False, evidence=PARKED_EVIDENCE, reason="flaky")
        breaker = led.lane("breaker")
        assert breaker["status"] == BLOCKED
        assert display_state(breaker) == BLOCKED
        assert display_state(breaker) != PARKED

    def test_display_state_is_the_plain_status_for_anything_else(self, led):
        _init(led, "lane-a")
        assert display_state(led.lane("lane-a")) == PENDING
        d = led.dispatch("lane-a")
        assert display_state(led.lane("lane-a")) == DISPATCHED
        assert led.settle("lane-a", d, ok=True,
                          evidence={"controller": "oprun-settle", "verdict": "accepted"})["accepted"]
        assert display_state(led.lane("lane-a")) == COMPLETED

    def test_lane_records_the_merge_target_it_was_dispatched_into(self, led):
        """``dispatch --into`` is recorded ON the lane, so settle can refuse a wrong checkout."""
        _init(led, "plain")
        assert led.lane("plain")["merge_into"] is None, "no target recorded means no target"
        _init(led, "targeted", merge_into="main")
        assert led.lane("targeted")["merge_into"] == "main"
        # an explicit re-dispatch replaces the target; a dispatch without --into leaves it alone
        _init(led, "targeted", merge_into="release")
        assert led.lane("targeted")["merge_into"] == "release"
        _init(led, "targeted")
        assert led.lane("targeted")["merge_into"] == "release"
        # the record survives dispatch and settle untouched
        assert led.dispatch("targeted") == "targeted-d1"
        assert led.lane("targeted")["merge_into"] == "release"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
