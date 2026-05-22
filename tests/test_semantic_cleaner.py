"""Regression guards for the semantic-Mirror item-list cleaner.

These cases come straight from the trace where the agent placed an
order with `items=["Jack's cheese pizza only, and not buffetroni"]` —
the entire confirmation sentence ended up as a single item name
because the override note had nowhere clean to extract from.

The cleaner exists so that even if the LLM hallucinates garbage in
likely_kept_items, it never reaches `place_order`.
"""

from mirror.semantic import _clean_item_list


def test_clean_items_passthrough():
    assert _clean_item_list(["mushroom", "large cheese"]) == ["mushroom", "large cheese"]


def test_full_sentence_rejected():
    """The exact bug from the trace."""
    assert _clean_item_list(
        ["Jack's cheese pizza only, and not buffetroni"]
    ) == []


def test_non_pizza_words_rejected():
    """Garbled STT artifacts must never reach place_order."""
    assert _clean_item_list(["phone"]) == []
    assert _clean_item_list(["cord", "no"]) == []


def test_modifier_only_words_rejected():
    """'mushroom only' has 'only' which the LLM was told to drop —
    defense in depth."""
    assert _clean_item_list(["mushroom only"]) == []
    assert _clean_item_list(["no pepperoni"]) == []
    assert _clean_item_list(["actually mushroom"]) == []


def test_none_and_non_list_inputs():
    assert _clean_item_list(None) == []
    assert _clean_item_list("not a list") == []
    assert _clean_item_list({"items": ["mushroom"]}) == []


def test_long_phrases_rejected():
    """Sentences > 5 words can't be valid item names."""
    assert _clean_item_list(["one large mushroom pizza with extra cheese please"]) == []


def test_size_modifier_with_real_topping_kept():
    assert _clean_item_list(["large pepperoni"]) == ["large pepperoni"]
    assert _clean_item_list(["small cheese"]) == ["small cheese"]
