"""Deterministic check layer — runs the compiled policy checks against a
turn. A hard hit here routes straight to ``block`` (the router's
deterministic-hit path); the verifier is never consulted.

This is just the orchestration over ``Policy.run``; the checks themselves
were compiled in ``policy/compiler.py``.
"""

from __future__ import annotations

from plivo_mirror.contracts import Policy, TurnContext, Verdict


def run_deterministic(
    ctx: TurnContext, policies: list[Policy] | None = None
) -> Verdict | None:
    """Run every compiled check and return the first violating ``Verdict``
    (a ``block``), or ``None`` if nothing fired. ``policies`` defaults to
    ``ctx.state.compiled_policies``."""
    pols = policies if policies is not None else ctx.state.compiled_policies
    for p in pols:
        v = p.run(ctx)
        if v is not None and v.decision == "block":
            return v
    return None


__all__ = ["run_deterministic"]
