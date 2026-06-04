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


# -- fleet stats: /stats/overview + /stats/patterns ---------------------------

@pytest.fixture()
def fleet_client():
    """Three calls: the SAME wrong price in two of them (systemic), one
    clean; one call audited with a judge finding."""
    store = CallStore(":memory:")
    replay_into(store, room_id="fleet-1")             # wrong $59.99
    replay_into(store, room_id="fleet-2")             # wrong $59.99 again
    # clean call, different agent version
    async def clean():
        engine = Engine(EngineConfig(mode="shadow"), reference=REFERENCE)
        observer = MirrorObserver(engine, TelemetryEmitter(store),
                                  agent_id="aurora", agent_version="1.0.1")
        session = FakeSession(room_id="fleet-3")
        observer.attach(session)
        session.add_item(ConversationItem(
            role="agent", text="The Turbo plan is $79.99 a month.",
            claims=[{"claim_id": "c1", "claim_type": "price",
                     "spoken_value": "$79.99",
                     "ref": "reference.plan.turbo.price_per_month"}]))
        await observer.drain()
        observer.close()
    asyncio.run(clean())
    store.save_audit_findings("fleet-2", [
        {"turn_id": "fleet-2-t1", "kind": "missed_failure",
         "rationale": "invented promo", "category": "promo_hallucination"},
    ], t=2.0)
    return TestClient(create_app(store))


def test_stats_overview_rollups(fleet_client):
    s = fleet_client.get("/stats/overview").json()
    assert s["calls"] == 3
    assert s["flagged_calls"] == 2                    # fleet-1, fleet-2
    assert abs(s["flag_rate"] - 2 / 3) < 1e-9
    assert s["audited_calls"] == 1 and s["judge_flagged_calls"] == 1
    # per-version comparison: 1.0.0 flagged on both calls, 1.0.1 clean
    by_ver = {v["agent_version"]: v for v in s["versions"]}
    assert by_ver["1.0.0"]["calls"] == 2 and by_ver["1.0.0"]["flagged"] == 2
    assert by_ver["1.0.1"]["flagged"] == 0
    # category breakdown includes both engine (L2) and judge entries
    cats = {(c["category"], c["detector"]) for c in s["categories"]}
    assert ("price", "L2") in cats and ("promo_hallucination", "JUDGE") in cats
    # daily trend covers today with all three calls
    assert sum(d["calls"] for d in s["daily"]) == 3


def test_systemic_pattern_groups_same_wrong_fact(fleet_client):
    p = fleet_client.get("/stats/patterns").json()
    [pattern] = p["fact_patterns"]                    # one cluster, 2 calls
    assert pattern["source"] == "reference.plan.turbo.price_per_month"
    assert pattern["spoken_value"] == "$59.99"
    assert pattern["truth_value"] == "79.99"
    assert pattern["calls"] == 2
    assert sorted(pattern["call_ids"]) == ["fleet-1", "fleet-2"]
    # judge clusters need >=2 calls by default; one finding isn't systemic
    assert p["judge_clusters"] == []
    # but min_calls=1 surfaces it
    p1 = fleet_client.get("/stats/patterns", params={"min_calls": 1}).json()
    assert any(c["category"] == "promo_hallucination" for c in p1["judge_clusters"])


# -- agent registry -----------------------------------------------------------

def test_agent_register_config_and_mode_toggle():
    client = TestClient(create_app(CallStore(":memory:")))
    # register
    r = client.post("/agents", json={
        "agent_id": "aurora-support", "name": "Aurora Support",
        "system_prompt": "You are Aurora, the ISP support agent.",
        "facts": {"plan": {"turbo": {"price_per_month": 79.99}}},
        "policies": "Never promise refunds over $50.",
    })
    assert r.status_code == 200 and r.json()["mode"] == "shadow"
    # adapter-facing config
    cfg = client.get("/agents/aurora-support/config").json()
    assert cfg["registered"] and cfg["mode"] == "shadow"
    assert cfg["facts"]["plan"]["turbo"]["price_per_month"] == 79.99
    assert "Aurora" in cfg["system_prompt"]
    # dashboard toggle → intervene
    assert client.patch("/agents/aurora-support",
                        json={"mode": "intervene"}).json()["mode"] == "intervene"
    assert client.get("/agents/aurora-support/config").json()["mode"] == "intervene"
    # unregistered ids never fail the adapter
    cfg = client.get("/agents/never-registered/config").json()
    assert cfg == {"agent_id": "never-registered", "registered": False,
                   "mode": "shadow", "facts": {}, "policies": "",
                   "system_prompt": ""}
    # validation
    assert client.post("/agents", json={"agent_id": "bad id!"}).status_code == 422
    assert client.patch("/agents/aurora-support",
                        json={"mode": "yolo"}).status_code == 422
    assert client.patch("/agents/ghost",
                        json={"mode": "shadow"}).status_code == 404


def test_agent_list_includes_unregistered_seen_agents(fleet_client):
    fleet_client.post("/agents", json={"agent_id": "aurora",
                                       "name": "Aurora", "mode": "intervene"})
    agents = {a["agent_id"]: a for a in fleet_client.get("/agents").json()}
    aurora = agents["aurora"]
    assert aurora["registered"] and aurora["mode"] == "intervene"
    assert aurora["calls"] == 3 and aurora["flagged"] == 2
