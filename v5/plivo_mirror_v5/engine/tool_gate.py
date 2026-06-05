"""ToolGate — deterministic PRE-EXECUTION block for irreversible actions.

The speech gate (Hook B) stops a bad *sentence* before TTS. But in the bank
demo the unauthorized $2,000 transfer still *executed* — the firewall caught
and corrected the words, yet the money moved. ToolGate closes that gap: a
host calls ``gate.check(name, args, state)`` at the TOP of a tool, BEFORE the
side effect, and aborts when the action is not authorized.

It is the action-boundary twin of the L2 policy checks (same PolicyPack,
same `tool_authorization` / `arg_bindings`), but evaluated on a SINGLE
proposed call against live state and returning an allow/deny decision the
caller can act on — rather than a verdict emitted after the fact.

Deterministic, ~µs, no model. The model never authorizes itself: the
authorizing fact can only be written to SessionState by host code.
"""

from __future__ import annotations

from dataclasses import dataclass

from plivo_mirror_v5.engine.layers.l2_deterministic import values_match
from plivo_mirror_v5.engine.policy import PolicyPack
from plivo_mirror_v5.engine.session_state import SessionState

_DEFAULT_REFUSAL = (
    "I'm not able to do that on this call without the required authorization."
)


@dataclass
class ToolDecision:
    allow: bool
    reason: str | None = None          # why it was blocked (for logs/telemetry)
    policy_id: str | None = None       # which rule blocked it
    spoken_refusal: str | None = None  # safe line the agent says instead

    def __bool__(self) -> bool:        # `if gate.check(...):` reads naturally
        return self.allow


class ToolGate:
    """Pre-execution authorization + argument check for one tool call."""

    def __init__(self, pack: PolicyPack | None = None, *,
                 refusal: str = _DEFAULT_REFUSAL) -> None:
        self.pack = pack or PolicyPack()
        self.refusal = refusal

    def check(self, name: str, args: dict, state: SessionState) -> ToolDecision:
        snap = state.snapshot()
        args = args or {}

        # 1. Authorization separation — the prompt-injection / vishing defense.
        rule = self.pack.tool_authorization.get(name)
        if rule is not None:
            if isinstance(rule, dict):
                ref = rule["requires"]
                gate_arg = rule.get("when_arg_truthy")
                triggered = (not gate_arg) or bool(args.get(gate_arg))
            else:
                ref, gate_arg, triggered = rule, None, True
            if triggered and not bool(snap.get(ref.removeprefix("session."))):
                detail = (f"{name}({gate_arg}=true)" if gate_arg else name)
                return ToolDecision(
                    allow=False,
                    reason=f"{detail} requires {ref}, which is absent — "
                           f"a spoken claim cannot grant it",
                    policy_id=f"authz:{name}",
                    spoken_refusal=self.refusal)

        # 2. Argument ↔ validated-state consistency — wrong-action defense.
        for arg_name, ref in self.pack.arg_bindings.get(name, {}).items():
            key = ref.removeprefix("session.")
            if arg_name in args and snap.has(key) \
                    and not values_match(args[arg_name], snap.get(key)):
                return ToolDecision(
                    allow=False,
                    reason=f"{name}.{arg_name}={args[arg_name]!r} contradicts "
                           f"validated {ref}={snap.get(key)!r}",
                    policy_id=f"args:{name}.{arg_name}",
                    spoken_refusal="Let me re-confirm that detail before I "
                                   "proceed.")

        return ToolDecision(allow=True)
