"""Authorization separation service — independent of the model."""

from __future__ import annotations

from plivo_mirror.authz.service import (
    AuthorizationService,
    AuthzDecision,
    Requirement,
    RuleBasedAuthorizationService,
    requires_entity,
)

__all__ = [
    "AuthorizationService",
    "AuthzDecision",
    "Requirement",
    "RuleBasedAuthorizationService",
    "requires_entity",
]
