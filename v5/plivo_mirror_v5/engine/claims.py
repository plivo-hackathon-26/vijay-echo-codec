"""Claim extraction for LIVE transcripts.

Eval fixtures attach claims by hand; a live call does not. The
``LexiconClaimExtractor`` is the offline baseline that closes that gap:

- **Structured fact claims** — the lexicon is DERIVED from the agent's own
  ``ReferenceStore`` (so it stays vertical-agnostic): a key's distinctive
  path tokens are the sentence triggers, its value-type tokens (price /
  hours / days …) pick the claim type AND a type-bound value regex.
  "The Turbo plan is $59.99 a month" + key ``plan.turbo.price_per_month``
  → a ``price`` claim with ``ref=reference.plan.turbo.price_per_month``.
- **Action claims** — a host-supplied ``action_verbs`` map
  (``{"cancel_service": ["cancelled", "canceled"]}``) turns "I've cancelled
  your service" into an ``action`` claim with ``ref=tool.cancel_service``,
  which L2 diffs against the tool log (speech-vs-action).

Deliberately conservative: a claim is only produced when BOTH the key's
triggers and a value of the right TYPE appear in one sentence — a missed
claim is a recall gap; a wrong claim would be a false alarm, which is the
budget we protect.

``LLMClaimExtractor`` is the NLU upgrade: an OpenAI-compatible model maps
each agent sentence onto the SAME claim schema, constrained to the
reference keys / tool names it is given (it sees keys, never values — the
truth diff stays L2's job). Falls back to the lexicon on any failure, so
a model outage degrades recall, never crashes a call.
"""

from __future__ import annotations

import re

from plivo_mirror_v5.engine.reference import ReferenceStore

_MONEY_RE = re.compile(
    r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)|\b(\d+(?:\.\d+)?)\s*(?:dollars|bucks)\b",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*[ap]m)\s*(?:-|–|to|until)\s*(\d{1,2}(?::\d{2})?\s*[ap]m)\b",
    re.IGNORECASE,
)
# Don't split inside decimals ("$59.99") — only on punctuation that isn't
# sandwiched between digits.
_SENTENCE_SPLIT_RE = re.compile(r"(?<!\d)[.!?]+|[.!?]+(?!\d)")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Value-type vocabulary: these key tokens pick the claim type + value regex,
# and are never used as sentence triggers (nobody says "price_per_month").
_TYPE_VOCAB = {
    "price": {"price", "cost", "fee", "rate", "total", "amount"},
    "hours": {"hours", "schedule"},
    "policy": {"days", "weeks", "months", "percent", "window", "policy", "limit", "cap"},
}
# Unit tokens that bind the policy value regex ("60 days", "10 percent").
_POLICY_UNITS = {"days": r"days?", "weeks": r"weeks?", "months": r"months?",
                 "percent": r"(?:percent|%)"}
_GENERIC_TOKENS = frozenset({"per", "month", "year", "week", "day", "the", "of", "a"})


def _stem(token: str) -> str:
    """Crude plural fold so 'weekends' triggers 'weekend'."""
    return token[:-1] if token.endswith("s") and len(token) > 3 else token


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in _WORD_RE.findall(text.lower())}


class _Pattern:
    def __init__(self, key: str, key_tokens: set[str], truth_value=None) -> None:
        self.key = key
        unit = next((u for u in _POLICY_UNITS if _stem(u) in key_tokens), None)
        # A "percent" key ALWAYS binds the % regex — `cancellation_fee_percent`
        # must capture the "20%" in a sentence, never the "$249.60" beside it
        # (the price vocab's "fee" would otherwise win and bind money).
        # Other units (days/months) keep vocab priority: price_per_month is a
        # price key, not a months-policy key.
        if unit == "percent":
            self.claim_type = "policy"
        else:
            self.claim_type = "fact"
            for claim_type in ("price", "hours", "policy"):  # priority order
                if key_tokens & {_stem(t) for t in _TYPE_VOCAB[claim_type]}:
                    self.claim_type = claim_type
                    break
        type_tokens = {_stem(t) for vocab in _TYPE_VOCAB.values() for t in vocab}
        self.triggers = key_tokens - type_tokens - {_stem(t) for t in _GENERIC_TOKENS}
        self.value_re = self._build_value_re(unit)
        # Comparability gate: a pattern only exists when the stored truth is
        # the same TYPE the regex captures (a number / an hours range).
        # Prose-valued keys ("20% of the fare, fixed and non-waivable...")
        # are judge grounding, NOT L2 diff targets — extracting against them
        # guarantees a false alarm, which is the budget we protect.
        if not self._truth_comparable(truth_value):
            self.value_re = None

    def _truth_comparable(self, truth_value) -> bool:
        if truth_value is None:  # unknown (tests / direct use): keep old behavior
            return True
        if self.claim_type == "hours":
            return _HOURS_RE.search(str(truth_value)) is not None
        from plivo_mirror_v5.engine.layers.l2_deterministic import _as_number
        return _as_number(truth_value) is not None

    def _build_value_re(self, unit: str | None) -> re.Pattern | None:
        if self.claim_type == "price":
            return _MONEY_RE
        if self.claim_type == "hours":
            return _HOURS_RE
        if self.claim_type == "policy":
            if unit:
                # (?!\w) instead of \b: "%" is a non-word char, so "5%"
                # never satisfies a trailing \b — "60 days" still does.
                return re.compile(
                    rf"\b(\d+(?:\.\d+)?)\s*{_POLICY_UNITS[unit]}(?!\w)",
                    re.IGNORECASE)
            return re.compile(r"\b(\d+(?:\.\d+)?)\b")
        return None  # bare "fact" keys: no typed value → never extracted

    def match(self, sentence: str, sentence_tokens: set[str]) -> str | None:
        if self.value_re is None or not self.triggers:
            return None
        if not self.triggers <= sentence_tokens:
            return None
        m = self.value_re.search(sentence)
        if m is None:
            return None
        if self.claim_type == "hours":
            start, end = (p.replace(" ", "").lower() for p in m.groups())
            return f"{start}-{end}"
        value = next(g for g in m.groups() if g is not None)
        return value.replace(",", "")


class LexiconClaimExtractor:
    """``ClaimExtractor`` for the observer: keeps claims already attached
    to the item (host/tool-provided; they win on ref collisions) and adds
    lexicon-derived ones from the transcript."""

    def __init__(
        self,
        reference: ReferenceStore,
        *,
        action_verbs: dict[str, list[str]] | None = None,
        fact_claims: bool = True,
    ) -> None:
        """``fact_claims=False`` disables lexicon-derived REFERENCE-value
        claims, keeping only action claims (speech-vs-action) and claims
        attached by the host. The LIVE pipeline runs with False: across
        real test calls, lexicon attribution of numbers to reference keys
        produced repeated false positives ('the 20% fee' attributed to
        refund_percent[80]) and zero catches the grounded judge missed —
        language attribution is the judge's job; values from STATE/tools
        stay deterministic."""
        self._reference = reference
        self._patterns = [] if not fact_claims else [
            _Pattern(key, _tokens(key.replace(".", " ").replace("_", " ")),
                     truth_value=reference.get(key))
            for key in reference.keys()
        ]
        self._action_patterns: list[tuple[str, re.Pattern]] = [
            (tool, re.compile(rf"\b(?:{'|'.join(map(re.escape, verbs))})\b",
                              re.IGNORECASE))
            for tool, verbs in (action_verbs or {}).items()
        ]

    def extract(self, item) -> list[dict]:
        attached = list(getattr(item, "claims", []) or [])
        if getattr(item, "role", "agent") != "agent":
            return attached  # user-side extraction (corrections) is TODO
        seen_refs = {c.get("ref") for c in attached if c.get("ref")}
        extracted = [c for c in self.extract_from_text(getattr(item, "text", "") or "")
                     if c["ref"] not in seen_refs]
        return attached + extracted

    def extract_from_text(self, text: str) -> list[dict]:
        claims: list[dict] = []
        for sentence in filter(None, map(str.strip, _SENTENCE_SPLIT_RE.split(text))):
            sentence_tokens = _tokens(sentence)
            sentence_claims: list[dict] = []
            for pattern in self._patterns:
                value = pattern.match(sentence, sentence_tokens)
                if value is None:
                    continue
                sentence_claims.append({
                    "claim_id": f"x{len(claims) + len(sentence_claims) + 1}",
                    "claim_type": pattern.claim_type,
                    "spoken_value": value,
                    "ref": f"reference.{pattern.key}",
                    "text": sentence,
                })
            claims += self._drop_ambiguous(sentence_claims)
            for tool, verb_re in self._action_patterns:
                m = verb_re.search(sentence)
                if m is not None:
                    claims.append({
                        "claim_id": f"x{len(claims) + 1}",
                        "claim_type": "action",
                        "spoken_value": m.group(),
                        "ref": f"tool.{tool}",
                        "text": sentence,
                    })
        return [c for c in claims if _claim_is_well_formed(c)]

    def _drop_ambiguous(self, sentence_claims: list[dict]) -> list[dict]:
        """Same-sentence cross-key disambiguation. One sentence, two sibling
        keys, one captured value ("the standard cancellation refund is 80%
        of the fare" triggers BOTH refund_percent[80] and fee_percent[20]):
        a claim that MISMATCHES its key while its value exactly matches a
        sibling key's truth belongs to the sibling — keeping it would flag a
        correct statement. Live false-positive, fixed deterministically."""
        if len(sentence_claims) < 2:
            return sentence_claims
        from plivo_mirror_v5.engine.layers.l2_deterministic import values_match
        truths = {}
        for c in sentence_claims:
            value, found = self._reference.lookup(c["ref"].removeprefix("reference."))
            truths[c["claim_id"]] = (value, found)
        kept = []
        for c in sentence_claims:
            truth, found = truths[c["claim_id"]]
            if found and not values_match(c["spoken_value"], truth):
                stolen = any(
                    o is not c and truths[o["claim_id"]][1]
                    and values_match(c["spoken_value"], truths[o["claim_id"]][0])
                    for o in sentence_claims
                )
                if stolen:
                    continue  # the value belongs to the sibling key
            kept.append(c)
        return kept


# A "done-action" claim is only real when the agent asserts completion —
# offers, intents, futures, questions and refusals are NOT action claims.
_NON_ASSERTIVE_RE = re.compile(
    r"\b(?:can|could|will|would|shall|let me|going to|want me to|happy to|"
    r"about to|i'?ll|we'?ll|not able to|can'?t|cannot|unable to)\b",
    re.IGNORECASE,
)


def _claim_is_well_formed(claim: dict) -> bool:
    """Deterministic discipline filter applied AFTER any extractor (LLM or
    lexicon): kill the two claim shapes that cause false alarms.

    - action claims drawn from non-assertive sentences ("I can transfer
      you", "shall I place it?") are offers/intents, not completions;
    - claims mapped onto numeric reference keys (price/fee/count…) must
      actually assert a number — a bare product MENTION is not a value
      claim ("we have Margherita" vs price_margherita)."""
    text = str(claim.get("text") or "")
    if claim.get("claim_type") == "action":
        if _NON_ASSERTIVE_RE.search(text) or text.rstrip().endswith("?"):
            return False
    ref = claim.get("ref") or ""
    if ref.startswith("reference."):
        key_tokens = _tokens(ref.replace(".", " ").replace("_", " "))
        numeric_key = bool(key_tokens & {_stem(t) for t in _TYPE_VOCAB["price"]}
                           | (key_tokens & {"per", "count", "quantity", "number"}))
        spoken = str(claim.get("spoken_value") or "")
        if (claim.get("claim_type") in ("price", "policy") or numeric_key) \
                and not any(ch.isdigit() for ch in spoken):
            return False
    return True


_EXTRACT_SYSTEM = """You extract verifiable factual claims from one turn of a
voice agent's speech. Output STRICT JSON: {"claims": [...]}, each claim:
{"claim_type": "price"|"policy"|"hours"|"action"|"fact",
 "spoken_value": "<the exact value the agent asserted, normalized: bare
                  number for money/counts, '9am-5pm' style for hours,
                  past-tense verb for actions>",
 "ref": "<reference.KEY for a listed key | tool.NAME for a listed tool |
          null when the claim has no listed referent>",
 "text": "<the sentence containing the claim>"}

Rules:
- Only claims the AGENT asserts as fact. Questions, offers, hedges
  ("let me check", "I believe"), and the caller's words are NOT claims.
- "ref" MUST be one of the provided keys/tools, or null. Never invent keys.
- An "action" claim is the agent stating an action HAS BEEN done
  ("I've placed it", "that's cancelled") — map to the matching tool.
- Prose with no listed referent (perks, capabilities, availability,
  history) → claim_type "fact", ref null.
- No claims → {"claims": []}."""


class LLMClaimExtractor:
    """NLU claim extraction over an OpenAI-compatible endpoint, constrained
    to the reference-key / tool vocabulary. Sees keys, never truth values.
    Any failure falls back to the lexicon extractor (degrade, don't die)."""

    def __init__(
        self,
        reference: ReferenceStore,
        *,
        client=None,
        tools: list[str] | None = None,
        action_verbs: dict[str, list[str]] | None = None,
    ) -> None:
        if client is None:
            from plivo_mirror_v5.llm_client import ChatClient  # noqa: PLC0415
            client = ChatClient()
        self.client = client
        self.reference_keys = reference.keys()
        self.tools = sorted(set(tools or []) | set((action_verbs or {}).keys()))
        self.fallback = LexiconClaimExtractor(reference, action_verbs=action_verbs)

    def extract(self, item) -> list[dict]:
        attached = list(getattr(item, "claims", []) or [])
        if getattr(item, "role", "agent") != "agent":
            return attached
        text = getattr(item, "text", "") or ""
        seen_refs = {c.get("ref") for c in attached if c.get("ref")}
        extracted = [c for c in self.extract_from_text(text)
                     if not (c.get("ref") and c["ref"] in seen_refs)]
        return attached + extracted

    def extract_from_text(self, text: str) -> list[dict]:
        if not text.strip():
            return []
        user = (
            "Reference keys (use as ref=\"reference.<key>\"):\n"
            + "\n".join(f"- {k}" for k in self.reference_keys)
            + "\n\nTools (use as ref=\"tool.<name>\"):\n"
            + ("\n".join(f"- {t}" for t in self.tools) or "- (none)")
            + f"\n\nAgent turn:\n{text}"
        )
        try:
            payload = self.client.complete_json(_EXTRACT_SYSTEM, user)
            claims = []
            for i, c in enumerate(payload.get("claims", []), start=1):
                ref = c.get("ref") or None
                if ref is not None and not self._ref_valid(ref):
                    ref = None  # hallucinated referent → prose claim, L3's job
                claims.append({
                    "claim_id": f"n{i}",
                    "claim_type": c.get("claim_type", "fact"),
                    "spoken_value": c.get("spoken_value"),
                    "ref": ref,
                    "text": c.get("text") or text,
                })
            return [c for c in claims if _claim_is_well_formed(c)]
        except Exception:  # noqa: BLE001 — degrade to lexicon, never crash a call
            return self.fallback.extract_from_text(text)

    def _ref_valid(self, ref: str) -> bool:
        if ref.startswith("reference."):
            return ref[len("reference."):] in self.reference_keys
        if ref.startswith("tool."):
            return ref[len("tool."):] in self.tools
        return ref.startswith("session.")
