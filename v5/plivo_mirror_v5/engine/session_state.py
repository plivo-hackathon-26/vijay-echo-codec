"""SessionState — runtime per-call validated facts.

Session state is NOT the knowledge base: it holds what became true during
THIS call (the caller's confirmed address, the price the tool computed for
this order, the action that actually fired). The KB is static per-agent
knowledge; the two are complementary layers.

L2 always diffs against a ``snapshot()`` — an immutable view with a stable
``state_snapshot_id`` — never against live state, so a verdict can always
be audited against exactly the state version it saw.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable view of session state at a point in time."""

    snapshot_id: str
    facts: Mapping[str, Any]
    tool_log: tuple[dict, ...]
    untrusted_input: bool

    def get(self, key: str, default: Any = None) -> Any:
        return self.facts.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.facts


@dataclass
class _FactEntry:
    value: Any
    source: str | None = None      # who validated it ("tool:create_order", "readback", ...)
    turn_index: int | None = None


class SessionState:
    """Per-call validated facts + the committed tool log.

    Facts are keyed by dotted path (``"order.total"``, ``"caller.address"``)
    so claims can reference them as ``session.order.total``.
    """

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self._facts: dict[str, _FactEntry] = {}
        self._tool_log: list[dict] = []
        self._untrusted_input = False
        self._snap_counter = 0

    # -- facts ------------------------------------------------------------

    def set_fact(
        self,
        key: str,
        value: Any,
        *,
        source: str | None = None,
        turn_index: int | None = None,
    ) -> None:
        self._facts[key] = _FactEntry(value=value, source=source, turn_index=turn_index)

    def get_fact(self, key: str, default: Any = None) -> Any:
        entry = self._facts.get(key)
        return default if entry is None else entry.value

    def update_from_readback(
        self, key: str, value: Any, *, turn_index: int | None = None
    ) -> Any:
        """Write a caller readback/correction into state. Returns the value
        that was previously held (or None), for evidence payloads."""
        previous = self.get_fact(key)
        self.set_fact(key, value, source="readback", turn_index=turn_index)
        return previous

    # -- tool log ---------------------------------------------------------

    def record_tool_call(self, tool_call: dict, *, turn_index: int | None = None) -> None:
        """Append an executed tool call ({name, args, result, t_result})."""
        entry = dict(tool_call)
        entry["turn_index"] = turn_index
        self._tool_log.append(entry)

    @property
    def tool_log(self) -> list[dict]:
        return list(self._tool_log)

    # -- input trust (the L1 gate) -----------------------------------------

    def mark_input_trust(self, trusted: bool) -> None:
        """L1 sets this: when the last caller input was untrusted (low ASR
        confidence), L2/L3 downgrade verdicts on claims answering it."""
        self._untrusted_input = not trusted

    @property
    def untrusted_input(self) -> bool:
        return self._untrusted_input

    # -- snapshots ----------------------------------------------------------

    def snapshot(self) -> StateSnapshot:
        """Immutable copy of the current state with a stable id."""
        self._snap_counter += 1
        facts = {k: copy.deepcopy(e.value) for k, e in self._facts.items()}
        return StateSnapshot(
            snapshot_id=f"snap-{self.call_id}-{self._snap_counter}",
            facts=MappingProxyType(facts),
            tool_log=tuple(copy.deepcopy(tc) for tc in self._tool_log),
            untrusted_input=self._untrusted_input,
        )
