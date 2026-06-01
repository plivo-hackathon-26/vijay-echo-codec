"""The per-turn control loop — how the dual boundaries compose.

Speech boundary is inspected first (it governs what reaches the caller's
ears); the action boundary is inspected before any tool fires. The first
guard that intervenes wins — its ``Verdict`` becomes the turn outcome and
any pending tool calls are dropped by the caller.
"""

from __future__ import annotations

from plivo_mirror.contracts import Guard, TurnContext, Verdict


async def review_turn(
    speech_guard: Guard, action_guard: Guard, context: TurnContext
) -> Verdict:
    """Run the speech guard, then (if it passes) the action guard. Returns
    the first intervening verdict, or the speech guard's pass verdict."""
    speech_verdict = await speech_guard.inspect(context)
    if speech_verdict.intervened:
        return speech_verdict

    action_verdict = await action_guard.inspect(context)
    if action_verdict.intervened:
        return action_verdict

    return speech_verdict


__all__ = ["review_turn"]
