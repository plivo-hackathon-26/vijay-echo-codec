"""L1 — input integrity. A GATE, not a detector.

It never flags hallucinations itself. It does two things on user turns:

1. Marks the turn untrusted when ASR confidence is below threshold, so
   L2/L3 do not penalise the agent for correctly answering a
   mis-transcribed question (they downgrade to ``info`` while the gate is
   set).
2. Detects readback corrections and writes the confirmed/corrected facts
   into session state (claim-driven: a ``"correction"`` claim carries the
   ``session.*`` key and the corrected value).

All L1 verdicts are ``info`` — they are audit markers, never alarms.
"""

from __future__ import annotations

import re

from plivo_mirror_v5.engine.layers.base import LayerContext
from plivo_mirror_v5.engine.session_state import SessionState
from plivo_mirror_v5.engine.verdict import Evidence, TurnInput, Verdict, new_verdict_id

# Narrow phrasing that signals the caller is correcting a previous readback
# even when no structured correction claim was extracted.
# TODO: replace with the claim extractor's correction detection once a real
# NLU extractor lands; this regex is a coarse audit marker only.
_CORRECTION_PHRASE_RE = re.compile(
    r"\b(no,?\s+i\s+said|i\s+didn'?t\s+say|that'?s\s+not\s+what\s+i\s+said)\b",
    re.IGNORECASE,
)

_SESSION_PREFIX = "session."


class InputIntegrityLayer:
    name = "L1"

    def check(
        self, turn: TurnInput, state: SessionState, ctx: LayerContext
    ) -> list[Verdict]:
        if turn.role != "user":
            return []

        verdicts: list[Verdict] = []
        cfg = ctx.config

        # 1. ASR confidence gate.
        if (
            turn.asr_confidence is not None
            and turn.asr_confidence < cfg.asr_min_confidence
        ):
            state.mark_input_trust(False)
            verdicts.append(
                Verdict(
                    verdict_id=new_verdict_id(),
                    detector=self.name,
                    fired=True,
                    severity="info",
                    latency_ms=0.0,
                    evidence=Evidence(
                        claim_type="untrusted_input",
                        spoken_value=f"{turn.asr_confidence:.2f}",
                        truth_value=f">={cfg.asr_min_confidence:.2f}",
                        source="asr_confidence",
                        extra={"turn_id": turn.turn_id},
                    ),
                )
            )
        else:
            # A confident (or unconfidenced) caller turn clears the gate.
            state.mark_input_trust(True)

        # 2. Readback corrections → session state.
        for claim in turn.claims:
            if claim.get("claim_type") != "correction":
                continue
            ref = claim.get("ref") or ""
            if not ref.startswith(_SESSION_PREFIX):
                continue
            key = ref[len(_SESSION_PREFIX):]
            previous = state.update_from_readback(
                key, claim.get("spoken_value"), turn_index=turn.turn_index
            )
            verdicts.append(
                Verdict(
                    verdict_id=new_verdict_id(),
                    detector=self.name,
                    fired=True,
                    severity="info",
                    latency_ms=0.0,
                    evidence=Evidence(
                        claim_type="correction",
                        spoken_value=str(claim.get("spoken_value")),
                        truth_value=None if previous is None else str(previous),
                        source=ref,
                        extra={"claim_id": claim.get("claim_id")},
                    ),
                )
            )

        # 3. Coarse phrase-level correction marker (no structured claim).
        if not any(c.get("claim_type") == "correction" for c in turn.claims):
            if _CORRECTION_PHRASE_RE.search(turn.transcript):
                verdicts.append(
                    Verdict(
                        verdict_id=new_verdict_id(),
                        detector=self.name,
                        fired=True,
                        severity="info",
                        latency_ms=0.0,
                        evidence=Evidence(
                            claim_type="correction_phrase",
                            spoken_value=turn.transcript,
                            truth_value=None,
                            source="transcript",
                        ),
                    )
                )

        return verdicts
