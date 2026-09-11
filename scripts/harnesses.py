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
module holds no behaviour beyond lookups and two pure comparisons (:func:`identity_reason`
— the one rule that can park a lane — and :func:`identity_warning`, which can only advise),
and it never executes a harness.

**What the no-silent-substitution guard actually is.** The **model** pin is the guard: a lane
pinned to one model whose worker reports another is parked, and both raw strings are kept. But the
guard is only as strong as a harness's ability to report the model it ran, so each entry declares
:attr:`Harness.pin_verifiable` — ``False`` where the pin reaches the CLI but nothing in the
harness's own output can corroborate it, and then a reported-model mismatch is *advice*, not
evidence. **Harness identity is the other half, and it is not a guard at all:** oprun picks the
binary from this registry, so an agent's opinion about which CLI it is carries no routing
information and is demonstrably unreliable — a lane launched as ``droid`` (binary verified in the
journal) can self-report ``cursor-agent``, and parking correct work on that is a false-negative
generator. So the registry id always wins, the worker's spelling is recorded verbatim, and any
disagreement is recorded and printed as :func:`identity_warning` — never enforced.

Stdlib plus one sibling import: :mod:`ledger`'s ``normalize_model`` is the single definition of
"these two strings name the same model", so the comparison here cannot drift from the comparison
the ledger documents. The peer lane that owns ``scripts/probe.py`` consumes this registry; it is
not imported by it.
"""
from __future__ import annotations

from dataclasses import dataclass

from ledger import normalize_model

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

#: Accepted spellings that unambiguously mean one registry id. The ledger always records the
#: canonical id, so two spellings of one CLI can never split its identity — and every comparison
#: that mentions a harness resolves through this table (never through raw string equality), from
#: :func:`canonical_id` at dispatch down to the advisory :func:`identity_warning`.
#: ``openai-codex`` is the provider name a lane can be dispatched by (the frozen two-vendor
#: benchmark's ``--harness openai-codex``) for the CLI whose registry id is ``codex``.
HARNESS_ALIASES: dict[str, str] = {
    "cursor": "cursor-agent",
    "claude-code": "claude",
    "openai-codex": "codex",
}


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

    ``model_flag`` is the flag that carries an explicit model pin — the flag measured on this
    box for every entry that has one. It is why :func:`argv_for` hands a supplied pin to the
    CLI for *every* harness, not only for the ones that cannot run without one: passing the pin
    only where it was mandatory silently let a CLI run its own default while the ledger asserted
    the requested model. A CLI with no model flag at all declares ``model_flag=""``, and then a
    pin is **refused** (``KeyError``) rather than dropped.

    ``pin_verifiable`` says whether that pin can be *checked* — whether anything the harness itself
    emits names the model that ran. Passing a pin and being able to verify it are different
    properties, and conflating them is how a guard turns into a false-negative generator: where the
    pin provably reaches the CLI but no trustworthy model report comes back, a "mismatch" cannot be
    told apart from the agent guessing at its own name, so it is advisory
    (:func:`identity_warning`) instead of fatal. It defaults to ``True`` — the strict reading —
    and only a measured harness may lower it. Declared last because every field above is
    mandatory for the entry to be routable at all, while this one is a measurement that can be
    revisited without touching the recipe.
    """

    id: str                      # one of CANONICAL_IDS
    label: str                   # human name, e.g. "Cursor Agent"
    binary: str                  # how it is invoked; note when it is NOT on PATH
    unattended_argv: tuple[str, ...]   # the flags that make it run with no human
    output_format: str           # the machine-readable format, or "" if none
    terminal_event: str          # what its success terminator looks like
    witness_strength: str        # "strong" | "none"
    requires_model_pin: bool     # True when it cannot run without an explicit model
    model_flag: str              # the flag that carries a model pin; "" = this CLI takes none
    notes: str
    pin_verifiable: bool = True  # can a supplied pin be corroborated from the harness's own output?


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
        model_flag="--model",
        pin_verifiable=True,
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
        model_flag="--model",
        # Measured, not assumed — see the notes below: the pin reaches the CLI, but nothing the
        # exec stream emits names the model that ran, so a reported mismatch proves nothing.
        pin_verifiable=False,
        notes=(
            "exec --json streams structured turn events; turn.completed is the terminator. "
            "There is no --max-turns flag, so a run has no self-imposed cap: wrap it in an "
            "external timeout and treat a missing turn.completed as a stall to escalate, "
            "never as something to keep waiting on. -s workspace-write is what makes it "
            "able to edit the worktree unattended. "
            "pin_verifiable=False, MEASURED on this box (codex-cli 0.153.4): "
            "`codex exec --json -s workspace-write --model gpt-5.6-sol \"reply with the model "
            "id you are running as\"` emits exactly thread.started, turn.started, item.completed "
            "and turn.completed — NO event carries a model id — while the pin demonstrably "
            "reaches the model layer (a bogus --model is rejected with a 400, and the on-disk "
            "rollout records turn_context.model == \"gpt-5.6-sol\"). Asked for its own id under "
            "that pin, the agent answered \"gpt-5.6-terra\", i.e. the worker's model self-report "
            "is a guess and can be wrong in both directions. So a reported/model-pin mismatch "
            "cannot distinguish a substitution from a naming difference and is advisory "
            "(identity_warning), never a park; a pin that is not reported at all is still "
            "needs_review, and nothing here weakens the harness-identity rule (the registry "
            "decides that, and the worker's harness self-report is never trusted)."
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
        model_flag="--model",
        pin_verifiable=True,
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
        model_flag="--model",
        pin_verifiable=True,
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
        model_flag="--model",
        pin_verifiable=True,
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
        model_flag="--model",
        pin_verifiable=True,
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
        # Not installed on this box, so its pin flag is the one its `run` interface documents
        # rather than one measured here — but it is still *passed*: the ledger never asserts a
        # pin it did not hand to the CLI.
        model_flag="--model",
        pin_verifiable=True,
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


def canonical_id(harness_id: str) -> str:
    """The registry id for ``harness_id``: an accepted spelling resolves, an unknown id is loud.

    Raises :class:`KeyError` for an id that is neither a registry id nor an accepted spelling in
    :data:`HARNESS_ALIASES`. This is the only resolution path — dispatch, the acceptance paths and
    the advisory :func:`identity_warning` all go through it, so ``cursor`` and ``cursor-agent`` are
    one CLI everywhere and a typo is never routed somewhere plausible.
    """
    canonical = HARNESS_ALIASES.get(harness_id, harness_id)
    get(canonical)          # raises KeyError for an unknown id: never a silent fallback
    return canonical


def _canonical_id_or_none(harness_id: str) -> str | None:
    """``canonical_id`` for a spelling a *worker* reported, or ``None`` when it is unknown.

    A worker's spelling is not validated input: it is a claim to compare against the lane's
    designation, and the comparison it feeds (:func:`identity_warning`) can only ever produce a note,
    so "the registry does not know this spelling" is an answer (an unresolvable claim, noted as
    such), not a crash — and never a park, because the registry id is what decided the binary either
    way.
    """
    try:
        return canonical_id(harness_id)
    except KeyError:
        return None


def _pin_verifiable(harness_id: str) -> bool:
    """Whether a pin supplied to ``harness_id`` can be corroborated by what that harness reports.

    An id this module cannot resolve (blank, or not in the registry) gets the strict reading
    (``True``): a spelling the registry does not know is never a reason to relax the one guard
    that still bites.
    """
    if not harness_id:
        return True
    try:
        return get(canonical_id(harness_id)).pin_verifiable
    except KeyError:
        return True


def identity_reason(*, harness: str | None, model_requested: str | None,
                    harness_reported: object, model_reported: object) -> str:
    """Why a worker's reported identity does not match the lane's designation; ``""`` when it does.

    This is the no-silent-substitution rule, and it is enforced on the **acceptance path** — the
    ``settle`` verb and the ``advance`` runner, both of which import it from here — not only in a
    unit test. Exactly one thing can park a lane here, and it is the **model**:

    * the **model** is compared with ``ledger.normalize_model`` (casefold, then ``[-_ .]`` dropped):
      ``"Cursor Grok 4.6"`` and ``"cursor-grok-4.6"`` are one model, and two genuinely different
      pins are not — a naive string compare would false-positive the first pair;
    * a pinned lane whose sidecar **does not report a model at all** cannot be shown to hold, so it
      parks — regardless of harness, because "the pin was applied" would otherwise be asserted on no
      evidence;
    * a reported model that differs from the pin parks **only where the harness can be believed**:
      :attr:`Harness.pin_verifiable` says whether anything the CLI emits names the model that ran.
      Where it cannot (codex: the ``exec --json`` stream carries no model id, and the agent's own
      answer is a wrong guess), a mismatch is not evidence of a substitution — the difference may be
      the CLI's naming, not the model — so it is advisory (:func:`identity_warning`) and the lane is
      decided on its real evidence instead;
    * a lane with **no** model designation (``--model`` never passed, so the CLI auto-routes) has
      nothing to contradict — whatever it reports is recorded as-is and is not a substitution;
    * the **harness** is *not* a parking rule. The registry chose the binary, so a worker's opinion
      about which CLI it is has no information content: the ``harness``/``harness_reported``
      arguments are accepted (one observation pair for both acceptance paths, and the one place a
      future rule would look) and compared by :func:`identity_warning` only;
    * the raw strings are never rewritten, repaired or normalised into each other: callers store
      both, exactly as reported, whichever way the comparison went.
    """
    registered = "" if harness is None else str(harness).strip()
    requested = "" if model_requested is None else str(model_requested).strip()
    if not requested:
        return ""
    reported_model = "" if model_reported is None else str(model_reported).strip()
    if not reported_model:
        return (f"model not reported: lane is pinned to {model_requested!r} but the sidecar "
                f"reports model={model_reported!r}, so the pin cannot be shown to hold")
    if normalize_model(requested) == normalize_model(reported_model):
        return ""
    if not _pin_verifiable(registered):
        # The comparison cannot establish anything for this harness, so it must not fail the lane;
        # identity_warning records it for the reader.
        return ""
    return (f"model substitution: lane requested {model_requested!r} but the sidecar reports "
            f"{model_reported!r}")


def identity_warning(*, harness: str | None, model_requested: str | None,
                     harness_reported: object, model_reported: object) -> str:
    """Advisory notes about a worker's *self-report*; ``""`` when there is nothing to say.

    **This function never influences acceptance.** It exists because the honest output of a
    comparison that cannot decide anything is a note, not a verdict:

    * a reported harness that disagrees with the registry id (after alias canonicalisation), or that
      is not reported at all, is recorded as a warning — the registry decided the binary and the
      registry wins, so this can only ever be a signal for the reader. A lane that ran the right CLI
      while self-reporting the wrong one (measured live: registered ``droid``, binary verified in the
      journal, sidecar said ``cursor-agent``) is accepted on its real evidence, with the disagreement
      written down;
    * a reported model that disagrees with a pin on a harness whose ``pin_verifiable`` is ``False``
      is a warning for the same reason: the comparison cannot tell a substitution from a naming
      difference. A pin that was never reported is *not* in this category — that stays a park.
    """
    notes: list[str] = []
    registered = "" if harness is None else str(harness).strip()
    if registered:
        reported_harness = "" if harness_reported is None else str(harness_reported).strip()
        registered_id = _canonical_id_or_none(registered) or registered
        if not reported_harness:
            notes.append(
                f"harness not reported: lane is registered as {registered!r} but the sidecar "
                f"reports harness={harness_reported!r}; identity comes from the registry, so this "
                f"is advisory only"
            )
        else:
            reported_id = _canonical_id_or_none(reported_harness) or reported_harness
            if reported_id.casefold() != registered_id.casefold():
                notes.append(
                    f"harness self-report disagrees: lane is registered as {registered!r} but the "
                    f"sidecar reports harness {harness_reported!r} — oprun chose the binary from "
                    f"the registry, so this is advisory only"
                )
    requested = "" if model_requested is None else str(model_requested).strip()
    reported_model = "" if model_reported is None else str(model_reported).strip()
    if (requested and reported_model and not _pin_verifiable(registered)
            and normalize_model(requested) != normalize_model(reported_model)):
        notes.append(
            f"model self-report disagrees: lane is pinned to {model_requested!r} but the sidecar "
            f"reports {model_reported!r}; harness {registered!r} cannot corroborate a supplied pin "
            f"(pin_verifiable=False), so this cannot be told apart from a naming difference — "
            f"advisory only"
        )
    return "; ".join(notes)


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
    """The exact argv for an unattended run: binary, recipe, pin (if any), extras.

    Placeholders in the recipe (``<profile>``, ``<brief>``, ``<low|medium|high>``) are
    literal — the caller substitutes them. A harness that requires a pin but was given
    none raises :class:`KeyError` through :func:`routable`'s contract, i.e. it refuses
    rather than launching something that will 403.

    A supplied pin is passed to the CLI through :attr:`Harness.model_flag` for **every**
    harness, not only for the ones that cannot run without one. ``requires_model_pin`` means
    "cannot run at all without a pin" (claude's default model 403s) and nothing else: gating the
    flag on it dropped an operator's ``--model`` for every other harness, so the lane ran the
    CLI's own default while the ledger recorded the requested model — a pin asserted but never
    applied. A blank or whitespace pin counts as no pin, exactly as :func:`routable` treats it.

    A CLI that genuinely takes no model flag declares ``model_flag=""``, and then a pin is
    **refused** (``KeyError``) rather than dropped: never a quiet no-op.
    """
    harness = get(harness_id)
    if harness.requires_model_pin and not routable(harness_id, model_pin):
        raise KeyError(
            f"harness {harness_id!r} requires a model pin; none supplied"
        )
    argv = (harness.binary, *harness.unattended_argv)
    if model_pin and model_pin.strip():
        if not harness.model_flag:
            raise KeyError(
                f"harness {harness_id!r} takes no model flag (model_flag is empty): "
                f"refusing to drop the pin {model_pin!r}"
            )
        argv = (*argv, harness.model_flag, model_pin)
    return (*argv, *extra)
