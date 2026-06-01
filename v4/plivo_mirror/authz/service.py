"""Authorization separation — the prompt-injection defense.

A SEPARATE service decides what the caller is permitted to do. The model
reasons about *intent*; it never *authorizes*. So even if a prompt
injection (via retrieved data, a malicious caller, a confused LLM) coaxes
the agent into emitting a ``process_refund`` tool call, this service —
which never sees the model's text and cannot be talked out of its rules —
independently decides whether THIS caller, in THIS state, may do it.

Rules are CODE (predicates over ``SessionState``), never prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from plivo_mirror.state.session import SessionState


@dataclass
class Requirement:
    """A named, code-defined precondition on session state for an action."""

    name: str
    predicate: Callable[[SessionState], bool]
    reason: str = ""


@dataclass
class AuthzDecision:
    allowed: bool
    reason: str = ""
    policy_id: str | None = None


@runtime_checkable
class AuthorizationService(Protocol):
    """Decides whether ``action`` is permitted given the session state.
    Independent of any model output."""

    def authorize(self, action: str, *, state: SessionState) -> AuthzDecision: ...


def requires_entity(key: str, *, reason: str | None = None) -> Requirement:
    """The action requires a validated entity to be present in state
    (e.g. ``requires_entity("identity_verified")``)."""
    return Requirement(
        name=f"requires_entity:{key}",
        predicate=lambda s: s.get_entity(key) is not None,
        reason=reason or f"requires {key} to be present in verified state",
    )


class RuleBasedAuthorizationService:
    """Default authorization service. Configured with per-action
    requirement lists. Unknown actions fall back to ``default_allow``
    (set it ``False`` for a deny-by-default posture on sensitive stacks).
    """

    def __init__(
        self,
        rules: dict[str, list[Requirement]] | None = None,
        *,
        default_allow: bool = True,
    ) -> None:
        self._rules = rules or {}
        self._default_allow = default_allow

    def authorize(self, action: str, *, state: SessionState) -> AuthzDecision:
        reqs = self._rules.get(action)
        if reqs is None:
            return AuthzDecision(
                allowed=self._default_allow,
                reason="no_rule" if self._default_allow else "no_rule_default_deny",
            )
        for r in reqs:
            try:
                ok = bool(r.predicate(state))
            except Exception:
                ok = False  # a failing predicate is a denial, never a crash
            if not ok:
                return AuthzDecision(allowed=False, reason=r.reason, policy_id=r.name)
        return AuthzDecision(allowed=True, reason="authorized")


__all__ = [
    "Requirement",
    "AuthzDecision",
    "AuthorizationService",
    "RuleBasedAuthorizationService",
    "requires_entity",
]
