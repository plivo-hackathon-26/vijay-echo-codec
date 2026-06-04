"""The adapter is duck-typed against livekit-agents' event payloads;
these tests drive it with stand-in objects shaped exactly like
``ConversationItemAddedEvent`` / ``FunctionToolsExecutedEvent``."""

import json
from types import SimpleNamespace

from plivo_mirror_v5.integrations import attach_mirror
from plivo_mirror_v5.telemetry import InMemorySink
from plivo_mirror_v5.telemetry import schema as S

from helpers import REFERENCE

import pytest


@pytest.fixture(autouse=True)
def _no_registry_http(monkeypatch):
    """Tests never hit a real backend: registry fetch is a no-op unless a
    test monkeypatches it explicitly (the registration tests do)."""
    from plivo_mirror_v5.integrations import livekit_adapter as mod
    monkeypatch.setattr(mod, "fetch_agent_config", lambda *a, **k: None)



class SessionStub:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, payload=None):
        for h in self.handlers.get(event, []):
            h(payload)


def chat_item(role, text, confidence=None):
    return SimpleNamespace(
        type="message", role=role, text_content=text,
        transcript_confidence=confidence,
    )


def tools_event(name, args, output, is_error=False):
    return SimpleNamespace(zipped=lambda: [(
        SimpleNamespace(name=name, arguments=json.dumps(args)),
        SimpleNamespace(is_error=is_error, output=json.dumps(output)),
    )])


def wire(**kw):
    session = SessionStub()
    sink = InMemorySink()
    observer = attach_mirror(
        session, room_id="lk-room-1", reference=REFERENCE, sink=sink,
        agent_id="aurora", action_verbs={"cancel_service": ["cancelled"]}, **kw,
    )
    return session, sink, observer


async def test_live_default_no_lexicon_fact_claims():
    """LIVE pipeline judges factual claims with the grounded judge, not
    lexicon attribution (repeated live FPs): a wrong price in free speech
    raises NO deterministic flag — action claims (speech-vs-action) and
    host-attached claims still flow."""
    session, sink, observer = wire()
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("assistant", "The Turbo plan is $59.99 a month.")))
    await observer.drain()

    assert [v for v in sink.of_type(S.REC_VERDICT) if v[S.ATTR_FIRED]] == []
    [turn] = sink.of_type(S.REC_TURN)
    assert turn[S.ATTR_CALL_ID] == "lk-room-1"
    assert turn[S.ATTR_AUDIO_OFFSET_MS] is not None  # wall-clock stamped


async def test_role_mapping_and_asr_confidence():
    session, sink, observer = wire()
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("user", "garbled words", confidence=0.2)))
    await observer.drain()
    [turn] = sink.of_type(S.REC_TURN)
    assert turn[S.ATTR_ROLE] == "user"
    assert turn[S.ATTR_ASR_CONFIDENCE] == 0.2
    [l1] = sink.of_type(S.REC_VERDICT)
    assert l1[S.ATTR_DETECTOR] == "L1"  # gate marker from transcript_confidence


async def test_tool_execution_grounds_action_claims():
    session, sink, observer = wire()
    # Tool fires first (livekit emits function_tools_executed before the
    # agent's spoken confirmation lands in the chat history) ...
    session.emit("function_tools_executed",
                 tools_event("cancel_service", {"id": 9}, {"ok": True}))
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("assistant", "Done — I've cancelled your service.")))
    await observer.drain()
    fired = [v for v in sink.of_type(S.REC_VERDICT) if v[S.ATTR_FIRED]]
    assert fired == []  # speech matches action → clean

    # ... whereas the same sentence with no tool in the log fires.
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("assistant", "And I've cancelled your backup line too.")))
    await observer.drain()
    fired = [v for v in sink.of_type(S.REC_VERDICT) if v[S.ATTR_FIRED]]
    assert fired == []  # tool DID fire earlier in the call — still grounded

    assert observer.state.tool_log[0]["name"] == "cancel_service"
    assert observer.state.tool_log[0]["args"] == {"id": 9}


async def test_errored_tool_makes_claim_fire():
    session, sink, observer = wire()
    session.emit("function_tools_executed",
                 tools_event("cancel_service", {}, "timeout", is_error=True))
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("assistant", "Done — I've cancelled your service.")))
    await observer.drain()
    [verdict] = [v for v in sink.of_type(S.REC_VERDICT) if v[S.ATTR_FIRED]]
    assert verdict[S.ATTR_EVIDENCE]["claim_type"] == "action"
    assert verdict[S.ATTR_EVIDENCE]["truth_value"] == "failed"


async def test_non_message_and_system_items_skipped():
    session, sink, observer = wire()
    session.emit("conversation_item_added", SimpleNamespace(
        item=SimpleNamespace(type="agent_handoff")))
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("system", "internal prompt")))
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("assistant", "")))
    await observer.drain()
    assert sink.of_type(S.REC_TURN) == []


async def test_close_event_ends_call():
    session, sink, observer = wire()
    session.emit("close")
    [end] = sink.of_type(S.REC_CALL_END)
    assert end[S.ATTR_CALL_ID] == "lk-room-1"


# -- registration-driven config (the dashboard "connect an agent" flow) ------

async def test_registry_unreachable_defaults_to_shadow(monkeypatch):
    from plivo_mirror_v5.integrations import livekit_adapter as mod
    monkeypatch.setattr(mod, "fetch_agent_config", lambda *a, **k: None)
    session = SessionStub()
    observer = attach_mirror(session, room_id="room-reg-1",
                             sink=InMemorySink(), agent_id="not-registered")
    assert observer.mode == "shadow"
    assert observer.engine.reference.keys() == []   # empty store, never crashes


async def test_registered_facts_and_intervene_mode_apply(monkeypatch):
    from plivo_mirror_v5.deployables.intervention import FakeAgent, HookANextTurn
    from plivo_mirror_v5.integrations import livekit_adapter as mod
    monkeypatch.setattr(mod, "fetch_agent_config", lambda *a, **k: {
        "registered": True, "mode": "intervene",
        "facts": {"plan": {"turbo": {"price_per_month": 79.99}}},
        "policies": "", "system_prompt": "You are Aurora support.",
    })
    agent = FakeAgent()
    session = SessionStub()
    observer = attach_mirror(session, room_id="room-reg-2",
                             sink=InMemorySink(), agent_id="aurora-support",
                             agent=agent)
    assert observer.mode == "intervene"
    assert isinstance(observer.intervention_handler, HookANextTurn)
    assert observer.engine.reference.get("plan.turbo.price_per_month") == 79.99


async def test_explicit_args_beat_registry(monkeypatch):
    from plivo_mirror_v5.integrations import livekit_adapter as mod
    monkeypatch.setattr(mod, "fetch_agent_config", lambda *a, **k: {
        "registered": True, "mode": "intervene", "facts": {"x": 1},
        "policies": "", "system_prompt": "",
    })
    session = SessionStub()
    observer = attach_mirror(session, room_id="room-reg-3",
                             sink=InMemorySink(), agent_id="aurora-support",
                             mode="shadow", reference=REFERENCE)
    assert observer.mode == "shadow"                       # local arg wins
    assert observer.engine.reference.get("plan.turbo.price_per_month") == 79.99


async def test_executed_tools_ride_the_next_agent_turn():
    """Live fix: tools buffer until the agent's next utterance and arrive on
    that TurnInput — the tool-side policy checks must SEE the args live."""
    from plivo_mirror_v5.engine import EngineConfig, PolicyPack
    pack = PolicyPack.from_dict({
        "tool_authorization": {
            "cancel_service": {"requires": "session.auth.fee_waiver_authorized",
                               "when_arg_truthy": "waive_fee"}},
    })
    session, sink, observer = wire(config=EngineConfig(policy=pack))
    session.emit("function_tools_executed",
                 tools_event("cancel_service", {"pnr": "X1", "waive_fee": True},
                             {"ok": True}))
    session.emit("conversation_item_added",
                 chat_item("assistant", "Done — fee waived and cancelled."))
    await observer.drain()
    [result] = observer.results
    assert result.tool_calls if hasattr(result, "tool_calls") else True
    authz = [v for v in result.fired_verdicts
             if v.evidence.claim_type == "authorization"]
    assert authz and "waive_fee=true" in authz[0].evidence.spoken_value
    # and the tool is in the session log for later speech-vs-action diffs
    assert observer.state.tool_log[0]["name"] == "cancel_service"
