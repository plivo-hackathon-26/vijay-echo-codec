"""CallRecorder — render a live call's audio to one mono WAV.

The audio tap already receives raw int16 PCM frames per role on a shared
``t0`` clock (the same clock the per-turn ``audio_offset_ms`` use). This
collects those frames and, at call end, lays them onto one mono timeline at
their ``t0``-relative offsets (summing + clipping any overlap), so the
resulting WAV plays back aligned to the transcript — clicking a turn in the
dashboard seeks to the right moment.

stdlib only (``wave`` + ``array``); no resampling needed because the tap
captures at a fixed 16 kHz mono. Best-effort: nothing here may raise into
the call path.
"""

from __future__ import annotations

import array
import io
import logging
import wave

try:
    import audioop  # stdlib (≤3.12): proper C downmix + anti-aliased resample
except ImportError:  # pragma: no cover — 3.13 removed it; pure-python fallback
    audioop = None

log = logging.getLogger("plivo_mirror_v5.recording")

TARGET_RATE = 16_000
_INT16_MAX = 32_767
_INT16_MIN = -32_768


def _to_mono16k(pcm, sample_rate: int, num_channels: int) -> "array.array":
    """Coerce ONE frame to mono int16 at 16 kHz.

    Frames arrive at the STT/TTS native rate (often 24 k / 48 k) and may be
    multi-channel. We MUST down-mix and resample with anti-aliasing —
    nearest-neighbor decimation (the old path) aliased speech into static.
    """
    raw = bytes(pcm)
    if not raw:
        return array.array("h")
    if len(raw) % 2:                       # keep it int16-aligned
        raw = raw[:-1]
    if audioop is not None:
        try:
            if num_channels and num_channels > 1:
                raw = audioop.tomono(raw, 2, 0.5, 0.5) if num_channels == 2 \
                    else _downmix_py(raw, num_channels)
            if sample_rate and sample_rate != TARGET_RATE:
                raw, _ = audioop.ratecv(raw, 2, 1, sample_rate, TARGET_RATE, None)
        except Exception:  # noqa: BLE001 — fall through to raw
            log.debug("audioop conversion failed", exc_info=True)
    else:  # pragma: no cover
        if num_channels and num_channels > 1:
            raw = _downmix_py(raw, num_channels)
        if sample_rate and sample_rate != TARGET_RATE:
            raw = _resample_py(raw, sample_rate, TARGET_RATE)
    out = array.array("h")
    out.frombytes(raw)
    return out


class CallRecorder:
    """Buffers (offset_ms, int16 samples) and renders a mono 16 kHz WAV."""

    def __init__(self, *, max_seconds: float = 3600.0) -> None:
        self.target_rate = TARGET_RATE
        self._frames: list[tuple[float, array.array]] = []
        self._max_samples = int(max_seconds * self.target_rate)
        self._dropped = False

    def add(self, role: str, pcm, sample_rate: int, t_ms: float,
            num_channels: int = 1) -> None:
        """Record one frame at its t0-relative offset, down-mixed + resampled
        to mono 16 kHz. ``role`` is accepted for symmetry with the tap; both
        roles share one mono timeline."""
        try:
            samples = _to_mono16k(pcm, sample_rate, num_channels)
            if samples:
                self._frames.append((t_ms, samples))
        except Exception:  # noqa: BLE001 — recording is best-effort
            if not self._dropped:
                log.debug("recorder dropped a frame", exc_info=True)
                self._dropped = True

    def duration_ms(self) -> float:
        if not self._frames:
            return 0.0
        last_off, last = self._frames[-1]
        return last_off + len(last) / self.target_rate * 1000.0

    def render_wav(self) -> bytes | None:
        """One mono 16 kHz WAV with each frame placed at its offset, overlaps
        summed and clipped. None when nothing was captured."""
        if not self._frames:
            return None
        total = min(self._max_samples,
                    int(self.duration_ms() / 1000.0 * self.target_rate) + 1)
        if total <= 0:
            return None
        mix = [0] * total
        for t_ms, samples in self._frames:
            pos = int(t_ms / 1000.0 * self.target_rate)
            for i, s in enumerate(samples):
                idx = pos + i
                if 0 <= idx < total:
                    v = mix[idx] + s
                    mix[idx] = _INT16_MAX if v > _INT16_MAX else (
                        _INT16_MIN if v < _INT16_MIN else v)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.target_rate)
            w.writeframes(array.array("h", mix).tobytes())
        return buf.getvalue()


def install_session_recorder(agent, *, recorder, tap=None, now_ms) -> None:
    """Tee the agent's PIPELINE audio into the recorder (and the levels tap).

    Unlike the room tap, the STT/TTS pipeline nodes carry audio in BOTH
    ``console`` and ``dev`` modes (they're independent of the room
    transport), so this is what makes a local console call recordable.
    Wraps the agent's ``stt_node`` (the caller's mic frames → role "user")
    and ``tts_node`` (the agent's spoken frames → role "agent") in place,
    delegating to whatever the agent already had. Best-effort: a teeing
    failure never breaks the audio pipeline.
    """
    import inspect  # noqa: PLC0415

    orig_stt = agent.stt_node      # bound (class default or the agent's own)
    orig_tts = agent.tts_node

    def _feed(role: str, frame) -> None:
        try:
            t = now_ms()
            ch = getattr(frame, "num_channels", 1)
            if recorder is not None:
                recorder.add(role, frame.data, frame.sample_rate, t, num_channels=ch)
            if tap is not None:
                tap.push_pcm(role, frame.data, frame.sample_rate, ch, t_ms=t)
        except Exception:  # noqa: BLE001 — capture is cosmetic, never break audio
            log.debug("session recorder feed dropped a frame", exc_info=True)

    async def stt_node(audio, model_settings):
        async def teed():
            async for frame in audio:
                _feed("user", frame)
                yield frame
        result = orig_stt(teed(), model_settings)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return
        async for ev in result:
            yield ev

    async def tts_node(text, model_settings):
        result = orig_tts(text, model_settings)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return
        async for frame in result:
            _feed("agent", frame)
            yield frame

    agent.stt_node = stt_node
    agent.tts_node = tts_node


# Pure-python fallbacks (only used if stdlib audioop is unavailable, e.g.
# Python 3.13+). audioop is preferred — it anti-aliases on downsample.

def _downmix_py(raw: bytes, channels: int) -> bytes:
    """Average interleaved int16 channels down to mono."""
    s = array.array("h"); s.frombytes(raw)
    n = len(s) // channels
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        base = i * channels
        out[i] = int(sum(s[base + c] for c in range(channels)) / channels)
    return out.tobytes()


def _resample_py(raw: bytes, src: int, dst: int) -> bytes:
    """Linear-interpolation resample (no anti-alias; last-resort fallback)."""
    s = array.array("h"); s.frombytes(raw)
    if not s or src == dst:
        return raw
    ratio = dst / src
    out_len = int(len(s) * ratio)
    out = array.array("h", bytes(2 * out_len))
    for i in range(out_len):
        x = i / ratio
        i0 = int(x); i1 = min(len(s) - 1, i0 + 1); f = x - i0
        out[i] = int(s[i0] * (1 - f) + s[i1] * f)
    return out.tobytes()
