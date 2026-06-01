"""Unit tests for the Tier-0 arithmetic consistency check (SER-5263).

Covers the three shapes it handles (sum, quantity×price, change), the
clean counterparts it must NOT fire on, and the deferral cases where it
returns None rather than guessing.
"""

from plivo_mirror.context import SupervisorContext, TurnPayload
from plivo_mirror.scorer.tier0.arithmetic import ArithmeticConsistencyCheck

CHK = ArithmeticConsistencyCheck()
CTX = SupervisorContext(call_uuid="test")


def _fires(customer: str, agent: str) -> bool:
    turn = TurnPayload(
        customer_text=customer, primary_text=agent, tool_calls=[], history=[]
    )
    res = CHK.evaluate(turn, CTX)
    return res.verdict is not None and res.verdict.should_intervene


# ── should fire: wrong arithmetic ────────────────────────────────────

def test_wrong_sum_total():
    assert _fires(
        "A margherita at nine dollars and a Coke at three dollars. What's the total?",
        "That comes to $15 total.",
    )


def test_wrong_quantity_times_price():
    assert _fires(
        "Two margheritas, they're nine each — what do I owe?",
        "Two margheritas comes to $20.",
    )


def test_wrong_change():
    assert _fires(
        "My order's twelve dollars and I'm paying with a twenty — how much change?",
        "You'll get $10 back.",
    )


def test_reports_expected_value_in_reason():
    turn = TurnPayload(
        customer_text="A margherita at nine dollars and a Coke at three dollars, total?",
        primary_text="That's $15 total.",
        tool_calls=[], history=[],
    )
    v = CHK.evaluate(turn, CTX).verdict
    assert v is not None and v.evidence["expected"] == 12.0
    assert v.evidence["asserted"] == 15.0


# ── must NOT fire: correct arithmetic ────────────────────────────────

def test_correct_sum_passes():
    assert not _fires(
        "A margherita at nine and a Coke at three — total?",
        "That's $12 — $9 for the margherita and $3 for the Coke.",
    )


def test_correct_quantity_times_price_passes():
    assert not _fires(
        "Two margheritas at nine each — what do I owe?",
        "Two margheritas at $9 each is $18.",
    )


def test_correct_change_passes():
    assert not _fires(
        "It's twelve dollars and I'm paying with a twenty — change?",
        "Your change is $8.",
    )


# ── must NOT fire: not a math turn / can't pin a result (defer) ──────

def test_no_total_language_defers():
    # Price hallucination is NOT arithmetic's job — defer to other tiers.
    assert not _fires("How much is the BBQ chicken?", "The BBQ chicken is $14.99.")


def test_no_numbers_defers():
    assert not _fires("What's the total?", "Let me add that up for you.")


def test_plain_order_does_not_fire():
    turn = TurnPayload(
        customer_text="Two Cokes please.",
        primary_text="Two Cokes, coming up.",
        tool_calls=[], history=[],
    )
    assert CHK.evaluate(turn, CTX).verdict is None
