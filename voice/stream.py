import asyncio
import base64
import json
import logging
import os

from fastapi import WebSocket, WebSocketDisconnect

import db
from agent.primary import run_turn
from mirror import evaluator as mirror_evaluator
from mirror import state as mirror_state
from prompts import GREETING
from voice.stt import DeepgramSession
from voice.tts import speak_on_call

log = logging.getLogger("mirror.stream")


async def handle_audio_stream(ws: WebSocket) -> None:
    await ws.accept()

    call_uuid = ws.query_params.get("call_uuid", "")
    # Seed transcript history with the XML greeting so the agent knows
    # what was already said.
    transcript_history: list[dict] = [{"role": "agent", "text": GREETING}]
    agent_lock = asyncio.Lock()

    async def on_final(text: str) -> None:
        text = text.strip()
        if not text:
            return
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
            # persisted but BEFORE the primary agent fires. Phase 2 is
            # observation only — the intervention_pending flag set here
            # will be consumed by Phase 3.
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

            # Brief natural pause before the agent responds — feels less
            # robotic than instant turn-taking.
            await asyncio.sleep(0.3)

            try:
                response = await run_turn(call_uuid, transcript_history)
            except Exception:
                log.exception("agent error")
                response = "Sorry, can you say that again?"
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

    dg_api_key = os.getenv("DEEPGRAM_API_KEY", "")
    dg = DeepgramSession(dg_api_key, on_final)

    try:
        await dg.start()
    except Exception:
        log.exception("deepgram start failed; closing ws")
        await ws.close()
        if call_uuid:
            db.end_call(call_uuid, "error")
        return

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
        await dg.close()
        if call_uuid:
            db.end_call(call_uuid, "completed")
            mirror_state.cleanup_call(call_uuid)
