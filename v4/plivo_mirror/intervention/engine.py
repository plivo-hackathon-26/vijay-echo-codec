"""Intervention engine — turns a violating ``Verdict`` into a real,
grounded corrected answer.

Flow:
  1. **Deflection filler** — the verdict's agent-voice line, spoken first
     to cover latency. It is NOT the answer.
  2. **Structured violation** (state can answer): template the corrected
     reply from validated state — NO LLM.
     **Open violation**: re-prompt the main LLM with the correction packet.
  3. **Re-verify** the candidate through the speech guard (and a
     pink-elephant echo check). Accept on pass; otherwise regenerate.
  4. **Cap retries** (default 2). On non-convergence, escalate via
     ``build_handoff``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable

from plivo_mirror.contracts import Guard, TurnContext, Verdict
from plivo_mirror.intervention.packet import build_packet, echoes
from plivo_mirror.intervention.regenerate import ReplyGenerator
from plivo_mirror.runtime.escalation import HandoffContext, build_handoff

# Spoken when a correction won't converge and we hand off to a human.
ESCALATION_LINE = (
    "I want to make sure you're taken care of — let me connect you with "
    "someone who can help directly."
)


def deflection_filler(verdict: Verdict) -> str:
    """The agent-voice deflection line, available BEFORE any regeneration.
    Spoken first to cover the latency of producing the grounded answer."""
    return verdict.spoken_correction or "Let me make sure I have that right."


@dataclass
class InterventionResult:
    filler: str                      # spoken first (deflection)
    answer: str                      # grounded corrected answer (spoken second)
    escalated: bool = False
    handoff: HandoffContext | None = None
    attempts: int = 0


def template_corrected_reply(context: TurnContext) -> str:
    """Structured correction: read the confirmed intent back from state.
    Returns ``""`` when state can't answer (→ open/regeneration path)."""
    intent = context.state.confirmed_intent
    return f"Got it — {intent}. Anything else?" if intent else ""


def _escalate(filler: str, context: TurnContext, attempts: int) -> InterventionResult:
    return InterventionResult(
        filler=filler,
        answer="",
        escalated=True,
        handoff=build_handoff(context.state, "correction did not converge"),
        attempts=attempts,
    )


async def _reverify(
    speech_guard: Guard, context: TurnContext, answer: str, flagged_span: str
) -> Verdict | None:
    """Return ``None`` if ``answer`` is acceptable (passes the speech guard
    AND does not echo the flagged span); otherwise the intervening verdict."""
    if not answer.strip():
        return Verdict.correct(reason="empty regenerated reply", span=flagged_span, spoken_correction="")
    if echoes(answer, flagged_span):
        return Verdict.correct(
            reason="regenerated reply echoed the withheld claim",
            span=flagged_span,
            spoken_correction="",
        )
    candidate = TurnContext(
        state=context.state, planned_reply=answer, customer_text=context.customer_text
    )
    v = await speech_guard.inspect(candidate)
    return v if v.intervened else None


async def run_intervention(
    *,
    verdict: Verdict,
    context: TurnContext,
    speech_guard: Guard,
    generator: ReplyGenerator | None = None,
    max_retries: int = 2,
) -> InterventionResult:
    filler = deflection_filler(verdict)

    # Structured path: answer straight from validated state (no LLM).
    structured = template_corrected_reply(context)
    if structured:
        rej = await _reverify(speech_guard, context, structured, verdict.span)
        if rej is None:
            return InterventionResult(filler=filler, answer=structured, attempts=1)
        # A state read-back that still fails can't be fixed by re-templating.
        return _escalate(filler, context, attempts=1)

    # Open path: regenerate via the main LLM, re-verify, cap retries.
    if generator is None:
        return _escalate(filler, context, attempts=0)

    packet = build_packet(verdict, context.state)
    answer = await generator.regenerate(packet=packet, customer_text=context.customer_text)
    for attempt in range(1, max_retries + 1):
        rej = await _reverify(speech_guard, context, answer, verdict.span)
        if rej is None:
            return InterventionResult(filler=filler, answer=answer, attempts=attempt)
        if attempt == max_retries:
            break
        packet = build_packet(rej, context.state)  # fold the new violation in
        answer = await generator.regenerate(
            packet=packet, customer_text=context.customer_text
        )
    return _escalate(filler, context, attempts=max_retries)


async def stream_intervention(
    *,
    verdict: Verdict,
    context: TurnContext,
    speech_guard: Guard,
    generator: ReplyGenerator | None = None,
    max_retries: int = 2,
    on_escalate: Callable[[HandoffContext], None] | None = None,
) -> AsyncIterator[str]:
    """Stream the intervention as spoken chunks IN SPOKEN ORDER.

    Yields the deflection filler FIRST — before any regeneration is
    awaited — so the consumer (TTS) starts speaking it while the grounded
    answer is still being produced. Then yields the grounded answer, or
    the escalation line on non-convergence. ``on_escalate`` (if given)
    receives the warm-handoff context so the caller can drive a transfer.
    """
    yield deflection_filler(verdict)  # on the wire before the await below

    result = await run_intervention(
        verdict=verdict,
        context=context,
        speech_guard=speech_guard,
        generator=generator,
        max_retries=max_retries,
    )

    if result.escalated:
        if on_escalate is not None and result.handoff is not None:
            on_escalate(result.handoff)
        yield ESCALATION_LINE
    elif result.answer:
        yield result.answer


__all__ = [
    "InterventionResult",
    "run_intervention",
    "stream_intervention",
    "deflection_filler",
    "template_corrected_reply",
    "ESCALATION_LINE",
]
