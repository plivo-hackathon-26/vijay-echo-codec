"""Layer protocol + the shared per-turn context the engine hands each
layer. Layers are stateless; everything they need arrives in ``ctx``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from plivo_mirror_v5.engine.config import EngineConfig
    from plivo_mirror_v5.engine.reference import ReferenceStore
    from plivo_mirror_v5.engine.session_state import SessionState, StateSnapshot
    from plivo_mirror_v5.engine.verdict import TurnInput, Verdict


@dataclass
class LayerContext:
    """Everything a layer may consult, fixed for the turn being evaluated.

    ``snapshot`` is the immutable state view L2 diffs against. L2 fills
    ``l2_claim_ids`` for the claims it had jurisdiction over — the audit
    trail of what was deterministically checked (arbitration's backstop
    for any same-claim verdict from another detector).

    ``commit`` is True only for the engine's real turn path. The pre-TTS
    gate re-runs L2 over DRAFTS (and over each regeneration candidate) —
    those evaluations must leave no residue in session state, or a single
    gated turn advances the disclosure turn-counter several times and a
    never-spoken draft pollutes the fire-once disclosure flags."""

    config: "EngineConfig"
    snapshot: "StateSnapshot"
    reference: "ReferenceStore"
    l2_claim_ids: set[str] = field(default_factory=set)
    commit: bool = True


@runtime_checkable
class Layer(Protocol):
    name: str

    def check(
        self, turn: "TurnInput", state: "SessionState", ctx: LayerContext
    ) -> "list[Verdict]": ...
