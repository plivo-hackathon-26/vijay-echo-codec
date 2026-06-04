"""Post-call LLM analysis as a monitoring feature (endpoint + storage).
The judge LLM is faked; the wiring under test is real."""

from fastapi.testclient import TestClient

import plivo_mirror_v5.auditor.post_call_judge as judge_mod
from plivo_mirror_v5.deployables.monitoring.backend.app import create_app
from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore


def seeded_store():
    store = CallStore(":memory:")
    store.ingest({"type": "call_start", "mirror.call_id": "c9",
                  "mirror.agent_id": "a", "t": 1.0})
    store.ingest({"type": "turn", "mirror.call_id": "c9", "mirror.turn_id": "c9-t0",
                  "mirror.turn_index": 0, "mirror.role": "user",
                  "mirror.transcript": "how much is turbo?", "t": 2.0})
    store.ingest({"type": "turn", "mirror.call_id": "c9", "mirror.turn_id": "c9-t1",
                  "mirror.turn_index": 1, "mirror.role": "agent",
                  "mirror.transcript": "Turbo is $59.99.", "t": 3.0})
    store.ingest({"type": "call_end", "mirror.call_id": "c9", "t": 4.0})
    return store


class FakeJudgeClient:
    def complete_json(self, system, user):
        return {"violation": True, "category": "price_hallucination",
                "reason": "stated 59.99 with no grounding"}


def test_analyze_endpoint_stores_and_returns_findings(monkeypatch):
    monkeypatch.setattr(judge_mod, "_default_client", lambda: FakeJudgeClient(),
                        raising=False)
    # LLMPostCallJudge builds ChatClient when client is None — patch that path
    monkeypatch.setattr("plivo_mirror_v5.llm_client.ChatClient",
                        lambda *a, **k: FakeJudgeClient())
    store = seeded_store()
    client = TestClient(create_app(store))

    out = client.post("/calls/c9/analyze").json()
    assert out["analyzed"] is True
    [finding] = out["findings"]
    assert finding["kind"] == "missed_failure"  # inline had no fired verdicts
    assert finding["turn_id"] == "c9-t1"
    assert finding["category"] == "price_hallucination"

    # persisted into the call detail payload
    call = client.get("/calls/c9").json()
    assert call["audit"]["analyzed"] is True
    assert len(call["audit"]["findings"]) == 1

    # re-running replaces, not duplicates
    client.post("/calls/c9/analyze")
    assert len(client.get("/calls/c9").json()["audit"]["findings"]) == 1


def test_unanalyzed_call_reports_analyzed_false():
    client = TestClient(create_app(seeded_store()))
    call = client.get("/calls/c9").json()
    assert call["audit"] == {"analyzed": False, "findings": []}


def test_zero_findings_still_marks_analyzed(monkeypatch):
    class CleanJudge:
        def complete_json(self, system, user):
            return {"violation": False, "category": None, "reason": "fine"}

    monkeypatch.setattr("plivo_mirror_v5.llm_client.ChatClient",
                        lambda *a, **k: CleanJudge())
    client = TestClient(create_app(seeded_store()))
    out = client.post("/calls/c9/analyze").json()
    assert out == {"analyzed": True, "findings": []}


def test_analyze_unknown_call_404():
    client = TestClient(create_app(CallStore(":memory:")))
    assert client.post("/calls/nope/analyze").status_code == 404
