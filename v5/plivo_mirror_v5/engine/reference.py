"""ReferenceStore — deterministic keyed lookup over structured per-agent
data (menu, pricing, policies, hours).

Non-negotiable: this is exact/keyed lookup, NEVER vector search. Vector
search is fuzzy, slower, and not auditable — structured truth must resolve
deterministically or not at all. (Unstructured/prose knowledge is the
grounded LLM judge's territory, never L2's.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MISSING = object()


def _flatten(node: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = node


class ReferenceStore:
    """Nested structured data flattened to dotted keys.

    ``{"plan": {"turbo": {"price": 79.99}}}`` resolves as
    ``get("plan.turbo.price") -> 79.99``. Intermediate nodes are not
    addressable; there is no fuzzy matching by design.
    """

    def __init__(self, data: dict | None = None) -> None:
        self._flat: dict[str, Any] = {}
        if data:
            _flatten(data, "", self._flat)

    @classmethod
    def from_file(cls, path: str | Path) -> "ReferenceStore":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def get(self, key: str, default: Any = None) -> Any:
        return self._flat.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._flat

    def lookup(self, key: str) -> tuple[Any, bool]:
        """Returns ``(value, found)`` — distinguishes a stored None/0/""
        from an absent key, which decides L2 jurisdiction."""
        value = self._flat.get(key, _MISSING)
        if value is _MISSING:
            return None, False
        return value, True

    def keys(self) -> list[str]:
        return sorted(self._flat)
