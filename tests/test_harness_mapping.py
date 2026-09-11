"""C1 guard: no shipped harness may reach ``probe`` unmapped.

``scripts/harnesses.py`` builds recipes from a registry whose ids are ``probe.HARNESS_IDS``. If a
harness id can be dispatched but has no terminal-event parser, its stdout cannot be interpreted at
all — and a missing parser must never degrade into an implicit pass. This file keeps the two sets
equal and the parser contract enforced.
"""
from __future__ import annotations

import pytest

import probe

CANARY_TRAP_STDOUT = (
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":403,'
    '"result":"Failed to authenticate. API Error: 403 Access to model denied."}'
)

DOCUMENTED_KEYS = {"present", "ok", "detail"}


def test_every_registry_harness_has_a_parser() -> None:
    """Coverage is exact: no harness unmapped, no parser for a harness that does not exist."""
    assert set(probe.TERMINAL_EVENT_PARSERS) == set(probe.HARNESS_IDS)


def test_parser_table_matches_harness_ids_order_independently() -> None:
    for harness in probe.HARNESS_IDS:
        assert harness in probe.TERMINAL_EVENT_PARSERS


@pytest.mark.parametrize("harness", probe.HARNESS_IDS)
def test_parser_is_callable_and_returns_the_documented_shape(harness: str) -> None:
    parser = probe.TERMINAL_EVENT_PARSERS[harness]
    assert callable(parser)
    result = parser("")
    assert isinstance(result, dict)
    assert set(result) == DOCUMENTED_KEYS
    assert isinstance(result["present"], bool)
    assert isinstance(result["ok"], bool)
    assert isinstance(result["detail"], str)
    assert result["detail"]


@pytest.mark.parametrize("harness", probe.HARNESS_IDS)
def test_empty_stdout_is_never_ok(harness: str) -> None:
    """Absence of a terminal event is never a pass, for every harness."""
    result = probe.parse_terminal_event(harness, "")
    assert result["present"] is False
    assert result["ok"] is False


@pytest.mark.parametrize("harness", probe.HARNESS_IDS)
def test_dispatch_helper_agrees_with_the_table(harness: str) -> None:
    assert probe.parse_terminal_event(harness, "") == probe.TERMINAL_EVENT_PARSERS[harness]("")


@pytest.mark.parametrize("harness", probe.HARNESS_IDS)
def test_no_harness_accepts_the_canary_trap(harness: str) -> None:
    """``is_error:true`` with exit 1 must never parse as ok, whatever the harness."""
    assert probe.parse_terminal_event(harness, CANARY_TRAP_STDOUT)["ok"] is False


def test_unknown_harness_is_loud() -> None:
    with pytest.raises(KeyError):
        probe.parse_terminal_event("not-a-harness", "{}")
