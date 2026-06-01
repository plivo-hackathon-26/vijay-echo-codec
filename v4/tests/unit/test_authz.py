"""Phase 3 — authorization separation service."""

from __future__ import annotations

from plivo_mirror.authz.service import (
    Requirement,
    RuleBasedAuthorizationService,
    requires_entity,
)
from plivo_mirror.state.entities import ValidatedEntity
from plivo_mirror.state.session import SessionState


def test_unknown_action_default_allow():
    svc = RuleBasedAuthorizationService()
    assert svc.authorize("anything", state=SessionState()).allowed is True


def test_unknown_action_default_deny():
    svc = RuleBasedAuthorizationService(default_allow=False)
    d = svc.authorize("process_refund", state=SessionState())
    assert d.allowed is False
    assert d.reason == "no_rule_default_deny"


def test_requires_entity_blocks_when_missing():
    svc = RuleBasedAuthorizationService(
        {"process_refund": [requires_entity("identity_verified")]}
    )
    d = svc.authorize("process_refund", state=SessionState())
    assert d.allowed is False
    assert d.policy_id == "requires_entity:identity_verified"


def test_requires_entity_allows_when_present():
    st = SessionState()
    st.set_entity("identity_verified", ValidatedEntity("name", "yes", "yes"))
    svc = RuleBasedAuthorizationService(
        {"process_refund": [requires_entity("identity_verified")]}
    )
    assert svc.authorize("process_refund", state=st).allowed is True


def test_predicate_exception_is_denial_not_crash():
    bad = Requirement(name="boom", predicate=lambda s: 1 / 0, reason="x")
    svc = RuleBasedAuthorizationService({"act": [bad]})
    assert svc.authorize("act", state=SessionState()).allowed is False
