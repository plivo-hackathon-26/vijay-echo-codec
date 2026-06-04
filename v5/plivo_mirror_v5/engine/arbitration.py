"""Arbitration — deterministic wins.

If L2 produced a verdict for a claim (i.e. had jurisdiction), any L3
verdict on the same claim is suppressed: it stops firing and records
``suppressed_by=["L2"]`` so the suppression is auditable. Never emit two
firing verdicts for the same underlying claim. This keeps the false-alarm
budget from compounding across layers.
"""

from __future__ import annotations

from plivo_mirror_v5.engine.verdict import Verdict

# Strict precedence, strongest first.
_PRECEDENCE = ("L2", "L3")


def arbitrate(verdicts: list[Verdict]) -> list[Verdict]:
    """Apply precedence in place; returns the same list for convenience."""
    by_claim: dict[str, list[Verdict]] = {}
    for v in verdicts:
        if v.claim_id is not None:
            by_claim.setdefault(v.claim_id, []).append(v)

    for claim_verdicts in by_claim.values():
        present = {v.detector for v in claim_verdicts}
        winner = next((d for d in _PRECEDENCE if d in present), None)
        if winner is None:
            continue
        for v in claim_verdicts:
            if v.detector == winner or v.detector not in _PRECEDENCE:
                continue
            if v.fired:
                v.evidence.extra["fired_before_suppression"] = True
                v.fired = False
            v.suppressed_by.append(winner)
    return verdicts
