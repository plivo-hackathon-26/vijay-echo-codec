"""Phase 3 — intervention orchestration.

When mirror.evaluator flags `intervention_needed=True` on a customer
turn, voice/stream.py calls `handle_intervention` here instead of the
normal primary-agent run_turn.

The flow:

  t=0    speak buffer line on the call (Plivo accepts immediately,
         starts playing on the line within a few hundred ms)
  t=0    in parallel, generate the corrected response with the LLM
         (self_correct strategy) OR pick the canned response (handoff)
  t≈Bms  wait until the buffer should have finished playing
  t≈Bms  speak the correction text

Where Bms is MIRROR_BUFFER_DURATION_MS (default 2500). If the LLM is
slower than Bms the call will have a longer-than-buffer pause; if it
is faster, we still hold for Bms so the two utterances don't overlap.

If the LLM call fails or times out (MIRROR_CORRECTION_TIMEOUT_S,
default 4.0), we fall back to a deterministic template based on
Mirror's evidence so the demo never goes silent.

The intervention path must never raise out of this function — every
external call is wrapped, and on catastrophic failure we still try to
say *something* on the call.
"""

import asyncio
import logging
import os
import time

import db
from mirror import state
from mirror.canned_corrections import CANNED, fallback_correction
from mirror.patterns import PIZZA_ITEMS, _word_in

log = logging.getLogger("mirror.interventions")

_GREEN = "\033[32m"
_RESET = "\033[0m"


def _buffer_duration_s(buffer_text: str) -> float:
    """How long to hold before speaking the correction.

    Plivo's WOMAN voice speaks ~13 characters per second. If we send
    the correction before the buffer audio finishes playing, the two
    overlap (or the second cuts off the first). Default: derive from
    the actual buffer text length + 0.5s safety pad.
    """
    raw = os.getenv("MIRROR_BUFFER_DURATION_MS")
    if raw:
        try:
            return max(0.0, int(raw) / 1000.0)
        except ValueError:
            pass
    return 0.5 + max(2.0, len(buffer_text) / 13.0)


def _correction_timeout_s() -> float:
    raw = os.getenv("MIRROR_CORRECTION_TIMEOUT_S", "4.0")
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 4.0


def _cooldown_s() -> float:
    raw = os.getenv("MIRROR_COOLDOWN_S", "10")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 10.0


def _extract_pizza_items(text: str) -> list:
    """Pull clean item names from a free-form sentence using the
    Mirror PIZZA_ITEMS vocabulary. Used as a safety net for semantic
    verdicts that didn't supply structured kept_items.
    """
    if not text:
        return []
    text_l = text.lower()
    return [item for item in PIZZA_ITEMS if _word_in(item, text_l)]


def _log_intervention(call_uuid: str, pattern_name: str, strategy: str) -> None:
    prefix = call_uuid[:8] if call_uuid else "????????"
    print(
        f"{_GREEN}🚑 INTERVENTION [{prefix}]: "
        f"{pattern_name} | strategy={strategy}{_RESET}",
        flush=True,
    )


def _log_corrected(call_uuid: str, correction_text: str) -> None:
    prefix = call_uuid[:8] if call_uuid else "????????"
    print(
        f"{_GREEN}✓ CORRECTED [{prefix}]: \"{correction_text}\"{_RESET}",
        flush=True,
    )


async def handle_intervention(
    call_uuid: str,
    pattern_result: dict,
    history: list,
    speak_fn,
    generate_fn,
) -> str:
    """Orchestrate buffer + correction. Returns the spoken correction text.

    speak_fn: voice.tts.speak_on_call (synchronous Plivo SDK call;
              we wrap it in to_thread).
    generate_fn: agent.primary.run_correction_turn (async).
    """
    started = time.monotonic()
    pattern_name = pattern_result.get("pattern_name", "unknown")
    strategy = pattern_result.get("strategy", "self_correct")
    canned = CANNED.get(pattern_name, {})
    buffer_text = canned.get("buffer", "One moment please...")

    _log_intervention(call_uuid, pattern_name, strategy)

    # Fire the buffer line non-blocking. speak_on_call is a sync REST
    # call, so we offload it to a thread so this coroutine yields.
    buffer_started = time.monotonic()
    buffer_task = asyncio.create_task(
        asyncio.to_thread(speak_fn, call_uuid, buffer_text)
    )

    cached_audio_used = False
    correction_text = ""

    if strategy == "self_correct":
        try:
            correction_text = await asyncio.wait_for(
                generate_fn(call_uuid, history, pattern_result.get("evidence", {})),
                timeout=_correction_timeout_s(),
            )
        except asyncio.TimeoutError:
            log.warning("correction LLM timed out — using fallback template")
            correction_text = fallback_correction(pattern_result)
            cached_audio_used = True
        except Exception:
            log.exception("correction LLM failed — using fallback template")
            correction_text = fallback_correction(pattern_result)
            cached_audio_used = True
    else:
        # handoff strategy: fully canned, no LLM needed
        correction_text = canned.get("correction") or fallback_correction(pattern_result)
        cached_audio_used = True

    if not correction_text:
        correction_text = fallback_correction(pattern_result)
        cached_audio_used = True

    # Buffer was fired-and-forget for Plivo; the task itself just waits
    # for the HTTP call to come back, which is fast. Make sure it
    # completed (and surface any error) before pacing further.
    try:
        await buffer_task
    except Exception:
        log.exception("buffer speak_on_call failed (continuing)")

    # Pace: hold until the buffer audio should have finished playing on
    # the line. We have already burned `correction_elapsed` waiting for
    # the LLM, so only sleep the remainder.
    buffer_s = _buffer_duration_s(buffer_text)
    elapsed = time.monotonic() - buffer_started
    remaining = max(0.0, buffer_s - elapsed)
    if remaining > 0:
        await asyncio.sleep(remaining)

    # Now speak the correction.
    try:
        await asyncio.to_thread(speak_fn, call_uuid, correction_text)
    except Exception:
        log.exception("correction speak_on_call failed")

    latency_ms = int((time.monotonic() - started) * 1000)
    _log_corrected(call_uuid, correction_text)

    # Persist the intervention row.
    try:
        db.add_intervention(
            call_uuid=call_uuid,
            triggered_by_event_id=pattern_result.get("mirror_event_id"),
            pattern_name=pattern_name,
            strategy=strategy,
            buffer_text=buffer_text,
            correction_text=correction_text,
            cached_audio_used=cached_audio_used,
            latency_ms=latency_ms,
        )
    except Exception:
        log.exception("failed to persist intervention row")

    # Suppress Mirror on the customer's immediate confirmation turn.
    state.set_cooldown(call_uuid, _cooldown_s())

    # For self-correct interventions, the primary agent's rigged
    # multi-item-capture rule will otherwise re-fire on the next turn
    # (it re-reads the contradictory turn from history and grabs every
    # item again). Install a one-shot system note that treats the
    # correction question as ground truth for the next turn only.
    if strategy == "self_correct":
        evidence = pattern_result.get("evidence", {}) or {}
        kept = list(evidence.get("likely_kept_items") or [])
        removed = list(evidence.get("likely_removed_items") or [])
        what_wants = (evidence.get("what_customer_wants") or "").strip()

        # Defense in depth: if neither the pattern nor the semantic
        # Mirror produced a clean kept list, fall back to extracting
        # pizza-vocab items from the LLM's natural-language intent.
        # NEVER dump the whole sentence into the override — the
        # agent will obediently pass it to place_order as the item
        # name (we saw this with "Jack's cheese pizza only, and not
        # buffetroni" being captured verbatim as the items list).
        if not kept and what_wants:
            kept = _extract_pizza_items(what_wants)

        if kept:
            kept_str = ", ".join(kept)
        else:
            kept_str = "(use only items from your confirmation question above — short noun phrases like 'mushroom' or 'large cheese')"

        removed_str = (
            ", ".join(removed) if removed else
            "(anything not in your confirmation question)"
        )

        override_note = (
            "URGENT — MIRROR CORRECTION CONTEXT (applies to THIS turn only):\n"
            f"You just asked the customer this confirmation question: \"{correction_text}\"\n"
            f"Mirror's analysis: items the customer ACTUALLY wants = [{kept_str}]. "
            f"Items they CORRECTED AWAY FROM = [{removed_str}].\n\n"
            "Decision rules for THIS turn:\n"
            "1. If the customer's next message is a confirmation "
            "(\"yes\", \"that's right\", \"yep\", \"correct\", \"mm-hmm\", "
            "\"sounds good\", \"please\", \"go ahead\"):\n"
            f"   → IMMEDIATELY call place_order with items=[{kept_str}] "
            "and proceed to calculate_total. Do NOT include any item "
            f"from [{removed_str}].\n"
            "2. If the customer denies or asks a follow-up question:\n"
            "   → Ask one short clarifying question. Do NOT place an "
            "order yet.\n\n"
            "This instruction OVERRIDES the standard multi-item-capture "
            "rule for THIS exchange. Do NOT re-extract items from the "
            "earlier contradictory customer turn."
        )
        state.set_post_correction_override(call_uuid, override_note)

    return correction_text
