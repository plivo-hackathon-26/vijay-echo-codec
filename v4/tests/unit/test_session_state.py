"""Phase 1 — SessionState: validated writes, committed log, spoken log."""

from __future__ import annotations

from decimal import Decimal

from plivo_mirror.state.session import SessionState


def test_write_entity_valid_stores_typed_value():
    st = SessionState(call_id="c1")
    ent = st.write_entity("refund_amount", "amount", "$25.00")
    assert ent is not None
    assert st.entity_value("refund_amount") == Decimal("25.00")
    assert st.get_entity("refund_amount").kind == "amount"


def test_write_entity_invalid_leaves_state_unchanged():
    st = SessionState()
    assert st.write_entity("amt", "amount", "free") is None
    assert st.get_entity("amt") is None
    assert st.entity_value("amt", default="NONE") == "NONE"


def test_entities_snapshot_is_readonly_copy():
    st = SessionState()
    st.write_entity("name", "name", "Jane")
    snap = st.entities
    snap.clear()
    assert st.get_entity("name") is not None  # mutating copy did not affect state


def test_committed_action_log_and_dedupe():
    st = SessionState()
    assert st.already_committed("place_order", {"id": 1}) is False
    st.log_committed_action("place_order", {"id": 1})
    assert st.already_committed("place_order", {"id": 1}) is True
    assert st.already_committed("place_order", {"id": 2}) is False
    assert len(st.committed_actions) == 1


def test_committed_args_are_copied():
    st = SessionState()
    args = {"id": 1}
    st.log_committed_action("place_order", args)
    args["id"] = 999  # mutate caller's dict
    assert st.already_committed("place_order", {"id": 1}) is True


def test_spoken_log_and_has_spoken():
    st = SessionState()
    st.note_spoken("Your total is twelve dollars.")
    st.note_spoken("   ")  # ignored
    assert st.spoken == ["Your total is twelve dollars."]
    assert st.has_spoken("TWELVE dollars") is True
    assert st.has_spoken("refund") is False
    assert st.has_spoken("") is False


def test_confirm_intent():
    st = SessionState()
    assert st.confirmed_intent is None
    st.confirm_intent("one large mushroom pizza")
    assert st.confirmed_intent == "one large mushroom pizza"
