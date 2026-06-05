"""ToolGate — pre-execution block for irreversible actions.

The action-boundary fix: stop the tool BEFORE the side effect, not just
correct the speech after. Deterministic; no model.
"""

from plivo_mirror_v5.engine import PolicyPack, SessionState, ToolGate

BANK_PACK = PolicyPack.from_dict({
    "tool_authorization": {
        "transfer_funds": "session.auth.identity_verified",
        "dispute_fee": {"requires": "session.auth.fee_waiver_authorized",
                        "when_arg_truthy": "waive"},
    },
    "arg_bindings": {"transfer_funds": {"to_account": "session.verified.account"}},
})


def gate():
    return ToolGate(BANK_PACK)


def test_blocks_transfer_without_identity_verification():
    state = SessionState("c")           # nothing verified
    d = gate().check("transfer_funds", {"amount": 2000, "to_account": "5582"}, state)
    assert not d.allow and not d        # __bool__ reads as falsey
    assert d.policy_id == "authz:transfer_funds"
    assert "identity_verified" in d.reason
    assert d.spoken_refusal             # a safe line to say instead


def test_allows_transfer_once_host_verified_identity():
    state = SessionState("c")
    state.set_fact("auth.identity_verified", True, source="host")
    d = gate().check("transfer_funds", {"amount": 500, "to_account": "savings"}, state)
    assert d.allow and d


def test_conditional_waive_blocks_only_when_arg_truthy():
    state = SessionState("c")
    # normal dispute logging (no waive) is always fine
    assert gate().check("dispute_fee", {"fee_type": "overdraft"}, state).allow
    assert gate().check("dispute_fee", {"fee_type": "overdraft", "waive": False},
                        state).allow
    # waiving without supervisor authorization is blocked
    blocked = gate().check("dispute_fee", {"fee_type": "overdraft", "waive": True},
                           state)
    assert not blocked.allow and "waive=true" in blocked.reason


def test_arg_binding_mismatch_blocks_wrong_account():
    state = SessionState("c")
    state.set_fact("auth.identity_verified", True, source="host")
    state.set_fact("verified.account", "1111", source="host")
    # caller-validated account is 1111; a transfer to 9999 is the wrong action
    d = gate().check("transfer_funds", {"amount": 100, "to_account": "9999"}, state)
    assert not d.allow and d.policy_id == "args:transfer_funds.to_account"


def test_unguarded_tool_is_allowed():
    state = SessionState("c")
    assert gate().check("get_balance", {}, state).allow
    # empty pack → everything allowed (gate is opt-in via policy)
    assert ToolGate().check("transfer_funds", {"amount": 1}, state).allow
