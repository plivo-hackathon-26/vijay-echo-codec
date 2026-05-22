import asyncio
import base64
import json
import logging
import os
import time

from fastapi import WebSocket, WebSocketDisconnect

import db
from agent.primary import run_correction_turn, run_turn
from mirror import evaluator as mirror_evaluator
from mirror import interventions as mirror_interventions
from mirror import semantic as mirror_semantic
from mirror import state as mirror_state
from prompts import GREETING
from voice.stt import DeepgramSession
from voice.tts import speak_on_call

log = logging.getLogger("mirror.stream")

_WRAPUP_MARKERS = (
    "thanks so much",
    "thanks for calling",
    "thank you for calling",
    "have a great",
    "have a good",
    "have a nice",
    "all set",
    "goodbye",
    "bye now",
    "talk to you",
)


def _is_wrapping_up(history: list) -> bool:
    """True if the agent's most recent turn sounds like a sign-off.

    Used to suppress the silence watcher once the call is essentially
    over — no point prompting "are you still there?" right after we
    said "have a great day!".
    """
    last_agent_text = ""
    for turn in reversed(history):
        if turn.get("role") == "agent":
            last_agent_text = (turn.get("text") or "").lower()
            break
    return any(marker in last_agent_text for marker in _WRAPUP_MARKERS)


async def handle_audio_stream(ws: WebSocket) -> None:
    await ws.accept()

    call_uuid = ws.query_params.get("call_uuid", "")
    # Seed transcript history with the XML greeting so the agent knows
    # what was already said.
    transcript_history: list[dict] = [{"role": "agent", "text": GREETING}]
    agent_lock = asyncio.Lock()
    # Silence-watcher state: last time the customer was heard (any
    # Deepgram final). Initialized to the WS open time so the watcher
    # will eventually prompt even if the customer never speaks.
    silence_state: dict = {"last_customer_ts": time.monotonic(), "prompted": False}

    async def on_final(text: str) -> None:
        text = text.strip()
        if not text:
            return
        # Any final transcript = customer is engaged. Reset the
        # silence watcher even if we drop the text below.
        silence_state["last_customer_ts"] = time.monotonic()
        silence_state["prompted"] = False
        if agent_lock.locked():
            log.info("dropping transcript (agent busy): %s", text)
            return
        async with agent_lock:
            log.info("customer: %s", text)
            turn_id = None
            if call_uuid:
                turn_id = db.add_turn(call_uuid, "customer", text)
            transcript_history.append({"role": "customer", "text": text})

            # Mirror evaluation runs synchronously after the turn is
            # persisted but BEFORE the primary agent fires.
            if call_uuid:
                try:
                    recent_turns = db.get_recent_turns(call_uuid, limit=10)
                    mirror_evaluator.evaluate(
                        call_uuid=call_uuid,
                        recent_turns=recent_turns,
                        current_user_turn=text,
                        current_turn_id=turn_id,
                    )
                except Exception:
                    log.exception("mirror evaluator failed (continuing)")

            # Phase 3: if Mirror flagged this turn for intervention,
            # route through the buffer+correction orchestrator instead
            # of the normal primary-agent path. The intervention IS the
            # agent's response for this turn — we do NOT call run_turn.
            pending = (
                mirror_state.get_intervention_pending(call_uuid)
                if call_uuid
                else None
            )
            if pending:
                mirror_state.clear_intervention_pending(call_uuid)
                intervention_ok = False
                try:
                    correction_text = await mirror_interventions.handle_intervention(
                        call_uuid=call_uuid,
                        pattern_result=pending,
                        history=transcript_history,
                        speak_fn=speak_on_call,
                        generate_fn=run_correction_turn,
                    )
                    intervention_ok = True
                except Exception:
                    log.exception("intervention failed; falling through to normal agent")

                if intervention_ok:
                    transcript_history.append(
                        {"role": "agent", "text": correction_text}
                    )
                    if call_uuid:
                        db.add_turn(call_uuid, "agent", correction_text)
                    # Hold the lock until the correction has finished
                    # playing on the line. Plivo's REST `speak` returns
                    # when queued, not when audio ends, so we hold for
                    # the full estimated playback plus a ~1s safety pad
                    # to keep the agent's own tail audio from leaking
                    # back to Deepgram as the next customer turn.
                    duration = 1.0 + max(2.5, len(correction_text) / 13.0)
                    await asyncio.sleep(duration)
                    return

            # Normal path: brief natural pause, then primary agent.
            await asyncio.sleep(0.3)

            # One-shot Mirror override: if the previous turn fired a
            # self-correct intervention, inject a system note that
            # overrides the rigged item-capture rule for THIS turn
            # only. Cleared immediately so it doesn't leak into later
            # turns.
            extra_note = (
                mirror_state.get_post_correction_override(call_uuid)
                if call_uuid
                else None
            )
            if extra_note and call_uuid:
                mirror_state.clear_post_correction_override(call_uuid)

            primary_tool_calls: list = []
            try:
                result = await run_turn(
                    call_uuid,
                    transcript_history,
                    extra_system_note=extra_note,
                    return_details=True,
                )
                response = result["text"]
                primary_tool_calls = result["tool_calls"]
            except Exception:
                log.exception("agent error")
                response = "Sorry, can you say that again?"

            # Semantic Mirror — runs AFTER the primary has generated its
            # response. Reviews the response + tool calls against what
            # the customer actually said and decides whether the agent
            # is about to deliver the wrong order. Skipped when the
            # pattern-based Mirror already fired (we returned above).
            semantic_verdict = None
            if call_uuid:
                try:
                    semantic_verdict = await mirror_semantic.review_response(
                        customer_text=text,
                        primary_response_text=response,
                        tool_calls=primary_tool_calls,
                        history=transcript_history,
                    )
                except Exception:
                    log.exception("semantic mirror failed (continuing with primary)")

            if semantic_verdict and semantic_verdict.get("intervention_needed"):
                # Persist as a mirror_event so the dashboard / analytics
                # see this turn was flagged.
                event_id = None
                try:
                    event_id = db.add_mirror_event(
                        call_uuid=call_uuid,
                        turn_id=turn_id,
                        pattern_name=semantic_verdict["pattern_name"],
                        severity=semantic_verdict["severity"],
                        evidence_dict=semantic_verdict["evidence"],
                        intervention_needed=True,
                    )
                except Exception:
                    log.exception("failed to persist semantic mirror_event")
                semantic_verdict["mirror_event_id"] = event_id

                prefix = call_uuid[:8] if call_uuid else "????????"
                reason = semantic_verdict["evidence"].get("reason", "")
                intent = semantic_verdict["evidence"].get("what_customer_wants", "")
                print(
                    f"\033[33m⚠ MIRROR [{prefix}]: "
                    f"semantic_mismatch (intervention) | "
                    f"reason={reason!r} intent={intent!r}\033[0m",
                    flush=True,
                )

                intervention_ok = False
                try:
                    correction_text = await mirror_interventions.handle_intervention(
                        call_uuid=call_uuid,
                        pattern_result=semantic_verdict,
                        history=transcript_history,
                        speak_fn=speak_on_call,
                        generate_fn=run_correction_turn,
                    )
                    intervention_ok = True
                except Exception:
                    log.exception(
                        "semantic intervention failed; falling through to primary's response"
                    )

                if intervention_ok:
                    # The primary's planned response is SUPPRESSED — we
                    # never speak it. The correction is what the
                    # customer hears instead.
                    transcript_history.append(
                        {"role": "agent", "text": correction_text}
                    )
                    if call_uuid:
                        db.add_turn(call_uuid, "agent", correction_text)
                    duration = 1.0 + max(2.5, len(correction_text) / 13.0)
                    await asyncio.sleep(duration)
                    return

            # No intervention from either Mirror layer — proceed with
            # the primary's planned response.
            transcript_history.append({"role": "agent", "text": response})
            log.info("agent: %s", response)

            try:
                await asyncio.to_thread(speak_on_call, call_uuid, response)
            except Exception as e:
                # Common case: caller hung up while the agent was mid-turn,
                # so the call_uuid no longer exists. Not worth a stack trace.
                log.info("speak_on_call skipped (call likely ended): %s", e)

            # Hold the lock while Plivo plays the audio so the agent's own
            # voice doesn't get transcribed back in as a customer turn.
            # Rough heuristic: ~15 chars/sec speaking rate.
            duration = max(2.0, len(response) / 15.0)
            await asyncio.sleep(duration)

    async def silence_watcher() -> None:
        """Re-engage the customer after a long stretch with no transcripts.

        Fires once per silence window — `prompted` blocks re-prompting
        until the customer speaks again (on_final resets it). Skips
        whenever the agent is mid-turn so we never talk over Plivo.
        """
        try:
            threshold_s = float(os.getenv("MIRROR_SILENCE_PROMPT_S", "30"))
        except ValueError:
            threshold_s = 30.0
        check_interval_s = 2.0
        # Keep this short — the lock is held through playback so a long
        # prompt can swallow the customer's reply if they answer right
        # as we're talking.
        prompt_text = "Hey, are you still there?"

        while True:
            try:
                await asyncio.sleep(check_interval_s)
                if agent_lock.locked():
                    continue
                if silence_state["prompted"]:
                    continue
                elapsed = time.monotonic() - silence_state["last_customer_ts"]
                if elapsed < threshold_s:
                    continue
                if _is_wrapping_up(transcript_history):
                    # Call is in its sign-off — don't ambush the
                    # customer with "still there?" after we just
                    # said goodbye.
                    silence_state["prompted"] = True
                    continue

                async with agent_lock:
                    log.info(
                        "silence: %.1fs since last customer activity — prompting",
                        elapsed,
                    )
                    silence_state["prompted"] = True
                    transcript_history.append(
                        {"role": "agent", "text": prompt_text}
                    )
                    if call_uuid:
                        db.add_turn(call_uuid, "agent", prompt_text)
                    try:
                        await asyncio.to_thread(speak_on_call, call_uuid, prompt_text)
                    except Exception as e:
                        log.info("silence prompt speak skipped: %s", e)
                    # Hold the lock through playback so the agent's own
                    # voice doesn't bleed back into Deepgram.
                    duration = 1.0 + max(2.0, len(prompt_text) / 13.0)
                    await asyncio.sleep(duration)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("silence watcher iteration failed; continuing")

    def on_activity() -> None:
        # Any transcript event (interim, buffered segment, or
        # speech_final) means the customer is engaged. Reset the
        # silence watcher so it doesn't prompt while they're talking.
        silence_state["last_customer_ts"] = time.monotonic()
        silence_state["prompted"] = False

    dg_api_key = os.getenv("DEEPGRAM_API_KEY", "")
    dg = DeepgramSession(dg_api_key, on_final, on_activity=on_activity)

    try:
        await dg.start()
    except Exception:
        log.exception("deepgram start failed; closing ws")
        await ws.close()
        if call_uuid:
            db.end_call(call_uuid, "error")
        return

    silence_task = asyncio.create_task(silence_watcher())

    media_count = 0
    media_bytes = 0
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("non-json ws frame: %s", raw[:200])
                continue

            event = data.get("event")

            if event == "start":
                meta = data.get("start", {})
                log.info("stream start: %s", meta)
                if not call_uuid:
                    call_uuid = (
                        meta.get("callId")
                        or meta.get("call_uuid")
                        or meta.get("callUuid")
                        or ""
                    )
            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    try:
                        audio = base64.b64decode(payload)
                    except Exception:
                        log.exception("base64 decode failed")
                        continue
                    media_count += 1
                    media_bytes += len(audio)
                    if media_count % 100 == 0:
                        log.info(
                            "media frames=%d bytes=%d (~%.1fs of audio)",
                            media_count,
                            media_bytes,
                            media_bytes / 8000.0,
                        )
                    await dg.send(audio)
            elif event == "stop":
                log.info("stream stop call=%s media_total=%d", call_uuid, media_count)
                break
            else:
                log.info("ws event=%s data=%s", event, data)
    except WebSocketDisconnect:
        log.info("ws disconnect call=%s", call_uuid)
    except Exception:
        log.exception("ws loop error call=%s", call_uuid)
    finally:
        silence_task.cancel()
        try:
            await silence_task
        except (asyncio.CancelledError, Exception):
            pass
        await dg.close()
        if call_uuid:
            db.end_call(call_uuid, "completed")
            mirror_state.cleanup_call(call_uuid)
