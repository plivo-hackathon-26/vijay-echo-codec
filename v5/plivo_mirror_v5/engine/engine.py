"""Engine — orchestrates L1 → L2 → L3 + arbitration for one turn.

The engine is pure detection: ``evaluate_turn`` returns a ``TurnResult``
and nothing else. It does NOT emit telemetry and does NOT take actions —
routing the result (telemetry vs. intervention hook) is the deployables'
job, selected by the observer's ``mode`` flag.

Layer discipline (the latency budget is real):
- L1 is cheap and gates the others.
- L2 is the only inline-safe detector (budget asserted in tests).
- L3 belongs out of the hot path — the observer runs the whole evaluation
  off the event loop; an inline caller (Hook B) must run L2 only.
"""

from __future__ import annotations

import time

from plivo_mirror_v5.engine.arbitration import arbitrate
from plivo_mirror_v5.engine.config import EngineConfig
from plivo_mirror_v5.engine.kb_retriever import KBRetriever
from plivo_mirror_v5.engine.layers.base import LayerContext
from plivo_mirror_v5.engine.layers.l1_input_integrity import InputIntegrityLayer
from plivo_mirror_v5.engine.layers.l2_deterministic import DeterministicDiffLayer
from plivo_mirror_v5.engine.layers.l3_grounding_nli import GroundingNLILayer, NLIScorer
from plivo_mirror_v5.engine.reference import ReferenceStore
from plivo_mirror_v5.engine.session_state import SessionState
from plivo_mirror_v5.engine.verdict import TurnInput, TurnResult, Verdict


class Engine:
    def __init__(
        self,
        config: EngineConfig,
        reference: ReferenceStore,
        kb: KBRetriever | None = None,
        nli: NLIScorer | None = None,
    ) -> None:
        self.config = config
        self.reference = reference
        self.kb = kb
        self.l1 = InputIntegrityLayer()
        self.l2 = DeterministicDiffLayer()
        self.l3 = GroundingNLILayer(nli=nli)

    def evaluate_turn(self, turn: TurnInput, state: SessionState) -> TurnResult:
        # L2 always diffs against a snapshot taken at turn start, never
        # live state — the snapshot id makes diff timing auditable.
        snapshot = state.snapshot()
        ctx = LayerContext(
            config=self.config,
            snapshot=snapshot,
            reference=self.reference,
            kb=self.kb,
        )

        verdicts: list[Verdict] = []
        if self.config.enable_l1:
            verdicts += self._timed(self.l1, turn, state, ctx)
        if self.config.enable_l2:
            verdicts += self._timed(self.l2, turn, state, ctx)
            # _resolve filled ctx.l2_claim_ids — L3 skips those claims.
        if self.config.enable_l3:
            verdicts += self._timed(self.l3, turn, state, ctx)

        arbitrate(verdicts)

        # Commit this turn's executed tool calls to the call's tool log so
        # later "I did X" claims diff against them.
        for tc in turn.tool_calls:
            state.record_tool_call(tc, turn_index=turn.turn_index)

        return TurnResult(
            turn_id=turn.turn_id,
            call_id=turn.call_id,
            turn_index=turn.turn_index,
            role=turn.role,
            transcript=turn.transcript,
            asr_confidence=turn.asr_confidence,
            state_snapshot_id=snapshot.snapshot_id,
            verdicts=verdicts,
            action=None,  # actions are the deployables' job
        )

    @staticmethod
    def _timed(layer, turn, state, ctx) -> list[Verdict]:
        start = time.perf_counter()
        verdicts = layer.check(turn, state, ctx)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        for v in verdicts:
            v.latency_ms = elapsed_ms
        return verdicts
