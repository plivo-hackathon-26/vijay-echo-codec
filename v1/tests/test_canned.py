"""Canned correction + fallback template tests."""

from mirror.canned_corrections import CANNED, fallback_correction


def test_demo_fallback_with_classifier_hints():
    text = fallback_correction({
        "pattern_name": "contradiction",
        "evidence": {
            "items_detected": ["pepperoni", "mushroom"],
            "markers_found": ["no ", "actually"],
            "likely_kept_items": ["mushroom"],
            "likely_removed_items": ["pepperoni"],
        },
    })
    assert "mushroom" in text
    assert "no pepperoni" in text
    assert "pizza pizza" not in text


def test_fallback_only_kept_hints():
    text = fallback_correction({
        "pattern_name": "contradiction",
        "evidence": {
            "items_detected": ["mushroom"],
            "likely_kept_items": ["mushroom"],
            "likely_removed_items": [],
        },
    })
    assert "mushroom" in text
    assert "pizza pizza" not in text


def test_fallback_only_removed_hints():
    text = fallback_correction({
        "pattern_name": "contradiction",
        "evidence": {
            "items_detected": ["pepperoni"],
            "likely_kept_items": [],
            "likely_removed_items": ["pepperoni"],
        },
    })
    assert "no pepperoni" in text
    assert "pizza pizza" not in text


def test_fallback_legacy_negated_item():
    text = fallback_correction({
        "pattern_name": "contradiction",
        "evidence": {
            "items_detected": ["pepperoni"],
            "negated_item": "pepperoni",
        },
    })
    assert "pizza pizza" not in text
    assert "pepperoni" in text


def test_fallback_legacy_two_items_no_hints():
    text = fallback_correction({
        "pattern_name": "contradiction",
        "evidence": {"items_detected": ["pepperoni", "mushroom"]},
    })
    assert "mushroom" in text
    assert "pepperoni" in text


def test_fallback_handoff_canned():
    text = fallback_correction({"pattern_name": "missing_tool_request", "evidence": {}})
    assert text == CANNED["missing_tool_request"]["correction"]


def test_fallback_unknown_pattern():
    text = fallback_correction({"pattern_name": "totally_unknown", "evidence": {}})
    assert text  # non-empty
    assert "pizza pizza" not in text


def test_buffer_lines_present():
    assert CANNED["contradiction"]["buffer"].endswith("...")
    assert CANNED["missing_tool_request"]["buffer"].endswith("...")
