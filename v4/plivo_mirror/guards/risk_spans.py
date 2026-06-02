"""Risk-span tagger — the free, deterministic signal that decides whether
a reply is worth the verifier's attention.

Flags the consequential spans the spec calls out: numbers, prices,
percentages, and commitment words (refund / discount / eligible /
guarantee …), plus a conservative proper-name heuristic. A reply with NO
risky span takes the zero-latency pass path; the verifier never sees it.

Domain-agnostic on purpose — no menu, no vertical vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# "semantic" is not produced by the lexical tagger — it is synthesized by the
# speech guard when the semantic signal (NLI) flags a contradiction with no
# lexical trigger, so the rest of the router treats it like any flagged span.
SpanKind = Literal["price", "number", "percent", "commitment", "name", "semantic"]


@dataclass(frozen=True)
class RiskSpan:
    text: str
    kind: SpanKind
    start: int
    end: int


# Commitment / liability vocabulary — verbal commitments the agent must
# not make unless state/policy authorizes them.
_COMMITMENT_WORDS = (
    "refund",
    "discount",
    "eligible",
    "eligibility",
    "guarantee",
    "guaranteed",
    "waive",
    "waived",
    "free of charge",
    "no charge",
    "promise",
    "approved",
    "approve",
    "cancel",
    "cancellation",
    "reimburse",
    "credit",
    "covered",
    "entitled",
)

_PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:dollars?|usd)\b", re.I)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?\s?percent\b", re.I)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_COMMIT_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _COMMITMENT_WORDS) + r")\b", re.I
)
# Conservative proper-name heuristic: a capitalized word NOT at the start
# of a sentence and not a common sentence-lead word.
_NAME_RE = re.compile(r"(?<=[a-z,]\s)([A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})*)")


def _add(spans: list[RiskSpan], m: re.Match, kind: SpanKind, taken: list[tuple[int, int]]) -> None:
    s, e = m.start(), m.end()
    # don't double-tag overlapping ranges (e.g. a price's digits as a number)
    if any(not (e <= ts or s >= te) for ts, te in taken):
        return
    taken.append((s, e))
    spans.append(RiskSpan(text=m.group(0).strip(), kind=kind, start=s, end=e))


def tag_risk_spans(text: str) -> list[RiskSpan]:
    """Return the risky spans in ``text``, highest-liability first
    (commitments, then prices/percents, then bare numbers, then names).
    Overlapping matches are de-duplicated by priority order."""
    if not text:
        return []
    spans: list[RiskSpan] = []
    taken: list[tuple[int, int]] = []
    for m in _COMMIT_RE.finditer(text):
        _add(spans, m, "commitment", taken)
    for m in _PRICE_RE.finditer(text):
        _add(spans, m, "price", taken)
    for m in _PERCENT_RE.finditer(text):
        _add(spans, m, "percent", taken)
    for m in _NUMBER_RE.finditer(text):
        _add(spans, m, "number", taken)
    for m in _NAME_RE.finditer(text):
        _add(spans, m, "name", taken)
    spans.sort(key=lambda s: s.start)
    return spans


__all__ = ["SpanKind", "RiskSpan", "tag_risk_spans"]
