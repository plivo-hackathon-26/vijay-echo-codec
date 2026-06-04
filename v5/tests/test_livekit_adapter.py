"""The adapter is duck-typed against livekit-agents' event payloads;
these tests drive it with stand-in objects shaped exactly like
``ConversationItemAddedEvent`` / ``FunctionToolsExecutedEvent``."""

import json
from types import SimpleNamespace

from plivo_mirror_v5.integrations import attach_mirror
from plivo_mirror_v5.telemetry import InMemorySink
from plivo_mirror_v5.telemetry import schema as S

from helpers import REFERENCE


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


async def test_agent_item_is_verified_with_lexicon_claims():
    session, sink, observer = wire()
    session.emit("conversation_item_added", SimpleNamespace(
        item=chat_item("assistant", "The Turbo plan is $59.99 a month.")))
    await observer.drain()

    [verdict] = [v for v in sink.of_type(S.REC_VERDICT) if v[S.ATTR_FIRED]]
    assert verdict[S.ATTR_DETECTOR] == "L2"
    assert verdict[S.ATTR_EVIDENCE]["spoken_value"] == "59.99"
    assert verdict[S.ATTR_EVIDENCE]["truth_value"] == "79.99"
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
