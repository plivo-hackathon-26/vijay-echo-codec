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

log = logging.getLogger("plivo_mirror_v5.recording")

TARGET_RATE = 16_000
_INT16_MAX = 32_767
_INT16_MIN = -32_768


def _to_int16(pcm) -> "array.array":
    """Coerce a frame's data (bytes / memoryview / array) to an int16 array,
    regardless of how LiveKit hands it over."""
    if isinstance(pcm, array.array) and pcm.typecode == "h":
        return pcm
    a = array.array("h")
    try:
        a.frombytes(bytes(pcm))
    except (TypeError, ValueError):
        return array.array("h")
    return a


class CallRecorder:
    """Buffers (offset_ms, int16 samples) and renders a mono 16 kHz WAV."""

    def __init__(self, *, max_seconds: float = 3600.0) -> None:
        self.target_rate = TARGET_RATE
        self._frames: list[tuple[float, array.array]] = []
        self._max_samples = int(max_seconds * self.target_rate)
        self._dropped = False

    def add(self, role: str, pcm, sample_rate: int, t_ms: float) -> None:
        """Record one frame at its t0-relative offset. ``role`` is accepted
        for symmetry with the tap but both roles share one mono timeline."""
        try:
            samples = _to_int16(pcm)
            if not samples:
                return
            if sample_rate and sample_rate != self.target_rate:
                samples = _resample(samples, sample_rate, self.target_rate)
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


def _resample(samples: "array.array", src_rate: int, dst_rate: int) -> "array.array":
    """Cheap nearest-sample resample (only used if the tap ever delivers a
    non-target rate; with a 16 kHz capture this is a no-op path)."""
    if src_rate == dst_rate or not samples:
        return samples
    ratio = dst_rate / src_rate
    out_len = int(len(samples) * ratio)
    out = array.array("h", bytes(2 * out_len))
    for i in range(out_len):
        out[i] = samples[min(len(samples) - 1, int(i / ratio))]
    return out
