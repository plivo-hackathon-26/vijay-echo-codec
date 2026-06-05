"""AudioLevelTap — real per-turn waveform levels for the signal view.

Accumulates RMS samples per role ("user" / "agent") on a wall-clock
timeline, then slices a turn's window into N normalized bars for the
dashboard. Two feeders:

- ``tap_room(room)`` — live LiveKit: subscribes to every audio track in
  the room via ``rtc.AudioStream`` and pushes RMS per frame batch. The
  remote (caller) tracks map to ``user``; the agent's own published track
  maps to ``agent``. Guarded import — only used when a real room exists.
- ``push_pcm(role, pcm, t_ms)`` — direct PCM (the recording analyzer
  feeds whole wav files through this offline).

Levels are cosmetic telemetry: everything here is best-effort and must
never raise into the call path.
"""

from __future__ import annotations

import array
import bisect
import logging
import math
import time

log = logging.getLogger("plivo_mirror_v5.audio")

_WINDOW_MS = 100.0          # one RMS sample per 100ms of audio
_MAX_SAMPLES = 36_000       # ~1h per role; ring-buffer beyond that
# int16 full scale; levels normalize against a speech-ish ceiling so
# normal conversation spans ~0.1-1.0 rather than hugging 0.
_NORM_CEILING = 12_000.0


def rms_levels(pcm: "array.array | list[int]", sample_rate: int,
               n_channels: int = 1) -> list[float]:
    """RMS per 100ms window over int16 PCM, normalized to 0..1."""
    window = max(1, int(sample_rate * n_channels * (_WINDOW_MS / 1000.0)))
    levels = []
    for start in range(0, len(pcm), window):
        chunk = pcm[start:start + window]
        if not chunk:
            break
        acc = 0.0
        for sample in chunk:
            acc += float(sample) * float(sample)
        levels.append(min(1.0, math.sqrt(acc / len(chunk)) / _NORM_CEILING))
    return levels


class AudioLevelTap:
    def __init__(self, recorder=None) -> None:
        # per role: parallel arrays (t_ms sorted, level)
        self._t: dict[str, list[float]] = {"user": [], "agent": []}
        self._lv: dict[str, list[float]] = {"user": [], "agent": []}
        self._t0 = time.monotonic()
        # optional CallRecorder: when set, raw frames are also buffered into a
        # playable WAV (same t0 as the levels, so it aligns with turn offsets).
        self.recorder = recorder

    # -- feeding ---------------------------------------------------------

    def now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def push_level(self, role: str, level: float, t_ms: float | None = None) -> None:
        t = self._t.setdefault(role, [])
        lv = self._lv.setdefault(role, [])
        t.append(self.now_ms() if t_ms is None else t_ms)
        lv.append(max(0.0, min(1.0, level)))
        if len(t) > _MAX_SAMPLES:
            del t[:_MAX_SAMPLES // 10]
            del lv[:_MAX_SAMPLES // 10]

    def push_pcm(self, role: str, pcm, sample_rate: int,
                 n_channels: int = 1, t_ms: float | None = None) -> None:
        """Feed raw int16 PCM; one level per 100ms window starting t_ms."""
        start = self.now_ms() if t_ms is None else t_ms
        for i, level in enumerate(rms_levels(pcm, sample_rate, n_channels)):
            self.push_level(role, level, start + i * _WINDOW_MS)

    # -- querying ----------------------------------------------------------

    def levels_for(self, role: str, start_ms: float, end_ms: float,
                   bars: int = 24) -> list[float] | None:
        """Resample the role's samples inside [start_ms, end_ms] into
        ``bars`` buckets (max per bucket). None when nothing was tapped."""
        t, lv = self._t.get(role, []), self._lv.get(role, [])
        lo = bisect.bisect_left(t, start_ms)
        hi = bisect.bisect_right(t, end_ms)
        if hi <= lo:
            return None
        span = max(1e-6, end_ms - start_ms)
        out = [0.0] * bars
        for i in range(lo, hi):
            bucket = min(bars - 1, int((t[i] - start_ms) / span * bars))
            out[bucket] = max(out[bucket], lv[i])
        return out

    # -- LiveKit wiring -----------------------------------------------------

    def tap_room(self, room, *, agent_identity: str | None = None) -> None:
        """Subscribe to the room's audio tracks (caller side) and, when
        possible, the agent's own published track. Never raises."""
        try:
            import asyncio

            from livekit import rtc  # noqa: PLC0415

            local_identity = agent_identity or getattr(
                getattr(room, "local_participant", None), "identity", None)

            def _pump(track, role: str) -> None:
                async def reader() -> None:
                    try:
                        # Fixed 16 kHz mono: uniform levels AND a clean,
                        # resample-free recording timeline.
                        stream = rtc.AudioStream(track, sample_rate=16_000,
                                                 num_channels=1)
                        async for event in stream:
                            frame = event.frame
                            t_ms = self.now_ms()
                            self.push_pcm(role, frame.data, frame.sample_rate,
                                          frame.num_channels, t_ms=t_ms)
                            if self.recorder is not None:
                                self.recorder.add(role, frame.data,
                                                  frame.sample_rate, t_ms)
                    except Exception:  # noqa: BLE001
                        log.debug("audio tap reader ended", exc_info=True)
                asyncio.create_task(reader())

            @room.on("track_subscribed")
            def _on_track(track, _publication, participant) -> None:
                if getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO:
                    return
                role = "agent" if participant.identity == local_identity else "user"
                _pump(track, role)

            # The agent's own audio (TTS) is published locally; tap it too.
            local = getattr(room, "local_participant", None)
            for pub in getattr(local, "track_publications", {}).values():
                track = getattr(pub, "track", None)
                if track is not None and getattr(track, "kind", None) == rtc.TrackKind.KIND_AUDIO:
                    _pump(track, "agent")
        except Exception:  # noqa: BLE001 — cosmetic telemetry only
            log.warning("audio level tap unavailable for this room", exc_info=True)
