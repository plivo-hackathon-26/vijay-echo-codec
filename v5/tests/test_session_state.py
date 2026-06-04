from plivo_mirror_v5.engine import SessionState


def test_set_get_fact():
    state = SessionState("call-1")
    state.set_fact("order.total", 86.39, source="tool:create_order")
    assert state.get_fact("order.total") == 86.39
    assert state.get_fact("missing", "dflt") == "dflt"


def test_snapshot_is_immutable_and_versioned():
    state = SessionState("call-1")
    state.set_fact("order.total", 10.0)
    snap1 = state.snapshot()
    state.set_fact("order.total", 20.0)
    snap2 = state.snapshot()

    assert snap1.snapshot_id != snap2.snapshot_id
    assert snap1.get("order.total") == 10.0  # snapshot unaffected by later writes
    assert snap2.get("order.total") == 20.0
    try:
        snap1.facts["order.total"] = 99  # type: ignore[index]
        raise AssertionError("snapshot facts should be read-only")
    except TypeError:
        pass


def test_snapshot_deep_copies_mutables():
    state = SessionState("call-1")
    state.set_fact("order.items", ["margherita"])
    snap = state.snapshot()
    state.get_fact("order.items").append("cola")
    assert snap.get("order.items") == ["margherita"]


def test_update_from_readback_returns_previous():
    state = SessionState("call-1")
    state.set_fact("caller.address", "42 Helm Street")
    previous = state.update_from_readback("caller.address", "42 Elm Street")
    assert previous == "42 Helm Street"
    assert state.get_fact("caller.address") == "42 Elm Street"


def test_input_trust_gate():
    state = SessionState("call-1")
    assert state.untrusted_input is False
    state.mark_input_trust(False)
    assert state.untrusted_input is True
    assert state.snapshot().untrusted_input is True
    state.mark_input_trust(True)
    assert state.untrusted_input is False


def test_tool_log_snapshot():
    state = SessionState("call-1")
    state.record_tool_call({"name": "cancel_service", "result": {"ok": True}}, turn_index=3)
    snap = state.snapshot()
    state.record_tool_call({"name": "refund", "result": {"ok": True}}, turn_index=5)
    assert len(snap.tool_log) == 1
    assert snap.tool_log[0]["name"] == "cancel_service"
    assert snap.tool_log[0]["turn_index"] == 3
    assert len(state.tool_log) == 2
