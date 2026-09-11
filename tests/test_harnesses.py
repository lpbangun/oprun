"""Registry contract tests: ids, routability, and the measured recipe facts per harness.

These are the checks that keep a routing mistake from reaching a live dispatch. They assert
*measured* properties (flags, terminators, the claude 403, the opencode dropped terminator),
not prose: if a note stops carrying a fact a controller relies on, that is a failure.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

# conftest.py covers the pytest entrypoint; this covers the direct one
# (`python3 tests/test_harnesses.py`), matching tests/test_ledger.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

from harnesses import (  # noqa: E402
    CANONICAL_IDS,
    HARNESSES,
    HARNESS_ALIASES,
    Harness,
    argv_for,
    canonical_id,
    get,
    model_pin_required,
    routable,
    witness_for,
)

EXPECTED_IDS = {"cursor-agent", "codex", "droid", "claude", "hermes", "pi", "opencode"}


class TestRegistryShape:
    def test_canonical_ids_are_exactly_the_expected_set(self):
        assert set(CANONICAL_IDS) == EXPECTED_IDS
        assert len(CANONICAL_IDS) == len(EXPECTED_IDS) == 7

    def test_registry_has_every_canonical_id_once_and_no_extras(self):
        assert set(HARNESSES) == EXPECTED_IDS
        assert len(HARNESSES) == len(EXPECTED_IDS)

    def test_each_entry_is_keyed_by_its_own_id(self):
        for key, harness in HARNESSES.items():
            assert key == harness.id
            assert key in CANONICAL_IDS

    def test_get_returns_the_keyed_entry(self):
        for harness_id in CANONICAL_IDS:
            assert get(harness_id) is HARNESSES[harness_id]
            assert isinstance(get(harness_id), Harness)

    def test_entries_are_frozen(self):
        with pytest.raises(Exception):
            HARNESSES["codex"].id = "something-else"  # type: ignore[misc]

    def test_every_entry_carries_a_label_binary_and_terminal_event(self):
        for harness in HARNESSES.values():
            assert harness.label
            assert harness.binary
            assert harness.terminal_event
            assert harness.notes
            assert harness.witness_strength in {"strong", "none"}


class TestUnknownIds:
    def test_get_unknown_id_raises_key_error(self):
        with pytest.raises(KeyError):
            get("nope")

    def test_lookup_helpers_refuse_unknown_ids(self):
        for call in (model_pin_required, routable, witness_for):
            with pytest.raises(KeyError):
                call("nope")


class TestRoutability:
    def test_claude_is_not_routable_unpinned(self):
        assert routable("claude") is False

    def test_claude_requires_a_model_pin(self):
        assert model_pin_required("claude") is True

    def test_claude_is_routable_once_a_pin_is_supplied(self):
        assert routable("claude", "claude-sonnet-4-6") is True

    def test_blank_pin_does_not_count_as_a_pin(self):
        assert routable("claude", "") is False
        assert routable("claude", "   ") is False

    def test_every_other_harness_is_routable(self):
        for harness_id in CANONICAL_IDS:
            if harness_id == "claude":
                continue
            assert routable(harness_id) is True, harness_id
            assert model_pin_required(harness_id) is False, harness_id

    def test_pinning_a_harness_that_does_not_need_one_stays_routable(self):
        assert routable("codex", "gpt-5.6-sol") is True


class TestWitnessStrength:
    def test_opencode_witness_is_none(self):
        assert get("opencode").witness_strength == "none"
        assert witness_for("opencode") == "none"

    def test_opencode_notes_name_the_dropped_terminator(self):
        notes = get("opencode").notes.lower()
        assert "terminator" in notes
        assert "drop" in notes
        assert "#26855" in notes and "#31435" in notes

    def test_claude_witness_is_none_unpinned_and_strong_when_pinned(self):
        assert get("claude").witness_strength == "none"
        assert witness_for("claude") == "none"
        assert witness_for("claude", "claude-sonnet-4-6") == "strong"

    def test_claude_notes_warn_that_subtype_alone_is_a_trap(self):
        notes = get("claude").notes.lower()
        assert "subtype" in notes
        assert "is_error" in notes
        assert "trap" in notes

    def test_the_five_strong_witnesses_are_strong(self):
        for harness_id in ("cursor-agent", "codex", "droid", "hermes", "pi"):
            assert get(harness_id).witness_strength == "strong", harness_id
            assert witness_for(harness_id) == "strong", harness_id


class TestRecipes:
    def test_every_recipe_is_non_empty(self):
        for harness in HARNESSES.values():
            assert harness.unattended_argv, harness.id
            assert all(part for part in harness.unattended_argv), harness.id

    def test_no_recipe_is_interactive_only(self):
        # A recipe with no unattended mode blocks on an approval prompt and looks idle, so
        # every entry must name the flag (or subcommand) that makes it run to completion.
        unattended_marker = {
            "cursor-agent": "--yolo",
            "codex": "exec",
            "droid": "--auto",
            "claude": "-p",
            "hermes": "-p",
            "pi": "-p",
            "opencode": "run",
        }
        assert set(unattended_marker) == set(CANONICAL_IDS)
        for harness_id, marker in unattended_marker.items():
            assert marker in get(harness_id).unattended_argv, harness_id

    def test_cursor_agent_recipe_is_fully_unattended(self):
        argv = get("cursor-agent").unattended_argv
        assert "--yolo" in argv or "--force" in argv
        assert "--print" in argv or "-p" in argv
        assert "--output-format" in argv

    def test_cursor_agent_notes_say_it_auto_routes_its_model(self):
        notes = get("cursor-agent").notes.lower()
        assert "auto-route" in notes
        assert "model" in notes

    def test_codex_notes_name_the_missing_turn_cap_and_the_external_timeout(self):
        notes = get("codex").notes.lower()
        assert "--max-turns" in notes
        assert "no --max-turns" in notes
        assert "timeout" in notes

    def test_codex_recipe_uses_exec_json_workspace_write(self):
        argv = get("codex").unattended_argv
        assert argv[:2] == ("exec", "--json")
        assert "workspace-write" in argv

    def test_droid_notes_make_the_exit_code_authoritative(self):
        notes = get("droid").notes.lower()
        assert "exit code" in notes
        assert "--auto" in get("droid").unattended_argv

    def test_pi_binary_is_an_absolute_path_and_notes_say_not_on_path(self):
        harness = get("pi")
        assert os.path.isabs(harness.binary), harness.binary
        assert Path(harness.binary).name == "pi"
        assert "not on path" in harness.notes.lower()

    def test_hermes_notes_need_the_real_home(self):
        assert "HOME=/home/logani" in get("hermes").notes
        assert "-p" in get("hermes").unattended_argv

    def test_argv_for_prefixes_the_binary_and_appends_the_pin(self):
        assert argv_for("codex")[0] == "codex"
        assert argv_for("pi")[0] == "/home/logani/.hermes/node/bin/pi"
        assert argv_for("claude", model_pin="claude-sonnet-4-6") == (
            "claude", "-p", "--output-format", "json", "--model", "claude-sonnet-4-6",
        )

    def test_argv_for_refuses_an_unpinned_claude(self):
        with pytest.raises(KeyError):
            argv_for("claude")


class TestTheModelPinReachesTheCommandLine:
    """A pin the CLI never received is a pin the ledger asserted and the lane never applied.

    The measured defect: the flag was appended only for a harness with ``requires_model_pin`` —
    ``claude`` — so an operator's ``--model`` was silently dropped for every other harness while
    ``model_requested`` was still recorded. That is a silent substitution, produced by this module.
    """

    def test_a_supplied_pin_is_passed_for_every_harness(self):
        for harness_id in CANONICAL_IDS:
            argv = argv_for(harness_id, model_pin="some-model-1")
            assert "--model" in argv, harness_id
            assert argv[argv.index("--model") + 1] == "some-model-1", harness_id

    def test_the_codex_pin_is_passed_ahead_of_the_brief(self):
        assert argv_for("codex", model_pin="gpt-5.6-sol", extra=("BRIEF",)) == (
            "codex", "exec", "--json", "-s", "workspace-write",
            "--model", "gpt-5.6-sol", "BRIEF",
        )

    def test_pinning_never_rewrites_the_unattended_recipe(self):
        recipe = get("cursor-agent").unattended_argv
        argv = argv_for("cursor-agent", model_pin="cursor-grok-4.6")
        assert argv[:1 + len(recipe)] == ("cursor-agent", *recipe)
        assert argv[1 + len(recipe):] == ("--model", "cursor-grok-4.6")

    def test_a_blank_pin_counts_as_no_pin_and_is_not_passed(self):
        # routable()'s own convention, kept: a blank pin is not a pin, so nothing is claimed.
        assert "--model" not in argv_for("codex", model_pin="   ")
        assert "--model" not in argv_for("codex", model_pin="")
        assert argv_for("codex", model_pin=None) == ("codex", *get("codex").unattended_argv)

    def test_requires_model_pin_still_means_only_claude_cannot_run_unpinned(self):
        for harness_id in CANONICAL_IDS:
            assert model_pin_required(harness_id) is (harness_id == "claude"), harness_id

    def test_every_entry_names_the_flag_that_carries_a_pin(self):
        for harness in HARNESSES.values():
            assert harness.model_flag == "--model", harness.id

    def test_a_harness_with_no_model_flag_refuses_the_pin_rather_than_dropping_it(
            self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(HARNESSES, "codex",
                            dataclasses.replace(HARNESSES["codex"], model_flag=""))
        with pytest.raises(KeyError) as excinfo:
            argv_for("codex", model_pin="gpt-5.6-sol")
        assert "gpt-5.6-sol" in str(excinfo.value), "the refusal must name the pin it refused"


class TestCanonicalResolution:
    """One table for harness identity: an accepted spelling resolves, anything else is loud."""

    def test_accepted_spellings_resolve_to_the_registry_id(self):
        assert canonical_id("cursor") == "cursor-agent"
        assert canonical_id("claude-code") == "claude"
        assert canonical_id("openai-codex") == "codex"

    def test_a_registry_id_resolves_to_itself(self):
        for harness_id in CANONICAL_IDS:
            assert canonical_id(harness_id) == harness_id

    def test_an_unknown_id_is_a_hard_error(self):
        with pytest.raises(KeyError):
            canonical_id("not-a-harness")

    def test_the_alias_table_is_the_registrys_and_maps_only_to_real_ids(self):
        assert set(HARNESS_ALIASES) >= {"cursor", "claude-code", "openai-codex"}
        for alias, harness_id in HARNESS_ALIASES.items():
            assert harness_id in HARNESSES, alias
