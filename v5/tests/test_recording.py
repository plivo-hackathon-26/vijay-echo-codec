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


def test_recorder_renders_t0_aligned_wav():
    rec = CallRecorder()
    # 0.1s of tone at t=0 (user), 0.1s at t=500ms (agent); 16 kHz mono.
    rec.add("user", _pcm(1000, 1600), 16_000, t_ms=0.0)
    rec.add("agent", _pcm(2000, 1600), 16_000, t_ms=500.0)
    wav = rec.render_wav()
    assert wav and wav[:4] == b"RIFF"

    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getframerate() == 16_000
        frames = array("h"); frames.frombytes(w.readframes(w.getnframes()))
    # tone placed at offset 0, silence in the gap, tone again at 500ms (=8000)
    assert abs(frames[10] - 1000) < 5
    assert frames[4000] == 0                 # 250ms gap → silence
    assert abs(frames[8050] - 2000) < 5


def test_recorder_empty_is_none():
    assert CallRecorder().render_wav() is None


def test_recorder_overlap_sums_and_clips():
    rec = CallRecorder()
    rec.add("user", _pcm(20000, 800), 16_000, t_ms=0.0)
    rec.add("agent", _pcm(20000, 800), 16_000, t_ms=0.0)   # exact overlap
    with wave.open(io.BytesIO(rec.render_wav()), "rb") as w:
        frames = array("h"); frames.frombytes(w.readframes(w.getnframes()))
    assert frames[10] == 32_767              # 20000+20000 clipped to int16 max


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
