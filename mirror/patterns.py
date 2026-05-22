"""Pure-Python pattern rules for the Mirror supervisor.

Each rule takes (recent_turns, current_user_turn, agent_state) and returns
either None (no signal) or a dict describing the fire:

    {
        "pattern_name": str,
        "severity": "info" | "warning" | "intervention",
        "evidence": {...},
        "intervention_needed": bool,
    }

Rules must be deterministic and side-effect free. The evaluator handles
persistence and side-effects.
"""

import re
from collections import Counter

PIZZA_ITEMS = [
    "pepperoni",
    "mushroom",
    "cheese",
    "veggie",
    "margherita",
    "marinara",
    "bacon",
    "sausage",
    "ham",
    "pineapple",
    "olive",
    "onion",
    "pepper",
]

CORRECTION_MARKERS = [
    "no ",
    "not ",
    "actually",
    "wait",
    "change that",
    "instead",
    "scratch that",
    "make it",
    "just ",
    "only ",
    "cancel that",
]

PAST_ORDER_PHRASES = [
    "last order",
    "previous order",
    "my last",
    "order from",
    "earlier order",
    "recent order",
    "yesterday's order",
    "what did i order",
]

REFUND_PHRASES = [
    "refund",
    "money back",
    "cancel my order",
]

STATUS_PHRASES = [
    "where is my order",
    "delivery status",
    "tracking",
    "when will it arrive",
]

_NO_ITEM_RE = re.compile(r"\bno\s+(\w+)\b", re.IGNORECASE)


def _word_in(needle: str, haystack: str) -> bool:
    """Whole-word, case-insensitive presence check. Prevents 'pepper'
    from matching inside 'pepperoni'."""
    return bool(re.search(rf"\b{re.escape(needle)}\b", haystack))


def _word_count(needle: str, haystack: str) -> int:
    return len(re.findall(rf"\b{re.escape(needle)}\b", haystack))


def contradiction_rule(recent_turns, current_user_turn, agent_state):
    """Fire when the customer mentions multiple items along with a
    correction marker — or contradicts themselves about a single item."""
    text = current_user_turn.lower()

    items_mentioned = [item for item in PIZZA_ITEMS if _word_in(item, text)]
    markers_found = [marker for marker in CORRECTION_MARKERS if marker in text]

    # Primary signal: 2+ distinct items + at least one correction marker.
    if len(items_mentioned) >= 2 and len(markers_found) >= 1:
        return {
            "pattern_name": "contradiction",
            "severity": "intervention",
            "evidence": {
                "user_said": current_user_turn,
                "items_detected": items_mentioned,
                "markers_found": markers_found,
                "reasoning": "multiple items + correction marker(s)",
            },
            "intervention_needed": True,
        }

    # Secondary signal: explicit "no <item>" where <item> also appears
    # somewhere else in the turn ("pepperoni... no pepperoni").
    for matched_word in _NO_ITEM_RE.findall(text):
        item = matched_word.lower()
        if item in PIZZA_ITEMS and _word_count(item, text) >= 2:
            return {
                "pattern_name": "contradiction",
                "severity": "intervention",
                "evidence": {
                    "user_said": current_user_turn,
                    "negated_item": item,
                    "reasoning": (
                        f"'no {item}' said but '{item}' also mentioned "
                        "elsewhere in the same turn"
                    ),
                },
                "intervention_needed": True,
            }

    return None


def missing_tool_rule(recent_turns, current_user_turn, agent_state):
    """Fire when the customer asks for something the agent has no tool to do,
    so the agent will likely hallucinate from conversation memory instead."""
    text = current_user_turn.lower()
    categories = [
        ("past_order", PAST_ORDER_PHRASES),
        ("refund", REFUND_PHRASES),
        ("delivery_status", STATUS_PHRASES),
    ]
    for category, phrases in categories:
        for phrase in phrases:
            if phrase in text:
                return {
                    "pattern_name": "missing_tool_request",
                    "severity": "intervention",
                    "evidence": {
                        "user_said": current_user_turn,
                        "category": category,
                        "matched_phrase": phrase,
                        "reasoning": (
                            "customer is asking for capability the agent "
                            "has no tool for; agent will likely hallucinate"
                        ),
                    },
                    "intervention_needed": True,
                }
    return None


def repetition_rule(recent_turns, current_user_turn, agent_state):
    """Fire (warning only, no intervention) when the customer repeats the
    same content word 3+ times across the last 4 customer turns.

    recent_turns already includes the current turn (it was written before
    evaluation). We slice to the last 4 customer turns and count.
    """
    customer_turns = [t for t in recent_turns if t.get("role") == "customer"]
    window = customer_turns[-4:]
    if len(window) < 2:
        return None

    counter = Counter()
    for turn in window:
        for word in (turn.get("text") or "").lower().split():
            word = word.strip(".,!?;:\"'")
            if len(word) >= 4 and word.isalpha():
                counter[word] += 1

    for word, count in counter.most_common():
        if count >= 3:
            return {
                "pattern_name": "repetition",
                "severity": "warning",
                "evidence": {
                    "repeated_word": word,
                    "count": count,
                    "window_size": len(window),
                    "reasoning": "possible customer frustration signal",
                },
                "intervention_needed": False,
            }
    return None


ALL_RULES = [contradiction_rule, missing_tool_rule, repetition_rule]
