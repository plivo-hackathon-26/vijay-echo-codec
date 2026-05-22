"""Pure-Python rule tests. No DB, no network."""

from mirror.patterns import (
    contradiction_rule,
    missing_tool_rule,
    repetition_rule,
)


class TestContradictionRule:
    def test_demo_scenario_fires(self):
        r = contradiction_rule(
            [], "Large pepperoni, actually mushroom only, no pepperoni", {}
        )
        assert r is not None
        assert r["pattern_name"] == "contradiction"
        assert r["severity"] == "intervention"
        assert r["strategy"] == "self_correct"
        assert r["intervention_needed"] is True
        items = r["evidence"]["items_detected"]
        assert "pepperoni" in items and "mushroom" in items

    def test_demo_scenario_classifier(self):
        r = contradiction_rule(
            [], "Large pepperoni, actually mushroom only, no pepperoni", {}
        )
        assert r["evidence"]["likely_kept_items"] == ["mushroom"]
        assert r["evidence"]["likely_removed_items"] == ["pepperoni"]

    def test_actually_swap(self):
        r = contradiction_rule([], "Pepperoni, actually mushroom", {})
        assert r is not None
        assert r["evidence"]["likely_kept_items"] == ["mushroom"]
        assert r["evidence"]["likely_removed_items"] == ["pepperoni"]

    def test_only_marker_classifies_correctly(self):
        r = contradiction_rule([], "Mushroom only, no pepperoni", {})
        assert r is not None
        assert "mushroom" in r["evidence"]["likely_kept_items"]
        assert "pepperoni" in r["evidence"]["likely_removed_items"]

    def test_just_marker(self):
        r = contradiction_rule([], "Just cheese, no pepperoni", {})
        assert r is not None
        assert "cheese" in r["evidence"]["likely_kept_items"]
        assert "pepperoni" in r["evidence"]["likely_removed_items"]

    def test_secondary_self_negation(self):
        r = contradiction_rule([], "Pepperoni, no pepperoni", {})
        assert r is not None
        assert r["evidence"]["negated_item"] == "pepperoni"
        assert r["strategy"] == "self_correct"
        assert r["evidence"]["likely_removed_items"] == ["pepperoni"]

    def test_happy_path_no_fire(self):
        r = contradiction_rule([], "I'd like a large cheese pizza", {})
        assert r is None

    def test_single_item_with_modifier_no_fire(self):
        r = contradiction_rule([], "Large mushroom please", {})
        assert r is None

    def test_pepper_does_not_match_pepperoni(self):
        r = contradiction_rule([], "Add bell pepper, no mushroom", {})
        if r is None:
            return
        items = r["evidence"]["items_detected"]
        assert "pepperoni" not in items

    def test_smart_format_punctuation_does_not_break_markers(self):
        """Regression: Deepgram smart_format produces "No." (with
        period), not "no " (with space). Marker matching must still
        fire on either form."""
        r = contradiction_rule(
            [], "Hello. I'd like a large pepperoni. No. Mushroom only.", {}
        )
        assert r is not None
        assert r["pattern_name"] == "contradiction"
        # Strong-neg "No." should split the utterance correctly
        assert r["evidence"]["likely_kept_items"] == ["mushroom"]
        assert r["evidence"]["likely_removed_items"] == ["pepperoni"]

    def test_interjection_no_with_comma(self):
        r = contradiction_rule(
            [], "Large pepperoni, no, actually just mushroom please.", {}
        )
        assert r is not None
        assert "mushroom" in r["evidence"]["likely_kept_items"]
        assert "pepperoni" in r["evidence"]["likely_removed_items"]

    def test_markers_match_with_trailing_punctuation(self):
        """Regression: 'Mushroom only.' should fire markers same as
        'Mushroom only,'."""
        r = contradiction_rule([], "Pepperoni. Actually mushroom only.", {})
        assert r is not None
        markers = r["evidence"]["markers_found"]
        # at least one marker should be found despite the periods
        assert len(markers) >= 1


class TestMissingToolRule:
    def test_past_order_fires_with_handoff_strategy(self):
        r = missing_tool_rule([], "Can you check my last order?", {})
        assert r is not None
        assert r["pattern_name"] == "missing_tool_request"
        assert r["strategy"] == "handoff"
        assert r["evidence"]["category"] == "past_order"
        assert r["intervention_needed"] is True

    def test_refund_fires(self):
        r = missing_tool_rule([], "I want a refund please", {})
        assert r is not None
        assert r["evidence"]["category"] == "refund"
        assert r["strategy"] == "handoff"

    def test_delivery_status_fires(self):
        r = missing_tool_rule([], "Where is my order?", {})
        assert r is not None
        assert r["evidence"]["category"] == "delivery_status"

    def test_happy_path_no_fire(self):
        r = missing_tool_rule([], "I want a pepperoni pizza", {})
        assert r is None


class TestRepetitionRule:
    def test_warning_only_never_intervention(self):
        turns = [{"role": "customer", "text": "hello?"}] * 4
        r = repetition_rule(turns, "hello?", {})
        if r is not None:
            assert r["severity"] == "warning"
            assert r["intervention_needed"] is False

    def test_too_few_turns_no_fire(self):
        r = repetition_rule(
            [{"role": "customer", "text": "hello there"}], "hello there", {}
        )
        assert r is None
