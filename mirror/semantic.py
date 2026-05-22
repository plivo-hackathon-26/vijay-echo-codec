"""LLM-based semantic reviewer.

Runs AFTER the primary agent has generated its planned response but
BEFORE the response is spoken to the customer. Inspects:
- What the customer actually said
- What the primary agent plans to say
- Any tool calls (place_order, calculate_total) the primary made

…and decides whether the primary is about to make a mistake.

This is the second of Mirror's two detection layers:
  1. patterns.py — pure-Python regex pre-filter (zero LLM cost, fires
     instantly on obvious contradictions / missing-tool requests).
  2. semantic.py — LLM-based review (catches semantic mismatches,
     ambiguous corrections, wrong tool calls that patterns miss).

If patterns already flagged an intervention, semantic doesn't run —
we already know we're intervening. Semantic only runs when patterns
say "looks fine" but we want a smarter second opinion.
"""

import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

from mirror.patterns import PIZZA_ITEMS
from prompts import MIRROR_SEMANTIC_REVIEW_PROMPT

log = logging.getLogger("mirror.semantic")

_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        base_url = os.getenv("OPENAI_API_URL", "").strip().rstrip("/") or None
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        _client = AsyncOpenAI(api_key=key, base_url=base_url)
    return _client


def _summarize_history(history: list, max_turns: int = 6) -> str:
    """Compact recent history for the prompt. Each line: 'role: text'."""
    recent = history[-max_turns:]
    lines = []
    for turn in recent:
        role = "Customer" if turn.get("role") == "customer" else "Agent"
        text = (turn.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines) if lines else "(empty)"


def _ok_verdict(reason: str = "no concerns") -> dict:
    return {
        "needs_intervention": False,
        "reason": reason,
        "what_customer_wants": "",
        "suggested_correction": "",
    }


async def review_response(
    customer_text: str,
    primary_response_text: str,
    tool_calls: list,
    history: list,
    timeout_s: float | None = None,
) -> dict:
    """Compare the customer's intent with the primary agent's plan.

    Returns a dict shaped like a `patterns.py` fire so it can flow
    through the existing intervention machinery:

        {
            "pattern_name": "semantic_mismatch",
            "severity": "intervention",
            "strategy": "self_correct",
            "intervention_needed": True | False,
            "evidence": { ... },
        }

    On LLM failure or timeout this returns a NO-INTERVENTION verdict
    so a Mirror outage never silently degrades the agent.
    """
    if timeout_s is None:
        try:
            timeout_s = float(os.getenv("MIRROR_SEMANTIC_TIMEOUT_S", "4.0"))
        except ValueError:
            timeout_s = 4.0

    # Cheap heuristic shortcut: if the primary made no tool calls and
    # spoke a short response, almost nothing can go wrong. Skip the
    # LLM call to keep latency down on small-talk turns.
    if not tool_calls and len(primary_response_text) < 80:
        return _build_no_intervention_verdict("short response, no tool calls")

    tool_calls_payload = [
        {"name": tc.get("name"), "args": tc.get("args"), "result": tc.get("result")}
        for tc in tool_calls
    ]
    prompt = MIRROR_SEMANTIC_REVIEW_PROMPT.format(
        customer_text=customer_text,
        primary_response_text=primary_response_text,
        tool_calls_json=json.dumps(tool_calls_payload, ensure_ascii=False),
        history_summary=_summarize_history(history),
    )

    try:
        client = _openai()
        resp = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            messages=[{"role": "system", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=timeout_s,
        )
    except Exception:
        log.exception("semantic review LLM failed — defaulting to no-intervention")
        return _build_no_intervention_verdict("llm_error")

    raw = (resp.choices[0].message.content or "").strip()
    try:
        verdict_data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("semantic review returned non-JSON: %r", raw[:200])
        return _build_no_intervention_verdict("non_json_response")

    needs = bool(verdict_data.get("needs_intervention", False))
    reason = str(verdict_data.get("reason", "")).strip()
    intent = str(verdict_data.get("what_customer_wants", "")).strip()
    suggestion = str(verdict_data.get("suggested_correction", "")).strip()
    likely_kept = _clean_item_list(verdict_data.get("likely_kept_items"))
    likely_removed = _clean_item_list(verdict_data.get("likely_removed_items"))

    log.info(
        "semantic verdict: needs_intervention=%s reason=%r kept=%s removed=%s",
        needs,
        reason,
        likely_kept,
        likely_removed,
    )

    if not needs:
        return _build_no_intervention_verdict(reason or "ok")

    return {
        "pattern_name": "semantic_mismatch",
        "severity": "intervention",
        "strategy": "self_correct",
        "intervention_needed": True,
        "evidence": {
            "customer_said": customer_text,
            "primary_planned_response": primary_response_text,
            "primary_tool_calls": tool_calls_payload,
            "what_customer_wants": intent,
            "likely_kept_items": likely_kept,
            "likely_removed_items": likely_removed,
            "reason": reason,
            "suggested_correction_hint": suggestion,
        },
    }


def _clean_item_list(raw: Any) -> list:
    """Defensive cleaner for the LLM's structured item lists.

    Strips out anything that isn't a short clean noun phrase:
    - Empty strings
    - Sentences (>5 words)
    - Items containing markers we explicitly told the model to omit
      ("only", "no", "actually", "instead")
    """
    if not isinstance(raw, list):
        return []
    cleaned: list = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        s = entry.strip().lower()
        if not s or len(s.split()) > 5:
            continue
        # The LLM is told NEVER to include these — but defense in depth.
        if any(bad in s.split() for bad in ("only", "no", "not", "actually", "instead")):
            continue
        # Require at least one recognized pizza vocabulary word so
        # garbled STT artifacts like "cord" or "phone" never reach
        # place_order. Modifier-only entries ("large", "small")
        # without a topping word are also rejected.
        toppings = [w for w in PIZZA_ITEMS if w not in ("pizza",)]
        if not any(item in s for item in toppings):
            continue
        cleaned.append(s)
    return cleaned


def _build_no_intervention_verdict(reason: str) -> dict:
    return {
        "pattern_name": "semantic_mismatch",
        "severity": "info",
        "strategy": "self_correct",
        "intervention_needed": False,
        "evidence": {"reason": reason},
    }
