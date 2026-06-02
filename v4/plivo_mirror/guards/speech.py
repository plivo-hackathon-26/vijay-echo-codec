"""``SpeechGuard`` — guards the speech boundary (LLM tokens → TTS).

Implements the ``Guard`` protocol. The router, in order:

  1. **Deterministic layer** — compiled policy checks. A hard hit ⇒
     ``block`` immediately (verifier never runs).
  2. **Risk-span tagger** — no risky span ⇒ ``pass`` at zero latency
     (verifier never runs). This is the clean-turn fast path.
  3. **Confidence gate** — a risky span with high model confidence ⇒
     ``pass``; risky + uncertain ⇒ escalate. (In this setup logprobs are
     unavailable, so confidence is 0.0 and every risky span escalates.)
  4. **Grounded verifier** — the only expensive call, over the flagged
     spans. Unsupported ⇒ ``correct`` with an agent-voice grounded
     correction; supported ⇒ ``pass``.

Fails OPEN (``pass``) if the verifier errors — a guard fault must not
break a live call.
"""

from __future__ import annotations

import logging
from typing import Callable

from plivo_mirror.contracts import TurnContext, Verdict
from plivo_mirror.guards.deterministic import run_deterministic
from plivo_mirror.guards.risk_spans import RiskSpan, tag_risk_spans
from plivo_mirror.guards.semantic import SemanticSignal
from plivo_mirror.guards.signal import ConfidenceSignal, LogprobEntropySignal
from plivo_mirror.intervention.correction import (
    correction_for_spans,
    default_block_correction,
)
from plivo_mirror.verifier.base import GroundingEvidence, Verifier

log = logging.getLogger("plivo_mirror.guards.speech")


class SpeechGuard:
    def __init__(
        self,
        verifier: Verifier | None = None,
        *,
        signal: ConfidenceSignal | None = None,
        confidence_threshold: float = 0.6,
        tagger: Callable[[str], list[RiskSpan]] = tag_risk_spans,
        semantic_signal: SemanticSignal | None = None,
    ) -> None:
        self._verifier = verifier
        self._signal = signal or LogprobEntropySignal()
        self._threshold = confidence_threshold
        self._tag = tagger
        # Optional second recall tier: when the lexicon finds no risky span,
        # this catches replies that contradict the customer's stated request
        # (negation/compound/conditional). OFF by default (None) so the core
        # stays ~0ms and the heavy NLI dependency is opt-in. See semantic.py.
        self._semantic = semantic_signal

    async def inspect(self, context: TurnContext) -> Verdict:
        reply = context.planned_reply or ""

        # 1. Deterministic hit ⇒ block immediately.
        det = run_deterministic(context)
        if det is not None:
            if not det.spoken_correction:
                det.spoken_correction = default_block_correction()
            return det

        # 2. No lexical risky span ⇒ normally a zero-latency pass. But first
        #    consult the optional semantic tier (NLI): a reply can be fluent
        #    and lexically clean yet contradict a constraint the customer
        #    stated (ignored negation / dropped modifier / wrong condition).
        #    If it fires, synthesize a span so the rest of the router treats
        #    the turn like any flagged span and routes it to the verifier,
        #    which still has the final say (recall here, precision there).
        spans = self._tag(reply)
        if not spans:
            if self._semantic is not None:
                sem = self._semantic.contradicts(
                    context.customer_text, reply, state=context.state
                )
                if sem.contradiction:
                    spans = [RiskSpan(text=reply, kind="semantic", start=0, end=len(reply))]
            if not spans:
                return Verdict.pass_("no_risk_span")

        # 3. Confidence gate (only matters when a risky span is present).
        # HONEST NOTE: in the current LiveKit + Azure-gpt-5-mini setup,
        # ``context.logprobs`` is never populated, so ``confidence`` is a
        # constant 0.0 and this gate NEVER passes — routing is risk-span-
        # driven only. The gate stays here, intact, so that supplying a
        # real ConfidenceSignal (or threading logprobs through the adapter)
        # activates semantic-entropy routing with no code change. This is a
        # documented FUTURE capability, not a current one. See CLAUDE.md.
        conf = self._signal.confidence(reply, context.logprobs)
        if conf >= self._threshold:
            return Verdict.pass_("confident", confidence=conf)

        # 4. Escalate flagged spans to the grounded verifier.
        if self._verifier is None:
            # Nothing to escalate to — fail open rather than block blindly.
            return Verdict.pass_("no_verifier", confidence=conf)

        evidence = GroundingEvidence(
            reply=reply,
            flagged_spans=[s.text for s in spans],
            # validated per-call entities the caller supplied (source of truth)
            facts={k: str(e.value) for k, e in context.state.entities.items()},
            policies=[
                {"id": p.id, "text": p.text} for p in context.state.compiled_policies
            ],
            # code-owned reference facts (catalog/hours/prices/counts): without
            # these the verifier has nothing to confirm a legitimate number
            # against and false-flags it. Stringified as "key: value" lines.
            retrieved_facts=[
                f"{k}: {v}" for k, v in context.state.known_facts.items()
            ],
            # the customer's request — lets the verifier confirm/deny a
            # contradiction (the precision check behind the semantic tier).
            customer_text=context.customer_text,
        )
        try:
            result = await self._verifier.verify(reply, evidence)
        except Exception:
            log.warning("verifier raised; failing open (pass)", exc_info=True)
            return Verdict.pass_("verifier_error", confidence=conf)

        if result.supported:
            return Verdict.pass_("verifier_supported", confidence=conf)

        return Verdict.correct(
            reason=result.reason or "claim unsupported by state/policy",
            spoken_correction=correction_for_spans(spans),
            policy_id=result.policy_id,
            span=spans[0].text,
            confidence=conf,
        )


__all__ = ["SpeechGuard"]
