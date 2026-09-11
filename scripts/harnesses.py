"""oprun harness registry — unattended recipe + completion witness per coding-agent CLI.

Routing a unit of work to a harness is only safe if two things are explicit per CLI: the
**unattended recipe** (the exact argv that makes it run with no human present) and the
**completion witness** (how its output says it finished). A CLI with no unattended flag
does not fail loudly — it blocks on an approval prompt and looks idle, which is
indistinguishable from a long think. This module exists so that cannot happen by accident:
it is the only place a harness id gets turned into a command line, and unknown ids are a
hard error rather than a guess.

Every entry below was measured on this box on 2026-09-11. Nothing here is inferred from
documentation, and no field may be filled in with a flag nobody ran. Data first: this
module holds no behaviour beyond lookups, and it never executes a harness.

Stdlib only (:mod:`dataclasses`), no sibling imports — the peer lane that owns
``scripts/probe.py`` consumes this registry, it is not imported by it.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Canonical harness ids, in registry order. Frozen string set: a lane's ``harness`` field
#: is set at dispatch from these ids, and a worker's self-report never overrides it.
CANONICAL_IDS: tuple[str, ...] = (
    "cursor-agent",
    "codex",
    "droid",
    "claude",
    "hermes",
    "pi",
    "opencode",
)


@dataclass(frozen=True)
class Harness:
    """One coding-agent CLI: how to run it unattended, and how its finish is witnessed.

    ``unattended_argv`` may contain placeholder tokens in angle brackets (``<profile>``,
    ``<brief>``, ``<low|medium|high>``) that the caller substitutes; they are part of the
    measured recipe, kept literal so the recipe is readable rather than pre-formatted.

    ``witness_strength`` is the *static* strength of the shipped default configuration:
    ``"strong"`` means process exit plus a machine-readable terminal event plus a sidecar
    artifact the controller can re-read; ``"none"`` means the default configuration yields
    no terminator a controller may trust on its own (opencode always, claude while
    unpinned). :func:`witness_for` re-evaluates claude once a pin is supplied.
    """

    id: str                      # one of CANONICAL_IDS
    label: str                   # human name, e.g. "Cursor Agent"
    binary: str                  # how it is invoked; note when it is NOT on PATH
    unattended_argv: tuple[str, ...]   # the flags that make it run with no human
    output_format: str           # the machine-readable format, or "" if none
    terminal_event: str          # what its success terminator looks like
    witness_strength: str        # "strong" | "none"
    requires_model_pin: bool     # True when it cannot run without an explicit model
    notes: str


HARNESSES: dict[str, Harness] = {
    "cursor-agent": Harness(
        id="cursor-agent",
        label="Cursor Agent",
        binary="cursor-agent",
        unattended_argv=("-p", "--output-format", "json", "--yolo", "--trust"),
        output_format="json",
        terminal_event='{"type":"result","subtype":"success|error","is_error":bool}',
        witness_strength="strong",
        requires_model_pin=False,
        notes=(
            "-p runs unattended with full write+shell access (--yolo skips the approval "
            "prompt, --trust skips the workspace-trust prompt) — so the worktree, not the "
            "sandbox, is the boundary. It auto-routes its model: the resolved model is "
            "chosen by the CLI, so a lane MUST record the model it reports, never assume "
            "the pin it asked for. Witness: the result event's is_error plus exit code."
        ),
    ),
    "codex": Harness(
        id="codex",
        label="Codex CLI",
        binary="codex",
        unattended_argv=("exec", "--json", "-s", "workspace-write"),
        output_format="json",
        terminal_event="turn.completed / turn.failed",
        witness_strength="strong",
        requires_model_pin=False,
        notes=(
            "exec --json streams structured turn events; turn.completed is the terminator. "
            "There is no --max-turns flag, so a run has no self-imposed cap: wrap it in an "
            "external timeout and treat a missing turn.completed as a stall to escalate, "
            "never as something to keep waiting on. -s workspace-write is what makes it "
            "able to edit the worktree unattended."
        ),
    ),
    "droid": Harness(
        id="droid",
        label="Droid",
        binary="droid",
        unattended_argv=("exec", "-o", "json", "--auto", "<low|medium|high>"),
        output_format="json",
        terminal_event='{"type":"result",...}',
        witness_strength="strong",
        requires_model_pin=False,
        notes=(
            "--auto <level> is what makes it unattended; without it the run asks for "
            "approval and looks idle. The exit code is the documented authoritative "
            "signal — the result event is corroboration, not the verdict."
        ),
    ),
    "claude": Harness(
        id="claude",
        label="Claude Code",
        binary="claude",
        unattended_argv=("-p", "--output-format", "json"),
        output_format="json",
        terminal_event='{"type":"result","subtype":...,"is_error":bool}',
        witness_strength="none",
        requires_model_pin=True,
        notes=(
            "requires_model_pin: its default model 403s on this box, so an unpinned lane is "
            "not routable at all and is rejected at dispatch — supply an explicit --model. "
            "And subtype alone is a trap: measured on this box, subtype:\"success\" was "
            "emitted WITH is_error:true and exit 1. Exit code plus is_error are "
            "authoritative; subtype is a label, not a verdict."
        ),
    ),
    "hermes": Harness(
        id="hermes",
        label="Hermes",
        binary="hermes",
        unattended_argv=("-p", "<profile>", "chat", "--query-file", "<brief>"),
        output_format="",
        terminal_event="exit code + session id",
        witness_strength="strong",
        requires_model_pin=False,
        notes=(
            "Needs HOME=/home/logani, or the profile lookup resolves somewhere else. -p "
            "<profile> picks the profile and --query-file carries the brief, so an "
            "unattended lane is one process per unit. The session id it prints is the handle "
            "for reattaching or relaying; the exit code is the verdict."
        ),
    ),
    "pi": Harness(
        id="pi",
        label="Pi",
        binary="/home/logani/.hermes/node/bin/pi",
        unattended_argv=("-p",),
        output_format="",
        terminal_event="exit code",
        witness_strength="strong",
        requires_model_pin=False,
        notes=(
            "Not on PATH: the absolute binary path is required, so a recipe that spells it "
            "'pi' fails to launch. Also needs HOME=/home/logani. -p is the unattended "
            "print/prompt mode; the exit code is the only terminator, so pair the lane with a "
            "sidecar artifact it must write."
        ),
    ),
    "opencode": Harness(
        id="opencode",
        label="OpenCode",
        binary="opencode",
        unattended_argv=("run", "--format", "json"),
        output_format="json",
        terminal_event="step_finish",
        witness_strength="none",
        requires_model_pin=False,
        notes=(
            "Known to drop its terminator (opencode issues #26855 and #31435): the "
            "step_finish step is not reliably emitted, so a run can complete correctly and "
            "still look unfinished. Never trust its stdout alone — accept only on exit code "
            "plus a sidecar artifact the lane wrote, and treat a missing step_finish as an "
            "unreliable-witness case rather than a failure."
        ),
    ),
}


def get(harness_id: str) -> Harness:
    """The registry entry for ``harness_id``.

    Raises :class:`KeyError` for an unknown id: an unrecognised harness means the dispatch
    is misconfigured, and silently falling back to "some CLI" is the failure this registry
    exists to prevent.
    """
    try:
        return HARNESSES[harness_id]
    except KeyError:
        raise KeyError(
            f"unknown harness {harness_id!r}: expected one of "
            f"{', '.join(CANONICAL_IDS)}"
        ) from None


def model_pin_required(harness_id: str) -> bool:
    """True when the harness cannot run without an explicit model pin (claude)."""
    return get(harness_id).requires_model_pin


def routable(harness_id: str, model_pin: str | None = None) -> bool:
    """Whether this harness may be given a lane, given the pin the caller would supply.

    ``False`` when the harness requires a model pin and no usable pin is on hand — an
    unpinned ``claude`` lane 403s on this box, so routing to it would burn a dispatch to
    learn nothing. A blank or whitespace pin counts as no pin. Every harness that does not
    require a pin is routable as-is.
    """
    harness = get(harness_id)
    if not harness.requires_model_pin:
        return True
    return bool(model_pin and model_pin.strip())


def witness_for(harness_id: str, model_pin: str | None = None) -> str:
    """The witness strength actually in force, after the pin question is answered.

    Static :attr:`Harness.witness_strength` is the default-configuration value, and only the
    *pin* question can upgrade it: ``claude`` ships as ``"none"`` because the unpinned
    default is unroutable, and once a real pin is supplied its result event plus exit code
    become a strong witness. ``opencode`` stays ``"none"`` either way — its terminator is
    dropped, which no pin can fix (opencode #26855, #31435) — and so does every harness
    whose weak witness is not a pinning problem.
    """
    harness = get(harness_id)
    if (harness.requires_model_pin and harness.witness_strength == "none"
            and routable(harness_id, model_pin)):
        return "strong"
    return harness.witness_strength


def argv_for(harness_id: str, *, model_pin: str | None = None,
             extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The exact argv for an unattended run: binary, recipe, pin (if required), extras.

    Placeholders in the recipe (``<profile>``, ``<brief>``, ``<low|medium|high>``) are
    literal — the caller substitutes them. A harness that requires a pin but was given
    none raises :class:`KeyError` through :func:`routable`'s contract, i.e. it refuses
    rather than launching something that will 403.
    """
    harness = get(harness_id)
    if harness.requires_model_pin and not routable(harness_id, model_pin):
        raise KeyError(
            f"harness {harness_id!r} requires a model pin; none supplied"
        )
    argv = (harness.binary, *harness.unattended_argv)
    if model_pin and harness.requires_model_pin:
        argv = (*argv, "--model", model_pin)
    return (*argv, *extra)
