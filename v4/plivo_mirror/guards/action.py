"""``ActionGuard`` — guards the action boundary (tool call → execution).

Implements the ``Guard`` protocol. Mostly deterministic (~0ms); no LLM.
For each pending tool intent, in order:

  1. **Consistency** — proposed tool args vs validated ``SessionState``
     (the source of truth), and the spoken reply vs the action. A
     mismatch ⇒ ``block``. Also a turn-level *false-completion* check:
     the reply claims an action is done but no tool call backs it.
  2. **Authorization separation** — a SEPARATE ``AuthorizationService``
     decides if the caller may do this. Denied ⇒ ``block``. (The model
     never authorizes — this is the prompt-injection defense.)
  3. **Parameter/policy validation** — code-defined business rules
     (amount caps, refund windows, step ordering) per action.

A block carries an agent-voice re-confirm correction. Under the
zero-argument principle the executor reads its args from state, so the
consistency check is what catches the model proposing wrong args.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Callable

from plivo_mirror.authz.service import AuthorizationService
from plivo_mirror.contracts import ToolCallIntent, TurnContext, Verdict
from plivo_mirror.intervention.correction import reconfirm_correction
from plivo_mirror.state.session import SessionState

# A code-defined business-rule validator for one action.
Validator = Callable[[ToolCallIntent, SessionState], "Verdict | None"]

_COMPLETION_RE = re.compile(
    r"\b(i'?ve|i have)\s+(placed|submitted|processed|booked|charged|"
    r"refunded|cancelled|canceled|scheduled)\b"
    r"|\byour\s+(order|booking|payment|refund|request)\s+(is|has been)\s+"
    r"(placed|submitted|processed|booked|confirmed|done|cancelled|canceled)\b"
    r"|\b(all set|it'?s done|that'?s done)\b",
    re.I,
)
_REFUSAL_RE = re.compile(
    r"\bi\s+(?:will\s+not|won'?t)\s+(place|charge|book|process|submit|refund|cancel)\w*",
    re.I,
)


def _claims_completion(reply: str) -> bool:
    return bool(_COMPLETION_RE.search(reply or ""))


def _reply_refuses_action(reply: str) -> bool:
    return bool(_REFUSAL_RE.search(reply or ""))


def _norm(x) -> str:
    return str(x).strip().lower()


def _values_match(proposed, expected) -> bool:
    # Money: compare numerically so "12.5" == Decimal("12.50").
    if isinstance(expected, Decimal):
        try:
            return Decimal(_norm(proposed)) == expected
        except (InvalidOperation, ValueError):
            return False
    # Collections: order-insensitive set comparison of normalized members.
    if isinstance(proposed, (list, tuple)) or isinstance(expected, (list, tuple)):
        ps = {_norm(i) for i in (proposed if isinstance(proposed, (list, tuple)) else [proposed])}
        es = {_norm(i) for i in (expected if isinstance(expected, (list, tuple)) else [expected])}
        return ps == es
    return _norm(proposed) == _norm(expected)


def _arg_state_mismatch(
    intent: ToolCallIntent, state: SessionState
) -> tuple[str, object, object] | None:
    """First proposed arg that disagrees with a same-named validated
    entity in state, as ``(key, proposed, expected)`` — or ``None``."""
    for key, proposed in intent.args.items():
        ent = state.get_entity(key)
        if ent is None:
            continue
        if not _values_match(proposed, ent.value):
            return (key, proposed, ent.value)
    return None


def _unbacked_arg(intent: ToolCallIntent, state: SessionState) -> str | None:
    """First proposed arg whose key has NO validated entity in state — i.e.
    a model-supplied value that was never grounded. ``None`` if all args
    are state-backed."""
    for key in intent.args:
        if state.get_entity(key) is None:
            return key
    return None


def _correct_args_from_state(
    intent: ToolCallIntent, state: SessionState
) -> ToolCallIntent:
    """Return a copy of ``intent`` with every state-backed arg replaced by
    the validated value from state (the C4 correct-from-state remediation).
    Keys with no validated entity are left untouched."""
    fixed = dict(intent.args)
    for key in intent.args:
        ent = state.get_entity(key)
        if ent is not None:
            fixed[key] = ent.value
    return ToolCallIntent(
        name=intent.name,
        args=fixed,
        irreversible=intent.irreversible,
        tool_call_id=intent.tool_call_id,
    )


class ActionGuard:
    def __init__(
        self,
        *,
        authz: AuthorizationService | None = None,
        validators: dict[str, list[Validator]] | None = None,
        gated: set[str] | None = None,
        require_state_backed: set[str] | None = None,
        correct_from_state: bool | set[str] = False,
    ) -> None:
        self._authz = authz
        self._validators = validators or {}
        # Which tools to send through authorization. None ⇒ all (the
        # service's default_allow / no_rule path handles the rest).
        self._gated = gated
        # Tools for which EVERY arg must be backed by a validated state
        # entity — the zero-argument enforcement. A model-supplied arg with
        # no state backing is blocked outright (closes the unchecked path).
        self._require_state_backed = require_state_backed or set()
        # Tools for which an arg/state mismatch is REPAIRED from state
        # (and re-validated) instead of blocked. True ⇒ all tools.
        self._correct_from_state = correct_from_state

    def _is_gated(self, intent: ToolCallIntent) -> bool:
        return True if self._gated is None else intent.name in self._gated

    def _corrects(self, name: str) -> bool:
        if self._correct_from_state is True:
            return True
        if isinstance(self._correct_from_state, set):
            return name in self._correct_from_state
        return False

    async def inspect(self, context: TurnContext) -> Verdict:
        state = context.state
        intents = context.tool_intents

        # Turn-level: false completion — claims an action is done with no
        # tool call and nothing committed earlier on the call.
        if (
            not intents
            and _claims_completion(context.planned_reply)
            and not state.committed_actions
        ):
            return Verdict.block(
                reason="false completion: reply claims an action is done with no tool call",
                policy_id="false_completion",
                spoken_correction=reconfirm_correction("incomplete"),
            )

        for intent in intents:
            # 1a. zero-argument enforcement — for listed tools, every arg
            #     must be backed by a validated state entity.
            if intent.name in self._require_state_backed:
                unbacked = _unbacked_arg(intent, state)
                if unbacked is not None:
                    return Verdict.block(
                        reason=(
                            f"tool arg {unbacked!r} is not backed by validated "
                            f"state (zero-argument principle)"
                        ),
                        policy_id="arg_not_state_backed",
                        span=str(intent.args.get(unbacked)),
                        spoken_correction=reconfirm_correction("mismatch"),
                    )

            # 1b. consistency — proposed args vs validated state. When
            #     correct-from-state is enabled for this tool, repair the
            #     args from state and continue; otherwise block.
            mm = _arg_state_mismatch(intent, state)
            if mm is not None:
                if self._corrects(intent.name):
                    intent = _correct_args_from_state(intent, state)
                else:
                    key, proposed, expected = mm
                    return Verdict.block(
                        reason=(
                            f"tool arg {key!r}={proposed!r} disagrees with confirmed "
                            f"state {expected!r}"
                        ),
                        policy_id="arg_state_mismatch",
                        span=str(proposed),
                        spoken_correction=reconfirm_correction("mismatch"),
                    )

            # 1c. consistency — spoken reply refuses the action it's firing.
            if _reply_refuses_action(context.planned_reply):
                return Verdict.block(
                    reason="spoken reply refuses an action that is being fired",
                    policy_id="spoken_action_mismatch",
                    spoken_correction=reconfirm_correction("mismatch"),
                )

            # 2. authorization separation (independent of the model).
            if self._authz is not None and self._is_gated(intent):
                d = self._authz.authorize(intent.name, state=state)
                if not d.allowed:
                    return Verdict.block(
                        reason=f"not authorized: {d.reason}",
                        policy_id=d.policy_id or "unauthorized",
                        spoken_correction=reconfirm_correction("authz"),
                    )

            # 3. parameter/policy validation (code-defined business rules).
            for validator in self._validators.get(intent.name, []):
                res = validator(intent, state)
                if res is not None and res.decision != "pass":
                    if not res.spoken_correction:
                        res.spoken_correction = reconfirm_correction("policy")
                    return res

        return Verdict.pass_("action_ok")


__all__ = ["ActionGuard", "Validator"]
