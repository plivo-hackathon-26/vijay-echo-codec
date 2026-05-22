"""Integration test for the intervention orchestrator.

Uses fake speak/generate functions so we don't hit Plivo or Azure.
Asserts: order of utterances, cooldown set, override installed,
DB row written, latency captured.
"""

import os

import pytest

import db
from mirror import interventions, state


# Speed up the timing-sensitive parts so tests stay fast.
os.environ["MIRROR_BUFFER_DURATION_MS"] = "200"
os.environ["MIRROR_CORRECTION_TIMEOUT_S"] = "2.0"
os.environ["MIRROR_COOLDOWN_S"] = "5"


def _demo_contradiction_pattern():
    return {
        "pattern_name": "contradiction",
        "severity": "intervention",
        "strategy": "self_correct",
        "evidence": {
            "user_said": "Large pepperoni, actually mushroom only, no pepperoni",
            "items_detected": ["pepperoni", "mushroom"],
            "markers_found": ["no ", "actually"],
            "likely_kept_items": ["mushroom"],
            "likely_removed_items": ["pepperoni"],
        },
        "intervention_needed": True,
        "mirror_event_id": 999,
    }


async def test_demo_intervention_end_to_end():
    call = "test-int-demo"
    state.cleanup_call(call)
    spoken: list = []

    def fake_speak(call_uuid, text, voice="WOMAN"):
        spoken.append(text)

    async def fake_generate(call_uuid, history, evidence):
        return "Just to confirm — you'd like a mushroom pizza, no pepperoni — is that right?"

    correction = await interventions.handle_intervention(
        call_uuid=call,
        pattern_result=_demo_contradiction_pattern(),
        history=[],
        speak_fn=fake_speak,
        generate_fn=fake_generate,
    )

    # Buffer fires first, correction second
    assert len(spoken) == 2
    assert spoken[0].startswith("Sorry")
    assert spoken[1] == correction
    assert "mushroom" in correction
    assert "pepperoni" in correction

    # Cooldown is set so the next turn is suppressed
    assert state.is_in_cooldown(call) is True

    # Override note is installed for the next primary-agent turn
    override = state.get_post_correction_override(call)
    assert override is not None
    assert "mushroom" in override
    assert "place_order" in override

    state.cleanup_call(call)


async def test_handoff_intervention_uses_canned_text():
    call = "test-int-handoff"
    state.cleanup_call(call)
    spoken: list = []

    def fake_speak(call_uuid, text, voice="WOMAN"):
        spoken.append(text)

    async def fake_generate(call_uuid, history, evidence):
        raise AssertionError("LLM should not be called for handoff strategy")

    pattern = {
        "pattern_name": "missing_tool_request",
        "strategy": "handoff",
        "evidence": {"category": "past_order"},
        "mirror_event_id": 1,
    }
    correction = await interventions.handle_intervention(
        call_uuid=call,
        pattern_result=pattern,
        history=[],
        speak_fn=fake_speak,
        generate_fn=fake_generate,
    )

    assert "transfer" in correction.lower() or "connect" in correction.lower()
    assert state.is_in_cooldown(call) is True
    # Handoff does NOT install a post-correction override (the agent
    # isn't continuing the order — it's deferring to a human)
    assert state.get_post_correction_override(call) is None
    state.cleanup_call(call)


async def test_llm_timeout_falls_back_to_template():
    call = "test-int-timeout"
    state.cleanup_call(call)
    spoken: list = []

    def fake_speak(call_uuid, text, voice="WOMAN"):
        spoken.append(text)

    async def slow_generate(call_uuid, history, evidence):
        import asyncio
        await asyncio.sleep(10.0)  # exceeds MIRROR_CORRECTION_TIMEOUT_S
        return "should never appear"

    correction = await interventions.handle_intervention(
        call_uuid=call,
        pattern_result=_demo_contradiction_pattern(),
        history=[],
        speak_fn=fake_speak,
        generate_fn=slow_generate,
    )
    # Fallback template used
    assert "should never appear" not in correction
    assert "mushroom" in correction
    state.cleanup_call(call)


async def test_intervention_row_persisted():
    call = "test-int-persist"
    state.cleanup_call(call)

    def fake_speak(call_uuid, text, voice="WOMAN"):
        pass

    async def fake_generate(call_uuid, history, evidence):
        return "Just to confirm — mushroom — is that right?"

    await interventions.handle_intervention(
        call_uuid=call,
        pattern_result=_demo_contradiction_pattern(),
        history=[],
        speak_fn=fake_speak,
        generate_fn=fake_generate,
    )

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT pattern_name, strategy, buffer_text, correction_text, "
            "cached_audio_used, latency_ms, triggered_by_event_id "
            "FROM interventions WHERE call_uuid = ?",
            (call,),
        ).fetchone()
    assert row is not None
    assert row["pattern_name"] == "contradiction"
    assert row["strategy"] == "self_correct"
    assert row["triggered_by_event_id"] == 999
    assert row["latency_ms"] > 0
    state.cleanup_call(call)
