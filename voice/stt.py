import logging

from deepgram import (
    DeepgramClient,
    LiveOptions,
    LiveTranscriptionEvents,
)

log = logging.getLogger("mirror.stt")


# Per-agent Deepgram keyterm boosts. nova-3 boosts these tokens against
# phonetic competitors, which matters a lot for proper nouns the model
# hasn't seen often (Indian city names) and for the correction markers
# Mirror's pattern detector keys on.
KEYTERMS_PIZZA = [
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
    "pizza",
    "order",
    "large",
    "medium",
    "small",
    "actually",
    "instead",
    "only",
    "just",
    "Pizza Plivo",
]

KEYTERMS_TRAVEL = [
    # Destinations (matches _BASE_PRICES in agents/travel/primary.py)
    "Mumbai",
    "Delhi",
    "Bangalore",
    "Goa",
    "Chennai",
    "Kolkata",
    "Hyderabad",
    "Pune",
    "Jaipur",
    "Kochi",
    # Domain
    "flight",
    "flights",
    "book",
    "booking",
    "ticket",
    "fly",
    "airport",
    # Brand
    "SkyPlivo",
    # Days / dates
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    "tomorrow",
    "weekend",
    # Class
    "economy",
    "business",
    "first class",
    # Correction markers (Mirror pattern detector keys on these)
    "actually",
    "instead",
    "only",
    "just",
    "no",
]


class DeepgramSession:
    """One streaming Deepgram session per call.

    Audio in: send raw mulaw 8 kHz bytes via `send()`.
    Transcripts out: `on_final(text)` is awaited whenever Deepgram
    decides the customer has actually FINISHED speaking — i.e. when a
    transcript event arrives with `speech_final=True`.

    Implementation detail: Deepgram emits `is_final` for every stable
    segment (potentially mid-utterance, on a single word like "No.").
    Firing on_final on each of those would cut the customer off the
    moment they pause for breath. So we accumulate `is_final` segments
    into a per-utterance buffer and only flush them when `speech_final`
    arrives, controlled by `utterance_end_ms`.
    """

    def __init__(self, api_key: str, on_final, on_activity=None, keyterms=None):
        self._client = DeepgramClient(api_key)
        self._conn = self._client.listen.asyncwebsocket.v("1")
        self._on_final = on_final
        # Called on any transcript activity (interim, buffered segment,
        # or speech_final). Lets the caller reset things like the
        # silence-watcher timer so it doesn't prompt while the customer
        # is actually speaking but hasn't finished yet.
        self._on_activity = on_activity
        self._utterance_buffer: list[str] = []
        # Default to pizza vocabulary so existing call sites stay
        # backward-compatible. New callers pass an agent-specific list.
        self._keyterms = list(keyterms) if keyterms is not None else list(KEYTERMS_PIZZA)

        async def _on_open(_self, *args, **kwargs):
            log.info("dg open")

        async def _on_close(_self, *args, **kwargs):
            log.info("dg close")

        async def _on_transcript(_self, result, **kwargs):
            try:
                alts = getattr(result.channel, "alternatives", None) or []
                if not alts:
                    return
                text = (alts[0].transcript or "").strip()
                if not text:
                    return

                is_final = bool(getattr(result, "is_final", False))
                speech_final = bool(getattr(result, "speech_final", False))

                # Any non-empty transcript event = customer is engaged.
                # Tell the caller so the silence watcher resets.
                if self._on_activity is not None:
                    try:
                        self._on_activity()
                    except Exception:
                        log.exception("on_activity callback failed")

                if speech_final:
                    # Customer has actually stopped talking. Combine
                    # any buffered segments with this final one and
                    # hand the full utterance to the agent.
                    self._utterance_buffer.append(text)
                    full = " ".join(self._utterance_buffer).strip()
                    self._utterance_buffer = []
                    log.info("dg utterance: %s", full)
                    await self._on_final(full)
                elif is_final:
                    # Stable segment but the customer is still going.
                    # Buffer it; do NOT fire the agent yet.
                    self._utterance_buffer.append(text)
                    log.info("dg final segment (buffered): %s", text)
                else:
                    log.info("dg interim: %s", text)
            except Exception:
                log.exception("transcript handler error")

        async def _on_utterance_end(_self, *args, **kwargs):
            # Safety net: if Deepgram signals UtteranceEnd but we
            # somehow never saw speech_final (rare), flush whatever
            # we have so the customer isn't left hanging.
            if not self._utterance_buffer:
                return
            full = " ".join(self._utterance_buffer).strip()
            self._utterance_buffer = []
            log.info("dg utterance_end fallback: %s", full)
            await self._on_final(full)

        async def _on_error(_self, error, **kwargs):
            log.error("dg error: %s", error)

        self._conn.on(LiveTranscriptionEvents.Open, _on_open)
        self._conn.on(LiveTranscriptionEvents.Close, _on_close)
        self._conn.on(LiveTranscriptionEvents.Transcript, _on_transcript)
        self._conn.on(LiveTranscriptionEvents.UtteranceEnd, _on_utterance_end)
        self._conn.on(LiveTranscriptionEvents.Error, _on_error)

    async def start(self) -> None:
        # nova-3 is markedly more accurate than nova-2-phonecall on
        # common-noun confusions ("cheese" → "cell", "pepperoni" →
        # "petroleum"). mulaw 8 kHz is what Plivo AudioStream sends.
        #
        # Endpointing tuning (the key for "let me finish my sentence"):
        # - endpointing=600    — short pause → is_final fires
        #                        (lets transcripts stabilize quickly)
        # - utterance_end_ms=2500 — longer silence → speech_final fires
        #                           (only THEN do we treat the customer
        #                            as done and call the agent)
        # So the customer can pause for up to ~2.5s mid-thought without
        # the agent jumping in.
        options = LiveOptions(
            model="nova-3",
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            interim_results=True,
            smart_format=True,
            punctuate=True,
            # numerals=True converts words like "only" → "1" when near
            # numbers/quantities, which breaks our marker matching.
            # We never need numerals in a pizza-order transcript.
            numerals=False,
            endpointing=600,
            utterance_end_ms=2500,
            vad_events=True,
            # Domain vocabulary — these terms should dominate phonetic
            # competitors. Order matters less than presence; Deepgram
            # boosts each term internally. The actual list comes from
            # __init__ so it can be swapped per active agent.
            keyterm=self._keyterms,
        )
        ok = await self._conn.start(options)
        if ok is False:
            raise RuntimeError("Deepgram connection failed to start")

    async def send(self, audio: bytes) -> None:
        await self._conn.send(audio)

    async def close(self) -> None:
        try:
            await self._conn.finish()
        except Exception:
            log.exception("deepgram finish failed")
