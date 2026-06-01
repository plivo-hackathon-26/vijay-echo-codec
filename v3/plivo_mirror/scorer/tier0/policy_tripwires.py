"""Policy keyword tripwires.

Some policies are pure if-then rules: "if the customer says 'refund',
the agent MUST mention 'transfer' or 'supervisor' — otherwise the
agent is confirming a refund the operator forbids."

Tripwires are deterministic, run in microseconds, and produce
near-zero false positives because they only fire when *both* sides
of the rule fail.

Tripwires are OFF by default: a generic safety net cannot assume a
customer's business rules (a billing agent may be fully authorised to
process refunds without a human, so a "refund must transfer" rule would
fire false alarms on correct behaviour). ``DEFAULT_TRIPWIRES`` below is a
ready-made example set — opt in explicitly with
``PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES)`` or supply your own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from plivo_mirror.context import SupervisorContext, TurnPayload, Verdict
from plivo_mirror.scorer.tier0.base import Tier0Result


@dataclass
class Tripwire:
    """A single conditional rule.

    Fires when ``customer_pattern`` matches the customer's utterance AND
    ``required_in_response`` does NOT appear in the agent's planned
    response.
    """

    name: str
    customer_pattern: re.Pattern[str]
    required_in_response: re.Pattern[str]
    score: float = 0.95
    reason: str = ""

    def matches(self, customer_text: str, primary_text: str) -> bool:
        if not customer_text or not primary_text:
            return False
        if not self.customer_pattern.search(customer_text):
            return False
        if self.required_in_response.search(primary_text):
            return False
        return True


# Ready-made example tripwires for support/billing-style agents. NOT
# applied by default (see module docstring) — opt in by passing this to
# PolicyTripwireCheck(tripwires=DEFAULT_TRIPWIRES).
DEFAULT_TRIPWIRES: list[Tripwire] = [
    Tripwire(
        name="refund_must_transfer",
        customer_pattern=re.compile(
            r"\b(refund|reimburse|money\s*back|chargeback)\b", re.IGNORECASE
        ),
        required_in_response=re.compile(
            r"\b(transfer|supervisor|human|representative|agent|escalat\w+|manager)\b",
            re.IGNORECASE,
        ),
        reason="customer mentioned a refund; agent did not route to a human handoff",
    ),
    Tripwire(
        name="cancel_subscription_must_confirm_or_transfer",
        customer_pattern=re.compile(
            r"\bcancel\s+(?:my\s+)?(?:subscription|membership|account|plan)\b",
            re.IGNORECASE,
        ),
        required_in_response=re.compile(
            r"\b(confirm|are\s+you\s+sure|transfer|supervisor|human|escalat\w+)\b",
            re.IGNORECASE,
        ),
        reason="customer asked to cancel a subscription; agent neither asked to confirm nor offered handoff",
    ),
    Tripwire(
        name="dispute_charge_must_transfer",
        customer_pattern=re.compile(
            r"\bdispute\s+(?:the\s+)?(?:charge|payment|transaction|bill)\b",
            re.IGNORECASE,
        ),
        required_in_response=re.compile(
            r"\b(transfer|supervisor|human|representative|escalat\w+|specialist)\b",
            re.IGNORECASE,
        ),
        reason="customer raised a payment dispute; agent did not route to a human",
    ),
]


@dataclass
class PolicyTripwireCheck:
    """Tier 0 check that runs a list of conditional tripwires."""

    tripwires: list[Tripwire] = field(default_factory=list)
    name: str = "policy_tripwires"

    def evaluate(
        self, turn: TurnPayload, ctx: SupervisorContext
    ) -> Tier0Result:
        customer = turn.customer_text or ""
        primary = turn.primary_text or ""
        for tw in self.tripwires:
            if tw.matches(customer, primary):
                return Tier0Result(
                    verdict=Verdict(
                        score=tw.score,
                        reason=tw.reason or f"tripwire {tw.name!r} fired",
                        should_intervene=True,
                        suggested_correction="",
                        evidence={
                            "tier": "tier0",
                            "check": self.name,
                            "tripwire": tw.name,
                            "customer_text": customer,
                        },
                        should_report=True,
                    ),
                    check_name=self.name,
                    evidence={"tripwire": tw.name},
                )
        return Tier0Result(verdict=None, check_name=self.name)


__all__ = ["PolicyTripwireCheck", "Tripwire", "DEFAULT_TRIPWIRES"]
