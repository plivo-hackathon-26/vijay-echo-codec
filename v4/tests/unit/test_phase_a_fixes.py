"""Phase A foundation fixes:
  1. zero-argument enforcement (require_state_backed)
  2. correct-from-state remediation
  3. intent-memory auto-clear on commit
  4. persona reinject flows into the per-turn injection
  5. state-reading executor helper
"""

from __future__ import annotations

from plivo_mirror.contracts import ToolCallIntent, TurnContext, Verdict
from plivo_mirror.guards.action import ActionGuard
from plivo_mirror.runtime.grounding import (
    compose_injection,
    intent_note_block,
    persona_reinject_block,
)
from plivo_mirror.runtime.intent_memory import IntentMemory
from plivo_mirror.runtime.persona_guard import PersonaGuard
from plivo_mirror.state.entities import ValidatedEntity
from plivo_mirror.state.session import SessionState, args_from_state


def _ctx(reply="", intents=None, state=None):
    return TurnContext(
        state=state or SessionState(), planned_reply=reply, tool_intents=intents or []
    )


# ── 1. zero-argument enforcement ──────────────────────────────────────


async def test_unbacked_arg_to_irreversible_tool_is_blocked():
    guard = ActionGuard(require_state_backed={"process_refund"})
    intent = ToolCallIntent(name="process_refund", args={"amount": "9999"}, irreversible=True)
    v = await guard.inspect(_ctx("Refunding now.", intents=[intent]))
    assert v.decision == "block"
    assert v.policy_id == "arg_not_state_backed"


async def test_state_backed_arg_passes_require_state_backed():
    st = SessionState()
    st.write_entity("amount", "amount", "$500")
    guard = ActionGuard(require_state_backed={"process_refund"})
    intent = ToolCallIntent(name="process_refund", args={"amount": "500"})  # matches state
    v = await guard.inspect(_ctx("Refunding now.", intents=[intent], state=st))
    assert v.decision == "pass"


async def test_require_state_backed_only_applies_to_listed_tools():
    guard = ActionGuard(require_state_backed={"process_refund"})
    intent = ToolCallIntent(name="lookup", args={"q": "hours"})  # not listed
    v = await guard.inspect(_ctx("Checking.", intents=[intent]))
    assert v.decision == "pass"


# ── 2. correct-from-state remediation ─────────────────────────────────


async def test_correct_from_state_repairs_mismatch_instead_of_blocking():
    st = SessionState()
    st.set_entity("items", ValidatedEntity("item", ["turkey sub"], "..."))
    guard = ActionGuard(correct_from_state={"place_order"})
    intent = ToolCallIntent(
        name="place_order", args={"items": ["turkey sub", "italian sub"]}
    )
    v = await guard.inspect(_ctx("Placing it.", intents=[intent], state=st))
    assert v.decision == "pass"  # repaired from state, not blocked


async def test_correct_from_state_still_blocks_if_validator_fails_post_repair():
    st = SessionState()
    st.set_entity("items", ValidatedEntity("item", ["turkey sub", "club sandwich"], "..."))

    def max_one_item(intent, state):
        if len(intent.args.get("items", [])) > 1:
            return Verdict.block(reason="too many items", policy_id="cap")
        return None

    guard = ActionGuard(
        correct_from_state={"place_order"},
        validators={"place_order": [max_one_item]},
    )
    intent = ToolCallIntent(name="place_order", args={"items": ["wrong"]})
    v = await guard.inspect(_ctx("Placing.", intents=[intent], state=st))
    # repaired to the 2-item state value, which the validator then rejects
    assert v.decision == "block"
    assert v.policy_id == "cap"


async def test_without_correct_from_state_mismatch_still_blocks():
    st = SessionState()
    st.set_entity("items", ValidatedEntity("item", ["turkey sub"], "..."))
    guard = ActionGuard()  # default: no correction
    intent = ToolCallIntent(name="place_order", args={"items": ["italian sub"]})
    v = await guard.inspect(_ctx("Placing.", intents=[intent], state=st))
    assert v.decision == "block"
    assert v.policy_id == "arg_state_mismatch"


# ── 3. intent-memory auto-clear on commit ─────────────────────────────


def test_intent_memory_cleared_on_commit():
    st = SessionState()
    mem = IntentMemory()
    mem.hold("mushroom only", turns=5)
    st.on_commit(lambda _ca: mem.clear())
    st.log_committed_action("place_order", {"items": ["mushroom only"]})
    assert mem.active is None  # commit cleared it (not just TTL)


def test_commit_hook_fault_does_not_break_commit():
    st = SessionState()
    st.on_commit(lambda _ca: 1 / 0)
    ca = st.log_committed_action("place_order", {"x": 1})  # must not raise
    assert ca.tool == "place_order"


# ── 4. persona reinject flows into injection ──────────────────────────


def test_persona_reinject_text_reaches_injection():
    pg = PersonaGuard(system_summary="You are Bob.", reinject_every=1, escalate_after=0)
    sig = pg.observe_turn()
    assert sig.reinject is True and sig.reinject_text == "You are Bob."
    injected = compose_injection(persona_reinject_block(sig.reinject_text))
    assert "You are Bob." in injected


def test_compose_injection_joins_nonempty_only():
    assert compose_injection("", "", "") == ""
    out = compose_injection("FACTS: a", intent_note_block("mushroom"), persona_reinject_block("Bob"))
    assert "FACTS: a" in out and "mushroom" in out and "Bob" in out


# ── 5. state-reading executor helper ──────────────────────────────────


def test_args_from_state_reads_only_backed_keys():
    st = SessionState()
    st.set_entity("items", ValidatedEntity("item", ["turkey sub"], "..."))
    args = args_from_state(st, ["items", "not_set"])
    assert args == {"items": ["turkey sub"]}  # ungrounded key omitted
