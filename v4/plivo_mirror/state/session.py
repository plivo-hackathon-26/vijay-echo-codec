"""``SessionState`` — the single source of truth for one call.

Validated entities live HERE, outside the model's context. Tool calls
READ from this state and never accept arguments from the model (the
zero-argument principle). This is the structural backbone of both the
wrong-action defense and the prompt-injection defense.

Holds: validated entities, confirmed intent, compiled policies, the
committed-action log, and the spoken log (what has already been voiced).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from plivo_mirror.contracts import Policy
from plivo_mirror.state.entities import EntityKind, ValidatedEntity, validate


@dataclass
class CommittedAction:
    """An irreversible action that has actually fired. Appended to the
    committed-action log so the action guard can dedupe and reason about
    what is already done."""

    tool: str
    args: dict[str, Any]
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionState:
    """Per-call source of truth. One instance per call."""

    def __init__(
        self,
        *,
        call_id: str = "",
        policies: list[Policy] | None = None,
        known_facts: dict[str, str] | None = None,
    ) -> None:
        self.call_id = call_id
        self.confirmed_intent: str | None = None
        self.compiled_policies: list[Policy] = list(policies or [])
        self._entities: dict[str, ValidatedEntity] = {}
        self._committed: list[CommittedAction] = []
        self._spoken: list[str] = []
        # Code-owned REFERENCE facts (catalog, sizes, hours, prices): written
        # OUTSIDE the model, never authored by it. Distinct from ``_entities``
        # (per-call values the caller supplied + we validated). These ground
        # the verifier so legitimate prices/hours/counts stop false-firing.
        self._known_facts: dict[str, str] = {
            k: str(v) for k, v in (known_facts or {}).items()
        }
        # Hooks fired when an action commits (e.g. clear intent memory).
        self._commit_hooks: list[Callable[[CommittedAction], None]] = []

    # ── validated-entity writes ──────────────────────────────────────

    def write_entity(
        self, key: str, kind: EntityKind, raw: str | None
    ) -> ValidatedEntity | None:
        """Validate ``raw`` as ``kind`` and, if valid, store it under
        ``key``. Returns the stored entity, or ``None`` if validation
        failed (in which case state is left unchanged)."""
        ent = validate(kind, raw)
        if ent is None:
            return None
        self._entities[key] = ent
        return ent

    def set_entity(self, key: str, entity: ValidatedEntity) -> None:
        """Store an already-validated entity directly (e.g. from a
        catalog-checked ``validate_item`` call)."""
        self._entities[key] = entity

    def get_entity(self, key: str) -> ValidatedEntity | None:
        return self._entities.get(key)

    def entity_value(self, key: str, default: Any = None) -> Any:
        ent = self._entities.get(key)
        return ent.value if ent is not None else default

    @property
    def entities(self) -> dict[str, ValidatedEntity]:
        """A read-only snapshot. Mutating the returned dict does not
        affect state."""
        return dict(self._entities)

    # ── reference facts (code-owned business config) ─────────────────

    def add_known_fact(self, key: str, value: Any) -> None:
        """Seed a code-owned reference fact (e.g. ``"wings_per_order" ->
        "6"``). Written outside the model; used to ground the verifier."""
        self._known_facts[key] = str(value)

    @property
    def known_facts(self) -> dict[str, str]:
        """Read-only snapshot of the reference facts."""
        return dict(self._known_facts)

    # ── confirmed intent ─────────────────────────────────────────────

    def confirm_intent(self, intent: str) -> None:
        self.confirmed_intent = intent

    # ── committed-action log ─────────────────────────────────────────

    def on_commit(self, hook: Callable[[CommittedAction], None]) -> None:
        """Register a callback fired whenever an action commits. Used to
        auto-clear intent memory on commit without coupling state to the
        runtime layer."""
        self._commit_hooks.append(hook)

    def log_committed_action(
        self, tool: str, args: dict[str, Any]
    ) -> CommittedAction:
        ca = CommittedAction(tool=tool, args=dict(args))
        self._committed.append(ca)
        for hook in self._commit_hooks:
            try:
                hook(ca)
            except Exception:  # a hook fault must never break a commit
                pass
        return ca

    def already_committed(self, tool: str, args: dict[str, Any]) -> bool:
        """True if this exact (tool, args) has already fired on this call —
        the dedupe guard against duplicate irreversible side effects."""
        return any(c.tool == tool and c.args == args for c in self._committed)

    @property
    def committed_actions(self) -> list[CommittedAction]:
        return list(self._committed)

    # ── spoken log ───────────────────────────────────────────────────

    def note_spoken(self, text: str) -> None:
        if text and text.strip():
            self._spoken.append(text.strip())

    @property
    def spoken(self) -> list[str]:
        return list(self._spoken)

    def has_spoken(self, substr: str) -> bool:
        s = (substr or "").strip().lower()
        if not s:
            return False
        return any(s in t.lower() for t in self._spoken)


def args_from_state(state: SessionState, keys: Iterable[str]) -> dict[str, Any]:
    """Build tool arguments by READING validated values from state — the
    zero-argument principle. An executor calls this instead of trusting
    model-supplied args:

        async def place_order(self):
            args = args_from_state(self.state, ["items"])
            ...

    Only keys that have a validated entity in state are included; a key
    with no validated entity is omitted (it was never grounded)."""
    out: dict[str, Any] = {}
    for k in keys:
        ent = state.get_entity(k)
        if ent is not None:
            out[k] = ent.value
    return out


__all__ = ["SessionState", "CommittedAction", "args_from_state"]
