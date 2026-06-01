"""Phase 2 — risk-span tagger."""

from __future__ import annotations

from plivo_mirror.guards.risk_spans import tag_risk_spans


def _kinds(text):
    return {s.kind for s in tag_risk_spans(text)}


def test_clean_reply_has_no_risky_span():
    assert tag_risk_spans("Sure, what would you like to order?") == []
    assert tag_risk_spans("") == []


def test_price_detected():
    assert "price" in _kinds("That'll be $12.50.")
    assert "price" in _kinds("It costs 9 dollars.")


def test_percent_detected():
    assert "percent" in _kinds("You get 20% off today.")
    assert "percent" in _kinds("That's 15 percent off.")


def test_commitment_words_detected():
    assert "commitment" in _kinds("I'll process a full refund right now.")
    assert "commitment" in _kinds("You're eligible for a discount.")
    assert "commitment" in _kinds("I can waive that fee.")


def test_bare_number_detected():
    assert "number" in _kinds("Your order number is 4471.")


def test_overlapping_price_not_double_tagged_as_number():
    spans = tag_risk_spans("That'll be $12.50.")
    # the price match should claim the digits; no separate bare-number span
    assert [s.kind for s in spans] == ["price"]


def test_spans_sorted_by_position():
    spans = tag_risk_spans("You're eligible, and it's $5.")
    assert [s.start for s in spans] == sorted(s.start for s in spans)
