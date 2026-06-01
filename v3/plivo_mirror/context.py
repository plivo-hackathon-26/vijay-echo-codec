"""Core dataclasses passed through the supervisor pipeline.

Everything that flows through `pre_gate → scorer → tool_gate → orchestrator`
is one of these. Pure data, no behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCallIntent:
    """A tool the primary agent intends to call but has not yet executed.

    The supervisor's tool-gate inspects these *before* the tool fires so
    irreversible side effects (place_order, charge_card) get a chance to
    be vetoed.
    """

    name: str
    args: dict[str, Any]
    irreversible: bool = False
    tool_call_id: str | None = None


@dataclass
class HistoryTurn:
    """One past turn in the conversation history."""

    role: Literal["customer", "agent"]
    text: str


@dataclass
class TurnPayload:
    """Everything the scorer needs to judge the current agent response.

    Supports both turn-based and streaming modes:
      - turn-based: primary_text is the full agent response, is_partial=False
      - streaming:  primary_text is the accumulated stream so far,
                    is_partial=True until the boundary token arrives
    """

    customer_text: str
    primary_text: str
    tool_calls: list[ToolCallIntent] = field(default_factory=list)
    history: list[HistoryTurn] = field(default_factory=list)
    is_partial: bool = False
    is_first_sentence_boundary: bool = False


@dataclass
class SupervisorContext:
    """Per-call context plumbed through every layer.

    Replaces the ContextVar-based call_uuid threading that the demo code
    uses. Explicit beats implicit.
    """

    call_uuid: str
    tenant_id: str | None = None  # v2 will key state on this; v1 ignores
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    """The scorer's output. Shaped so the orchestrator can act on it
    without any further LLM round-trip."""

    score: float  # 0.0 = response is fine, 1.0 = certain failure
    reason: str
    should_intervene: bool
    suggested_correction: str = ""
    # Whether this turn should be queued for the (v2) post-call reporter.
    # v1 ignores this; the field reserves the seam.
    should_report: bool = False
    # If the verdict came from the tool-gate, name the offending tool.
    blocked_tool: str | None = None
    # Free-form evidence the orchestrator may surface in correction text.
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def no_intervention(cls, reason: str = "ok") -> "Verdict":
        return cls(score=0.0, reason=reason, should_intervene=False)

    def spoken_correction(self) -> str:
        """Return a clean AGENT-VOICE correction string the TTS can
        speak directly to the customer.

        Priority order (later additions override earlier):
          1. ``self.suggested_correction`` if it reads as agent voice.
          2. Synthesize from ``evidence.customer_intent`` if it's a
             concrete order (not a meta-description like "The customer
             said...").
          3. Tier-0 check-specific synthesis (tripwires, retraction).
          4. Generic clarification fallback.

        This is the same logic the LiveKit ``SupervisedAgent`` calls
        when intervening — exposed on Verdict so customers writing
        their own adapters reuse it.
        """
        from plivo_mirror.text import is_customer_voice, is_meta_description

        supplied = (self.suggested_correction or "").strip()
        if supplied and not is_customer_voice(supplied):
            return supplied

        evidence = self.evidence or {}
        intent = (evidence.get("customer_intent") or "").strip()
        if intent and not is_meta_description(intent):
            clean = intent.rstrip(".").strip()
            return f"Got it — {clean}. Anything else?"

        # Tier 0 check-specific fallbacks.
        check = evidence.get("check") or ""
        tripwire = evidence.get("tripwire") or ""
        if check == "tool_arg_retracted_item":
            violating = evidence.get("violating_tokens") or []
            if violating:
                items = " and ".join(violating)
                return (
                    f"Sorry — just to confirm, you'd like to drop the {items} "
                    "from the order. Is that right?"
                )
        if tripwire == "refund_must_transfer":
            return (
                "I'm sorry, refunds need to be handled by a human supervisor. "
                "Let me transfer you over."
            )
        if tripwire == "cancel_subscription_must_confirm_or_transfer":
            return (
                "Just to confirm — you'd like to cancel your subscription, "
                "is that right?"
            )
        if tripwire == "dispute_charge_must_transfer":
            return (
                "For payment disputes, I'll need to transfer you to a "
                "specialist. Hold on one moment."
            )
        if check == "contradiction_marker":
            return (
                "Sorry — let me make sure I got that right. "
                "Could you tell me again which one you'd like?"
            )
        if check in ("number_consistency", "quantity_consistency"):
            return "Sorry — let me confirm those numbers with you one more time."

        return "Sorry — let me make sure I got that right. Could you say that again?"

    def post_correction_context(self, customer_text: str = "") -> str:
        """Return the one-shot system note that should be injected into
        the agent's chat context AFTER an intervention, so the LLM
        remembers the customer's true intent and doesn't re-ask.

        ``customer_text`` is the customer's actual utterance — used as
        a fallback if ``evidence.customer_intent`` is missing or a
        meta-description.
        """
        from plivo_mirror.text import is_meta_description
        from plivo_mirror.scorer.tier0.tool_arg_check import _split_on_retraction

        evidence = self.evidence or {}
        customer_intent = (evidence.get("customer_intent") or "").strip()
        if not customer_intent or is_meta_description(customer_intent):
            # Fall back to the post-retraction tail of customer_text, or the
            # whole text if there's no retraction marker.
            split = _split_on_retraction(customer_text or "")
            customer_intent = (
                split[1].strip() if split else (customer_text or "(see prior messages)")
            )

        spoken = self.spoken_correction()
        return (
            "INTERNAL CONTEXT (one-shot, apply to THIS turn only, never read aloud):\n"
            "\n"
            "A correction was just spoken to the customer because the previous "
            "planned tool call did not match what the customer actually wanted.\n"
            "\n"
            f"  • Customer's stated intent: \"{customer_intent}\"\n"
            f"  • Correction the customer just heard: \"{spoken}\"\n"
            "\n"
            "Rules for this turn:\n"
            "1. The read-back has ALREADY been done — the correction above WAS\n"
            "   the read-back. Do NOT read the order back again.\n"
            "2. If the customer's next message is any form of confirmation\n"
            "   (\"yes\", \"yep\", \"that's right\", \"sure\", \"correct\", \"please\",\n"
            "   \"go ahead\", \"ok\", \"do it\"), call the appropriate tool IMMEDIATELY\n"
            "   with the customer's stated intent above. Then speak a SHORT\n"
            "   confirmation line. Do not pad, do not re-confirm.\n"
            "3. If the customer denies (\"no\", \"wait\", \"actually\") or asks\n"
            "   something new, treat their message as a fresh request.\n"
            "4. Override the \"read order back before tool call\" policy for this\n"
            "   one turn only — re-reading would frustrate the customer."
        )


@dataclass
class TurnOutcome:
    """Returned by ``CallSupervisor.review_and_speak`` so the caller
    knows what was actually spoken on this turn (the agent's planned
    text, or Mirror's correction) without inspecting Verdict + history
    separately."""

    verdict: Verdict
    spoken_text: str
    intervened: bool


__all__ = [
    "ToolCallIntent",
    "HistoryTurn",
    "TurnPayload",
    "SupervisorContext",
    "Verdict",
    "TurnOutcome",
]
