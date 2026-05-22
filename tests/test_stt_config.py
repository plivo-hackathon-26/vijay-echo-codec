"""Static assertions about the Deepgram config.

These are not behaviour tests — they're regression guards so a future
change can't silently drop nova-3, the domain keyterms, or smart
formatting and quietly degrade the demo.
"""

import inspect

from voice import stt


def _start_source() -> str:
    return inspect.getsource(stt.DeepgramSession.start)


def test_uses_nova_3():
    src = _start_source()
    assert 'model="nova-3"' in src


def test_pizza_keyterms_present():
    src = _start_source()
    for term in ("pepperoni", "mushroom", "cheese", "pizza"):
        assert f'"{term}"' in src, f"missing keyterm: {term}"


def test_correction_marker_keyterms_present():
    src = _start_source()
    # Mirror's contradiction rule depends on these markers — boosting
    # them in STT directly helps detection accuracy.
    for term in ("actually", "instead", "only"):
        assert f'"{term}"' in src, f"missing keyterm: {term}"


def test_smart_format_and_punctuate_enabled():
    src = _start_source()
    assert "smart_format=True" in src
    assert "punctuate=True" in src


def test_mulaw_8khz_encoding_preserved():
    src = _start_source()
    assert 'encoding="mulaw"' in src
    assert "sample_rate=8000" in src


def test_utterance_end_long_enough_for_natural_pauses():
    """Regression guard: customer must be able to pause >=2s mid-thought
    without the agent jumping in. utterance_end_ms governs when
    speech_final fires."""
    src = _start_source()
    # parse the numeric value
    import re
    m = re.search(r"utterance_end_ms=(\d+)", src)
    assert m is not None, "utterance_end_ms not set"
    assert int(m.group(1)) >= 2000, (
        f"utterance_end_ms={m.group(1)} is too short — customers will be "
        "cut off mid-sentence"
    )


def test_uses_speech_final_for_user_turn_boundary():
    """Regression guard: on_final must fire on speech_final, not on
    every is_final segment, or we'll cut the customer off mid-utterance.
    """
    import inspect
    from voice import stt
    src = inspect.getsource(stt.DeepgramSession)
    assert "speech_final" in src
    # buffer must exist so partial finals can be accumulated
    assert "_utterance_buffer" in src


def test_numerals_disabled():
    """Regression: numerals=True transforms 'only' near 'mushroom' into
    '1' which kills marker matching. Must be off."""
    src = _start_source()
    assert "numerals=False" in src, "numerals must be disabled"


def test_activity_callback_exposed():
    """Regression: stt session must accept an on_activity callback so
    the silence watcher can reset on interim transcripts (not just on
    speech_final)."""
    import inspect
    from voice import stt
    sig = inspect.signature(stt.DeepgramSession.__init__)
    assert "on_activity" in sig.parameters
