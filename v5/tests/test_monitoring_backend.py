import asyncio

import pytest
from fastapi.testclient import TestClient

from plivo_mirror_v5.deployables.monitoring.backend.app import create_app
from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore
from plivo_mirror_v5.engine import Engine, EngineConfig
from plivo_mirror_v5.integrations import ConversationItem, FakeSession, MirrorObserver
from plivo_mirror_v5.telemetry import TelemetryEmitter

from helpers import REFERENCE


def replay_into(store: CallStore, room_id="room-9"):
    """Drive a small shadow call straight into the store (the
    local-exporter path: CallStore is itself a TelemetrySink)."""

    async def run():
        engine = Engine(EngineConfig(mode="shadow"), reference=REFERENCE)
        observer = MirrorObserver(engine, TelemetryEmitter(store),
                                  agent_id="aurora", agent_version="1.0.0")
        session = FakeSession(room_id=room_id)
        observer.attach(session)
        session.add_item(ConversationItem(role="user", text="price of turbo?",
                                          asr_confidence=0.95))
        session.add_item(ConversationItem(
            role="agent", text="The Turbo plan is $59.99 a month.",
            claims=[{"claim_id": "c1", "claim_type": "price",
                     "spoken_value": "$59.99",
                     "ref": "reference.plan.turbo.price_per_month"}],
            audio_offset_ms=4000,
        ))
        await observer.drain()
        observer.close()

    asyncio.run(run())


@pytest.fixture()
def client():
    store = CallStore(":memory:")
    replay_into(store)
    return TestClient(create_app(store))


def test_call_list_has_rollups(client):
    [call] = client.get("/calls").json()
    assert call["call_id"] == "room-9"
    assert call["agent_id"] == "aurora"
    assert call["outcome"] == "completed"
    assert call["flags_by_layer"] == {"L2": 1}
    assert call["max_severity"] == "high"
    assert call["intervention_count"] == 1  # the would_have


def test_call_detail_renders_evidence_verbatim(client):
    call = client.get("/calls/room-9").json()
    assert len(call["turns"]) == 2
    agent_turn = call["turns"][1]
    assert agent_turn["audio_offset_ms"] == 4000
    assert agent_turn["state_snapshot_id"].startswith("snap-room-9-")
    [verdict] = [v for v in agent_turn["verdicts"] if v["fired"]]
    assert verdict["evidence"] == {
        "claim_type": "price",
        "spoken_value": "$59.99",
        "truth_value": "79.99",
        "source": "reference.plan.turbo.price_per_month",
        "extra": {"claim_id": "c1"},
    }
    [action] = agent_turn["actions"]
    assert action["taken"] == "would_have"


def test_unknown_call_404(client):
    assert client.get("/calls/nope").status_code == 404


def test_ingest_endpoint_roundtrip():
    client = TestClient(create_app(CallStore(":memory:")))
    rec = {"type": "call_start", "mirror.call_id": "room-x",
           "mirror.agent_id": "a", "mirror.agent_version": "1",
           "mirror.channel": "voice", "t": 1.0}
    assert client.post("/ingest", json=rec).json() == {"ingested": 1}
    assert client.post("/ingest", json=[rec]).json() == {"ingested": 1}
    assert client.post("/ingest", json={"type": "bogus"}).status_code == 422
    [call] = client.get("/calls").json()
    assert call["call_id"] == "room-x"
