import logging
import os

from plivo import RestClient

log = logging.getLogger("mirror.tts")

_client: RestClient | None = None


def _get_client() -> RestClient:
    global _client
    if _client is None:
        _client = RestClient(
            os.getenv("PLIVO_AUTH_ID", ""),
            os.getenv("PLIVO_AUTH_TOKEN", ""),
        )
    return _client


def speak_on_call(call_uuid: str, text: str, voice: str = "WOMAN") -> None:
    """Inject TTS speech onto a live Plivo call via the call control API.

    Fire-and-forget: Plivo returns as soon as the speak is queued;
    actual audio playback runs asynchronously on Plivo's side.
    """
    if not call_uuid:
        log.warning("speak_on_call: empty call_uuid, skipping")
        return
    if not text:
        return
    client = _get_client()
    client.calls.speak(
        call_uuid=call_uuid,
        text=text,
        voice=voice,
        language="en-US",
    )
    log.info("speak call=%s voice=%s text=%s", call_uuid, voice, text)
