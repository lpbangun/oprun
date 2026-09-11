"""The identity rule on the ACCEPTANCE path — a substituted MODEL must never be recorded as
``completed``, and a worker's opinion about which CLI it is must never fail correct work.

A unit test of ``ledger.normalize_model`` proves nothing about a lane: the rule has to run where a
lane is actually settled. Every case here drives a shipped acceptance path — the ``settle`` verb (a
real CLI process with stdin closed) and ``advance()`` (the bounded runner) — against a real ledger, a
real worktree and a real sidecar, so what is asserted is what decides a live lane.

The rule, as measured, is two different things:

* the **model pin** is the no-substitution guard. A pinned lane whose sidecar reports a different
  model parks, and a pinned lane whose sidecar reports **no** model at all parks too (there is
  nothing to show the pin held). One measured exception, and it is not a relaxation of the rule: a
  harness that declares ``pin_verifiable=False`` (see ``harnesses.Harness`` and the codex notes)
  passes the pin to the CLI but emits nothing that can corroborate it, so a reported difference
  cannot be told apart from the agent guessing at its own name — there it is the advisory
  ``identity_warning``, and the lane is decided on its real evidence;
* the **harness** is not a guard at all. oprun picked the binary from the registry, so the worker's
  spelling is recorded verbatim (``harness_reported``) and warned about (``identity_warning``), and
  never enforced. A live two-vendor run parked a correct ``droid`` lane — right binary, verified in
  the journal — because the agent self-reported ``cursor-agent``: parking correct work on an
  unreliable self-report is a false-negative generator.

The defects this file covers, in the order they were measured:

* **the pin was never passed** — ``argv_for`` appended ``--model`` only for a harness with
  ``requires_model_pin`` (``claude``), so a codex lane ran its own default while the ledger recorded
  ``model_requested: "gpt-5.6-sol"``. The argv half lives in ``tests/test_harnesses.py``; the alias
  half (``--harness openai-codex``, the provider spelling) is here;
* **``normalize_model`` was dead code** — ``probe.py`` captured ``harness``/``model`` and ``settle``
  rebuilt its evidence without them, so nothing ever compared requested against reported: a lane that
  reported ``gpt-6-astra`` against a ``gpt-5.6-sol`` pin was accepted as ``completed``;
* **the alias did not resolve** — ``--harness openai-codex`` was an unknown registry id, so the
  frozen two-vendor command failed before it dispatched anything;
* **the harness half of the guard was a false-negative generator** — it parked a lane that ran the
  designated binary and reported the wrong CLI name, so the harness self-report is now advisory;
* **codex cannot corroborate its pin** — measured: the pin reaches the model layer, ``exec --json``
  emits no model id, and the agent's own answer is a wrong guess, so a reported mismatch there says
  nothing about substitution.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "oprun.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import advance  # noqa: E402  (sibling: the bounded runner)
import harnesses  # noqa: E402  (sibling: the registry + the identity rule)
import oprun  # noqa: E402  (sibling: the CLI)
from ledger import COMPLETED, FAILED, Ledger  # noqa: E402

#: A test command that passes, run through the same interpreter as the suite.
TEST_PASS = [sys.executable, "-c", "raise SystemExit(0)"]

#: Both acceptance paths. Every case below is asserted on each, because both must decide it.
PATHS = ("settle", "advance")

#: A harness whose model pin CAN be corroborated (``pin_verifiable=True``): a reported mismatch is
#: fatal. ``codex`` deliberately is not used here — see the codex cases below.
VERIFIABLE_HARNESS = "droid"


# --- fixtures ----------------------------------------------------------------
def mission(tmp_path: Path, *, lane: str = "alpha", harness: str = "codex",
            model: str | None = "gpt-5.6-sol") -> dict:
    """A real ledger holding one DISPATCHED lane with a real worktree. Nothing is mocked."""
    ledger_path = tmp_path / "repo" / ".tmp" / "oprun" / "state.json"
    worktree = tmp_path / "wt" / lane
    worktree.mkdir(parents=True)
    led = Ledger(ledger_path)
    led.init_lane(lane, harness=harness, worktree=str(worktree), test_cmd=list(TEST_PASS),
                  model=model)
    return {"ledger": ledger_path, "worktree": worktree, "lane": lane,
            "dispatch_id": led.dispatch(lane)}


def sidecar(worktree: Path, dispatch_id: str, *, lane: str = "alpha",
            harness: object = "codex", model: object = "gpt-5.6-sol",
            status: str = "success", omit: tuple[str, ...] = ()) -> Path:
    """Write a worker's evidence file atomically, exactly where the controller reads it."""
    directory = worktree / advance.SIDECAR_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict = {"schema_version": 1, "task_id": lane, "dispatch_id": dispatch_id,
                     "status": status, "harness": harness, "model": model, "exit_code": 0}
    for key in omit:
        payload.pop(key, None)
    target = advance.sidecar_path(worktree, dispatch_id)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return target


def accept(path: str, fixture: dict):
    """Run one acceptance path: the CLI's ``CompletedProcess``, or ``advance()``'s summary."""
    if path == "settle":
        return subprocess.run(
            [sys.executable, str(CLI), "settle", fixture["lane"], "--accept",
             "--ledger", str(fixture["ledger"])],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
        )
    return advance.advance(fixture["ledger"], timeout=3, poll=0.05, emit=lambda _line: None)


def lane_record(fixture: dict) -> dict:
    """The lane as it is on disk after the acceptance path ran."""
    return Ledger(fixture["ledger"]).lane(fixture["lane"])


def assert_parked(fixture: dict, path: str, result, *needles: str) -> dict:
    """The lane was PARKED: never completed, reason recorded, and the CLI said so out loud."""
    lane = lane_record(fixture)
    reason = lane.get("needs_review_reason") or ""
    assert lane["status"] != COMPLETED, f"a mismatch was accepted: {lane['status']!r}"
    assert lane["status"] == FAILED, lane["status"]
    assert COMPLETED not in {entry["to"] for entry in lane["history"]}
    assert all(entry["event"] != "success" for entry in lane["history"])
    for needle in needles:
        assert needle in reason, reason
    if path == "settle":
        assert lane["evidence"]["verdict"] == "needs_review"
        assert result.returncode != 0, "a refused --accept must not exit 0"
        assert "Traceback" not in result.stderr
    else:
        assert result["accepted"] == []
        assert fixture["lane"] in result["needs_review"]
    return lane


def assert_accepted(fixture: dict, path: str, result, *warning_needles: str) -> dict:
    """The lane was ACCEPTED on the normal evidence rule, with an advisory warning naming the strings.

    ``assert_parked``'s mirror image, and the only shape the harness half of the guard may produce: the
    lane is ``completed``, the acceptance path said so out loud (exit 0 / listed in ``accepted``), and
    ``identity_warning`` is present in the settled evidence, naming what disagreed.
    """
    lane = lane_record(fixture)
    assert lane["status"] == COMPLETED, lane.get("needs_review_reason")
    assert COMPLETED in {entry["to"] for entry in lane["history"]}
    if path == "settle":
        assert result.returncode == 0, result.stderr
    else:
        assert fixture["lane"] in result["accepted"], result
    warning = lane["evidence"]["identity_warning"]
    for needle in warning_needles:
        assert needle in warning, warning
    return lane


# --- defect 2: a different model is never accepted ---------------------------
@pytest.mark.parametrize("path", PATHS)
def test_a_substituted_model_parks_the_lane_and_names_both_strings(tmp_path: Path, path: str):
    """The measured live defect, on a harness whose pin can be corroborated: requested, reported differ.

    ``VERIFIABLE_HARNESS`` and not ``codex``: codex passes its pin but emits nothing that names the
    model that ran, so its mismatch is advisory (see the codex cases below). Where the comparison can
    establish something, a mismatch is still fatal — that half of the rule must not move.
    """
    fixture = mission(tmp_path, harness=VERIFIABLE_HARNESS, model="gpt-5.6-sol")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness=VERIFIABLE_HARNESS,
            model="gpt-6-astra")

    lane = assert_parked(fixture, path, accept(path, fixture), "gpt-5.6-sol", "gpt-6-astra")

    assert lane["evidence"]["model_requested"] == "gpt-5.6-sol"
    assert lane["evidence"]["model_reported"] == "gpt-6-astra", "the raw report is never repaired"
    assert lane["evidence"]["harness_reported"] == VERIFIABLE_HARNESS
    assert lane["evidence"]["harness"] == VERIFIABLE_HARNESS
    assert lane["evidence"]["identity_warning"] == "", "an agreeing harness raises no advisory"


@pytest.mark.parametrize("path", PATHS)
def test_a_respelled_pin_is_the_same_model_and_is_accepted(tmp_path: Path, path: str):
    """``cursor-grok-4.6`` requested, ``Cursor Grok 4.6`` reported: one model, so accept."""
    fixture = mission(tmp_path, harness="cursor-agent", model="cursor-grok-4.6")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="cursor-agent",
            model="Cursor Grok 4.6")

    result = accept(path, fixture)
    lane = lane_record(fixture)

    assert lane["status"] == COMPLETED
    assert lane["evidence"]["model_requested"] == "cursor-grok-4.6"
    assert lane["evidence"]["model_reported"] == "Cursor Grok 4.6", "both raw strings are kept"
    if path == "settle":
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("harness_id", (VERIFIABLE_HARNESS, "codex"))
@pytest.mark.parametrize("path", PATHS)
def test_a_pin_that_was_never_reported_is_not_accepted(tmp_path: Path, path: str,
                                                       harness_id: str):
    """An unreported model cannot be shown to match the designation, so it parks (documented rule).

    The sidecar exists and claims success; it simply does not say which model ran. Accepting it would
    assert the pin held on no evidence at all — which is the failure this check exists to stop.

    Parametrised over a verifiable harness **and** codex on purpose: ``pin_verifiable=False`` buys an
    escape from a *reported difference* only. Having no report at all stays fatal on every harness.
    """
    fixture = mission(tmp_path, harness=harness_id, model="gpt-5.6-sol")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness=harness_id, omit=("model",))

    lane = assert_parked(fixture, path, accept(path, fixture), "gpt-5.6-sol", "not reported")

    assert lane["evidence"]["model_reported"] is None
    assert lane["evidence"]["model_requested"] == "gpt-5.6-sol"


@pytest.mark.parametrize("path", PATHS)
def test_no_model_designation_is_not_a_substitution(tmp_path: Path, path: str):
    """``cursor-agent`` auto-routes its own model: with no pin there is nothing to contradict.

    Whatever the CLI reports is recorded verbatim, and no mismatch is invented — only a *requested*
    model can be substituted.
    """
    fixture = mission(tmp_path, harness="cursor-agent", model=None)
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="cursor-agent",
            model="Cursor Grok 4.6")

    result = accept(path, fixture)
    lane = lane_record(fixture)

    assert lane["status"] == COMPLETED
    assert lane["evidence"]["model_requested"] is None
    assert lane["evidence"]["model_reported"] == "Cursor Grok 4.6"
    assert lane["evidence"]["identity_warning"] == "", "no pin means nothing to advise about"
    if path == "settle":
        assert result.returncode == 0, result.stderr


# --- defect 2: harness identity is the registry's, not the worker's -----------
@pytest.mark.parametrize("path", PATHS)
def test_two_spellings_of_one_cli_do_not_split_identity(tmp_path: Path, path: str):
    """A worker reporting ``cursor`` for a lane registered as ``cursor-agent`` is the same CLI."""
    fixture = mission(tmp_path, harness="cursor-agent", model="cursor-grok-4.6")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="cursor", model="cursor-grok-4.6")

    result = accept(path, fixture)
    lane = lane_record(fixture)

    assert lane["status"] == COMPLETED, lane.get("needs_review_reason")
    assert lane["evidence"]["harness"] == "cursor-agent", "the identity comes from the registry"
    assert lane["evidence"]["harness_reported"] == "cursor", "the worker's spelling, verbatim"
    assert lane["evidence"]["identity_warning"] == "", "an alias is not a disagreement"
    if path == "settle":
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", PATHS)
def test_a_provider_spelling_of_the_registered_cli_is_the_same_harness(tmp_path: Path, path: str):
    """``openai-codex`` is the provider name for the CLI registered as ``codex`` — one identity."""
    fixture = mission(tmp_path, harness="codex", model="gpt-5.6-sol")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="openai-codex",
            model="gpt-5.6-sol")

    result = accept(path, fixture)
    lane = lane_record(fixture)

    assert lane["status"] == COMPLETED, lane.get("needs_review_reason")
    assert lane["evidence"]["harness"] == "codex"
    assert lane["evidence"]["harness_reported"] == "openai-codex"
    assert lane["evidence"]["identity_warning"] == "", "a provider spelling is the same CLI"
    if path == "settle":
        assert result.returncode == 0, result.stderr


# --- the harness half: the registry decides, the self-report only advises ------
@pytest.mark.parametrize("path", PATHS)
def test_a_wrong_harness_self_report_is_accepted_with_a_warning(tmp_path: Path, path: str):
    """The live two-vendor defect: lane registered ``droid``, sidecar self-reported ``cursor-agent``.

    The binary was the registered one (verified in the journal, ``/home/logani/.local/bin/droid``), the
    work was correct and the lane's test passed — and the guard parked it on the agent's own guess about
    which CLI it was. oprun chose the binary from the registry, so that guess carries no information:
    the lane is accepted on its real evidence, with the disagreement recorded and printed.
    """
    fixture = mission(tmp_path, harness="droid", model=None)
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="cursor-agent", model=None)

    lane = assert_accepted(fixture, path, accept(path, fixture), "droid", "cursor-agent")

    assert lane["evidence"]["harness"] == "droid", "the identity comes from the registry"
    assert lane["evidence"]["harness_reported"] == "cursor-agent", "the worker's spelling, verbatim"
    assert "model" not in lane["evidence"]["identity_warning"], "no pin, so no model advisory"


@pytest.mark.parametrize("path", PATHS)
def test_a_harness_that_was_never_reported_is_no_longer_fatal(tmp_path: Path, path: str):
    """A sidecar that omits the harness is warned about, not parked: the registry id already won.

    Same policy as a wrong spelling — the self-report is advisory either way, because ``harness`` in
    the evidence is the lane's registry id and always was.
    """
    fixture = mission(tmp_path, harness="codex", model="gpt-5.6-sol")
    sidecar(fixture["worktree"], fixture["dispatch_id"], omit=("harness",))

    lane = assert_accepted(fixture, path, accept(path, fixture), "codex", "not reported")

    assert lane["evidence"]["harness"] == "codex"
    assert lane["evidence"]["harness_reported"] is None


# --- a matching identity is accepted, with the audit pair kept ---------------
@pytest.mark.parametrize("path", PATHS)
def test_a_matching_identity_is_accepted_with_both_raw_strings_recorded(tmp_path: Path, path: str):
    fixture = mission(tmp_path, harness="codex", model="gpt-5.6-sol")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="codex", model="gpt-5.6-sol")

    result = accept(path, fixture)
    lane = lane_record(fixture)
    evidence = lane["evidence"]

    assert lane["status"] == COMPLETED
    assert evidence["harness"] == "codex" and evidence["harness_reported"] == "codex"
    assert evidence["model_requested"] == "gpt-5.6-sol"
    assert evidence["model_reported"] == "gpt-5.6-sol"
    assert evidence["sidecar"].endswith(f"result.{fixture['dispatch_id']}.json")
    if path == "settle":
        assert result.returncode == 0, result.stderr


# --- the artifact-less lane: no reported identity, so nothing is invented ----
def test_settle_never_invents_an_identity_when_there_is_no_sidecar(tmp_path: Path):
    """No sidecar means no reported identity — the check cannot claim one on the worker's behalf.

    ``settle --accept`` is then decided by the controller's own re-run of ``test_cmd`` (the behaviour
    ``tests/test_approvals.py`` freezes), and the evidence records that nothing was reported.
    """
    fixture = mission(tmp_path, harness="codex", model="gpt-5.6-sol")

    result = accept("settle", fixture)
    lane = lane_record(fixture)

    assert result.returncode == 0, result.stderr
    assert lane["status"] == COMPLETED
    assert lane["evidence"]["model_reported"] is None
    assert lane["evidence"]["harness_reported"] is None


def test_advance_never_accepts_a_lane_without_an_artifact(tmp_path: Path):
    """The runner takes the other branch of the same rule: no artifact is no acceptance at all."""
    fixture = mission(tmp_path, harness="codex", model="gpt-5.6-sol")

    summary = accept("advance", fixture)

    assert summary["accepted"] == []
    assert lane_record(fixture)["status"] != COMPLETED


# --- defect 3: the provider spelling resolves -------------------------------
class TestHarnessAliases:
    """The frozen two-vendor command dispatches ``--harness openai-codex`` (the provider name) while
    the registry keys by CLI name (``codex``) — so the alias has to resolve, and to one identity."""

    def test_the_openai_codex_provider_spelling_resolves_to_the_codex_cli(self):
        assert oprun.HARNESS_ALIASES["openai-codex"] == "codex"
        assert oprun.canonical_harness("openai-codex") == "codex"
        assert harnesses.canonical_id("openai-codex") == "codex"

    def test_the_provider_spelling_builds_the_codex_command_line_with_the_pin_on_it(self):
        argv = oprun.build_worker_argv("openai-codex", model="gpt-5.6-sol",
                                       brief_path=Path("/tmp/brief.md"), brief_text="TASK")
        assert argv[:2] == ["codex", "exec"]
        assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
        assert argv[-1] == "TASK"

    def test_an_unknown_harness_is_still_refused(self):
        with pytest.raises(KeyError):
            oprun.canonical_harness("not-a-harness")


# --- the pin that cannot be corroborated: measured, not assumed ---------------
@pytest.mark.parametrize("path", PATHS)
def test_codex_model_behaviour_matches_its_measured_pin_verifiability(tmp_path: Path, path: str):
    """codex: the pin reaches the CLI, but nothing the CLI emits can corroborate it.

    MEASURED on this box (codex-cli 0.153.4), and written into the entry's ``notes``:
    ``codex exec --json -s workspace-write --model gpt-5.6-sol …`` emits exactly
    ``thread.started``, ``turn.started``, ``item.completed`` and ``turn.completed`` — **no event
    carries a model id** — while the pin demonstrably reaches the model layer (a bogus ``--model`` is
    rejected with a 400, and the on-disk rollout records ``turn_context.model == "gpt-5.6-sol"``).
    Asked for its own id under that pin, the agent answered ``"gpt-5.6-terra"``.

    So the expectation is read from the registry rather than hardcoded: the contract is
    ``pin_verifiable=False`` ⇒ a reported mismatch is advisory and the lane is accepted on its real
    evidence; ``pin_verifiable=True`` ⇒ the mismatch stays fatal. Flipping the property without
    re-measuring therefore flips this test's expectation with it.
    """
    fixture = mission(tmp_path, harness="codex", model="gpt-5.6-sol")
    sidecar(fixture["worktree"], fixture["dispatch_id"], harness="codex", model="gpt-6-astra")

    if harnesses.get("codex").pin_verifiable:
        lane = assert_parked(fixture, path, accept(path, fixture), "gpt-5.6-sol", "gpt-6-astra")
    else:
        lane = assert_accepted(fixture, path, accept(path, fixture), "gpt-5.6-sol", "gpt-6-astra")

    assert lane["evidence"]["model_requested"] == "gpt-5.6-sol"
    assert lane["evidence"]["model_reported"] == "gpt-6-astra", "the raw report is never repaired"
    assert lane["evidence"]["harness_reported"] == "codex"


def test_the_codex_pin_verifiability_was_measured_not_assumed():
    """The measurement, frozen where it can be re-checked: the entry says ``False`` and says why.

    If this fails, codex changed how it reports (or the entry was edited without running anything):
    re-run the command in ``notes`` and set the property from what it does.
    """
    harness = harnesses.get("codex")
    assert harness.pin_verifiable is False, "codex cannot corroborate a supplied pin (measured)"
    notes = harness.notes.lower()
    assert "pin_verifiable=false" in notes
    assert "no event carries a model id" in notes
    assert "gpt-5.6-terra" in notes, "the observed self-report must stay in the entry"


class TestPinVerifiabilityContract:
    """The property is declared on the dataclass, and no registry entry may inherit it silently."""

    def test_the_harness_dataclass_exposes_pin_verifiable_defaulting_to_strict(self):
        field = harnesses.Harness.__dataclass_fields__["pin_verifiable"]
        assert field.type in ("bool", bool), field.type
        assert field.default is True, "the default reading is the strict one"

    def test_every_registry_entry_has_it_set_explicitly(self):
        """A field a default can fill is a field that can rot: the source must set each one."""
        source = inspect.getsource(harnesses)
        for harness_id in harnesses.CANONICAL_IDS:
            entry = harnesses.HARNESSES[harness_id]
            assert isinstance(entry.pin_verifiable, bool), harness_id
            block = source.split(f'"{harness_id}": Harness(', 1)[1].split("\n    ),", 1)[0]
            assert "pin_verifiable=" in block, f"{harness_id} relies on the default"

    def test_only_a_measured_harness_is_marked_unverifiable(self):
        """The strict default (``True``) is what an unmeasured harness gets."""
        for harness_id in harnesses.CANONICAL_IDS:
            if harness_id == "codex":
                continue
            assert harnesses.get(harness_id).pin_verifiable is True, harness_id
