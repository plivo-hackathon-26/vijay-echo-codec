"""CallRecorder render + the backend audio-upload endpoint.

Offline: synthetic PCM, no LiveKit. The recorder's WAV render and the
worker→backend upload path are what make the dashboard player "real".
"""

import io
import wave
from array import array

from fastapi.testclient import TestClient

from plivo_mirror_v5.deployables.monitoring.backend.app import create_app
from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore
from plivo_mirror_v5.integrations.recording import CallRecorder


def _pcm(value: int, n: int) -> bytes:
    return array("h", [value] * n).tobytes()


def test_recorder_concatenates_contiguously():
    rec = CallRecorder()
    # frames append in arrival order → continuous waveform, no gaps/overlap.
    rec.add("user", _pcm(1000, 1600), 16_000)
    rec.add("agent", _pcm(2000, 1600), 16_000)
    wav = rec.render_wav()
    assert wav and wav[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getframerate() == 16_000
        frames = array("h"); frames.frombytes(w.readframes(w.getnframes()))
    assert len(frames) == 3200               # 1600 + 1600, contiguous
    assert abs(frames[10] - 1000) < 5        # first frame
    assert abs(frames[2000] - 2000) < 5      # second frame immediately follows
    assert 0 not in frames                   # no silence gaps inserted


def test_recorder_empty_is_none():
    assert CallRecorder().render_wav() is None


def _wav_bytes() -> bytes:
    rec = CallRecorder()
    rec.add("user", _pcm(1000, 1600), 16_000, t_ms=0.0)
    return rec.render_wav()


def test_audio_upload_then_serve(tmp_path):
    client = TestClient(create_app(CallStore(":memory:"), recordings_dir=tmp_path))
    client.post("/ingest", json={"type": "call_start", "mirror.call_id": "rec1",
                                 "t": 1.0})
    assert client.get("/calls/rec1").json()["has_audio"] is False
    r = client.post("/calls/rec1/audio", content=_wav_bytes(),
                    headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200 and r.json()["stored"] == "rec1"
    assert client.get("/calls/rec1").json()["has_audio"] is True
    audio = client.get("/calls/rec1/audio")
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"


def test_audio_upload_guards(tmp_path):
    client = TestClient(create_app(CallStore(":memory:"), recordings_dir=tmp_path))
    # id with a space fails _SAFE_CALL_ID (no slashes/spaces allowed)
    assert client.post("/calls/bad id/audio", content=b"x").status_code == 422
    assert client.post("/calls/ok/audio", content=b"").status_code == 422   # empty body


def test_audio_upload_respects_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("MIRROR_API_KEY", "sekret")
    client = TestClient(create_app(CallStore(":memory:"), recordings_dir=tmp_path))
    assert client.post("/calls/c/audio", content=_wav_bytes()).status_code == 401
    assert client.post("/calls/c/audio", content=_wav_bytes(),
                       headers={"X-API-Key": "sekret"}).status_code == 200


# -- session-level capture (makes console-mode calls recordable) --------------

class _Frame:
    def __init__(self, value, n=160):
        self.data = array("h", [value] * n).tobytes()
        self.sample_rate = 16_000
        self.num_channels = 1


async def _aiter(items):
    for it in items:
        yield it


class _FakeAgent:
    """Minimal agent with default-style stt/tts nodes to wrap."""
    async def stt_node(self, audio, model_settings):
        async for _frame in audio:          # default consumes mic frames
            yield "speech-event"
    async def tts_node(self, text, model_settings):
        async for _t in text:               # produce one frame per text chunk
            yield _Frame(3000)


async def test_session_recorder_tees_both_roles():
    from plivo_mirror_v5.integrations.recording import install_session_recorder
    from plivo_mirror_v5.integrations.audio_levels import AudioLevelTap

    agent = _FakeAgent()
    rec = CallRecorder()
    tap = AudioLevelTap()
    clock = iter([0.0, 100.0, 200.0, 300.0, 400.0, 500.0])
    install_session_recorder(agent, recorder=rec, tap=tap,
                             now_ms=lambda: next(clock))

    # user mic frames flow through stt_node → recorded as "user"
    events = [e async for e in agent.stt_node(_aiter([_Frame(1000), _Frame(1200)]), None)]
    assert events == ["speech-event", "speech-event"]   # pipeline unchanged
    # agent TTS frames flow through tts_node → recorded as "agent", passed through
    frames = [f async for f in agent.tts_node(_aiter(["hello", "there"]), None)]
    assert len(frames) == 2 and all(isinstance(f, _Frame) for f in frames)

    wav = rec.render_wav()
    assert wav and wav[:4] == b"RIFF"        # something was captured from both
    assert tap.levels_for("user", 0, 1000) is not None
    assert tap.levels_for("agent", 0, 1000) is not None


def test_recorder_resamples_and_downmixes():
    import wave, io
    from array import array as _arr
    rec = CallRecorder()
    # 0.5s of 48 kHz STEREO (interleaved L=R=1000) → must become 16 kHz mono.
    n = 24000  # 0.5s @ 48k, per channel
    stereo = _arr("h", [1000, 1000] * n).tobytes()
    rec.add("agent", stereo, 48_000, t_ms=0.0, num_channels=2)
    wav = rec.render_wav()
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getframerate() == 16_000 and w.getnchannels() == 1
        got = w.getnframes()
    # ~0.5s at 16k ≈ 8000 frames (resampler adds a small tail); within 5%
    assert abs(got - 8000) < 400, got
