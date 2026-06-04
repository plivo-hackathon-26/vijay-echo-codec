"""Tests for the four gap-closers: audio levels, audio replay endpoint,
recording analysis, LLM extractor + LLM judge (all offline via fakes)."""

import array
import math
import wave

from fastapi.testclient import TestClient

from plivo_mirror_v5.auditor import LLMPostCallJudge
from plivo_mirror_v5.deployables.monitoring.backend.app import create_app
from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore
from plivo_mirror_v5.engine.claims import LLMClaimExtractor
from plivo_mirror_v5.integrations.audio_levels import AudioLevelTap, rms_levels

from helpers import REFERENCE


# -- audio levels ------------------------------------------------------------

def test_rms_levels_loud_vs_silence():
    rate = 16000
    loud = array.array("h", (int(9000 * math.sin(i / 10)) for i in range(rate)))
    silent = array.array("h", [0] * rate)
    assert all(lv > 0.3 for lv in rms_levels(loud, rate))
    assert all(lv == 0.0 for lv in rms_levels(silent, rate))


def test_tap_windows_levels_per_turn():
    tap = AudioLevelTap()
    rate = 16000
    pcm = array.array("h", (int(8000 * math.sin(i / 8)) for i in range(rate)))
    tap.push_pcm("agent", pcm, rate, t_ms=1000.0)   # 1s of audio at t=1s
    levels = tap.levels_for("agent", 1000.0, 2000.0, bars=10)
    assert levels is not None and len(levels) == 10
    assert max(levels) > 0.3
    assert tap.levels_for("agent", 5000.0, 6000.0) is None  # nothing there
    assert tap.levels_for("user", 1000.0, 2000.0) is None   # other role


# -- audio replay endpoint ------------------------------------------------------

def test_audio_endpoint_serves_recording(tmp_path):
    wav_path = tmp_path / "call-rec-1.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    store = CallStore(":memory:")
    store.ingest({"type": "call_start", "mirror.call_id": "call-rec-1", "t": 1.0})
    client = TestClient(create_app(store, recordings_dir=tmp_path))

    call = client.get("/calls/call-rec-1").json()
    assert call["has_audio"] is True
    audio = client.get("/calls/call-rec-1/audio")
    assert audio.status_code == 200
    assert audio.content[:4] == b"RIFF"
    assert client.get("/calls/other/audio").status_code == 404
    assert client.get("/calls/..%2Fsecret/audio").status_code == 404


# -- LLM claim extractor (fake client) --------------------------------------

class FakeChat:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def complete_json(self, system, user):
        self.prompts.append((system, user))
        return self.payload


def test_llm_extractor_maps_claims_and_drops_invented_refs():
    fake = FakeChat({"claims": [
        {"claim_type": "price", "spoken_value": "59.99",
         "ref": "reference.plan.turbo.price_per_month", "text": "..."},
        {"claim_type": "fact", "spoken_value": "covers 5000 sqft",
         "ref": "reference.made.up.key", "text": "..."},
    ]})
    ex = LLMClaimExtractor(REFERENCE, client=fake, tools=["cancel_service"])
    claims = ex.extract_from_text("The Turbo plan is $59.99 and covers 5000 sqft.")
    assert claims[0]["ref"] == "reference.plan.turbo.price_per_month"
    assert claims[1]["ref"] is None  # hallucinated key neutralized → judge jurisdiction
    # the prompt exposes keys + tools but never truth values
    _, user = fake.prompts[0]
    assert "plan.turbo.price_per_month" in user
    assert "cancel_service" in user
    assert "79.99" not in user


def test_llm_extractor_falls_back_to_lexicon_on_error():
    class Boom:
        def complete_json(self, *_a):
            raise RuntimeError("model down")

    ex = LLMClaimExtractor(REFERENCE, client=Boom())
    [c] = ex.extract_from_text("The Turbo plan is $59.99 a month.")
    assert c["ref"] == "reference.plan.turbo.price_per_month"  # lexicon path


# -- LLM post-call judge (fake client) ------------------------------------------

def make_call(fired=False):
    return {
        "call_id": "c1",
        "turns": [
            {"role": "user", "turn_id": "c1-t0", "transcript": "how much is turbo?",
             "verdicts": []},
            {"role": "agent", "turn_id": "c1-t1",
             "transcript": "Turbo is $59.99 a month.",
             "verdicts": ([{"fired": True, "severity": "high", "verdict_id": "v1"}]
                          if fired else [])},
        ],
    }


def test_judge_flags_missed_failure():
    judge = LLMPostCallJudge(
        FakeChat({"violation": True, "category": "price_hallucination",
                  "reason": "said 59.99, reference says 79.99"}),
        facts={"price_turbo": "$79.99"},
    )
    [finding] = judge.audit_call(make_call(fired=False))
    assert finding.kind == "missed_failure"
    assert finding.turn_id == "c1-t1"
    assert finding.extra["category"] == "price_hallucination"


def test_judge_flags_inline_false_alarm():
    judge = LLMPostCallJudge(
        FakeChat({"violation": False, "category": None, "reason": "reply is fine"}))
    [finding] = judge.audit_call(make_call(fired=True))
    assert finding.kind == "false_alarm"
    assert finding.verdict_id == "v1"


def test_judge_agreement_yields_no_findings():
    judge = LLMPostCallJudge(
        FakeChat({"violation": True, "category": "x", "reason": "bad"}))
    assert judge.audit_call(make_call(fired=True)) == []
