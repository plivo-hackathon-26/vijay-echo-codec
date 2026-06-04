"""KBRetriever — retrieval over the *unstructured* prose knowledge base.

Used by L3 ONLY. A per-agent vector DB is the L3 retriever — never a
source of structured truth (that is the ReferenceStore's job).

Behind a Protocol so the engine and tests run with no embedding model, no
network, and no API keys:

- ``KeywordKBRetriever`` — offline default: token-overlap scoring over
  chunks loaded from a JSON file. Deterministic and dependency-free.
- ``FakeKBRetriever``    — test stub returning canned chunks.
- A real vector implementation (embeddings + ANN index) drops in later.
  # TODO: vector retriever (per-agent embedding index) — post-v5.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset(
    "a an the is are was were be been it its this that of to in on for with and or "
    "our your my we you i they he she at by as do does did not".split()
)


def tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


@dataclass(frozen=True)
class KBChunk:
    chunk_id: str
    text: str
    score: float = 0.0


@runtime_checkable
class KBRetriever(Protocol):
    def retrieve(self, query: str, k: int = 3) -> list[KBChunk]: ...


class KeywordKBRetriever:
    """Offline retriever: ranks chunks by content-token overlap with the
    query. Good enough to exercise L3 end-to-end without a model."""

    def __init__(self, chunks: list[dict]) -> None:
        # chunks: [{"chunk_id": ..., "text": ...}, ...]
        self._chunks = [
            (c["chunk_id"], c["text"], tokenize(c["text"])) for c in chunks
        ]

    @classmethod
    def from_file(cls, path: str | Path) -> "KeywordKBRetriever":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def retrieve(self, query: str, k: int = 3) -> list[KBChunk]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scored = []
        for chunk_id, text, c_tokens in self._chunks:
            overlap = len(q_tokens & c_tokens) / len(q_tokens)
            if overlap > 0:
                scored.append(KBChunk(chunk_id=chunk_id, text=text, score=overlap))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:k]


class FakeKBRetriever:
    """Test stub: always returns the canned chunks, highest score first."""

    def __init__(self, chunks: list[KBChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.queries: list[str] = []

    def retrieve(self, query: str, k: int = 3) -> list[KBChunk]:
        self.queries.append(query)
        return self.chunks[:k]
