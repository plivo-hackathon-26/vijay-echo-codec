"""Default grounded verifier — LLM-as-judge over an OpenAI-compatible
chat endpoint with a grounded-entailment prompt.

The prompt does entailment / NLU ONLY; all business logic lives in the
FACTS (validated state) and POLICIES (compiled checks) passed in as
evidence. No fine-tuned model — this satisfies the ``Verifier`` Protocol
and is meant to be swapped out later.

Honors the Azure ``gpt-5-mini`` quirks documented in CLAUDE.md:
``max_completion_tokens`` (never ``max_tokens``), no ``tool_choice``,
``response_format={"type":"json_object"}``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from plivo_mirror.verifier.base import GroundingEvidence, VerifierResult

log = logging.getLogger("plivo_mirror.verifier")

_SYSTEM = (
    "You are a grounding verifier for a voice agent. Decide whether a CLAIM the "
    "agent is about to say to a caller is SUPPORTED by the provided FACTS, "
    "POLICIES, and the CUSTOMER REQUEST. Judge only grounding, policy "
    "compliance, and faithfulness to the request — never tone or style.\n"
    "A claim is UNSUPPORTED if it: states a number, price, date, or fact that is "
    "not present in FACTS; makes a commitment (refund, discount, eligibility, "
    "guarantee, cancellation, credit) that FACTS/POLICIES do not authorize; "
    "contradicts any POLICY; or contradicts, ignores, or drops a constraint in "
    "the CUSTOMER REQUEST — e.g. ignores a negation ('no onions' but the reply "
    "adds onions), drops or globalizes a stated modifier ('extra cheese on the "
    "veggie half' but the reply applies it to everything), or acts on the wrong "
    "branch of a stated condition. When CUSTOMER REQUEST is '(none)', judge on "
    "FACTS and POLICIES only.\n"
    'Respond ONLY as JSON: {"supported": true|false, "policy_id": string|null, '
    '"reason": "<short>"}. Set policy_id to the id of the POLICY most relevant to '
    "your decision, or null."
)


def _build_user_prompt(claim: str, ev: GroundingEvidence) -> str:
    facts = "\n".join(f"  - {k}: {v}" for k, v in ev.facts.items()) or "  (none)"
    pols = (
        "\n".join(f"  - [{p.get('id')}] {p.get('text')}" for p in ev.policies)
        or "  (none)"
    )
    retrieved = "\n".join(f"  - {r}" for r in ev.retrieved_facts) or "  (none)"
    flagged = ", ".join(ev.flagged_spans) or "(whole reply)"
    customer = (ev.customer_text or "").strip() or "(none)"
    return (
        f"CLAIM (the agent is about to say this):\n  {claim}\n\n"
        f"FLAGGED SPANS: {flagged}\n\n"
        f"CUSTOMER REQUEST (what the caller just asked for):\n  {customer}\n\n"
        f"FACTS (validated session state — the only ground truth):\n{facts}\n\n"
        f"POLICIES:\n{pols}\n\n"
        f"RETRIEVED FACTS:\n{retrieved}\n\n"
        "Is the CLAIM supported?"
    )


class LLMJudgeVerifier:
    """``Verifier`` backed by an OpenAI-compatible async chat client.

    ``client`` must expose ``await client.chat.completions.create(...)``
    returning an object with ``.choices[0].message.content`` (a JSON
    string). Pass any OpenAI-compatible ``AsyncOpenAI`` / ``AsyncAzureOpenAI``
    instance.
    """

    def __init__(
        self, client: Any, model: str, *, max_completion_tokens: int = 300
    ) -> None:
        self._client = client
        self._model = model
        self._max_completion_tokens = max_completion_tokens

    @property
    def model(self) -> str:
        """The model this verifier runs on. By default (single-LLM) this is
        the same model as the voice agent."""
        return self._model

    async def verify(self, claim: str, evidence: GroundingEvidence) -> VerifierResult:
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _build_user_prompt(claim, evidence)},
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=self._max_completion_tokens,
            )
            content = resp.choices[0].message.content or "{}"
            data = json.loads(content)
        except Exception:
            # Fail open: a verifier error must not block a live call. We
            # err toward NOT intervening to keep the false-intervention
            # rate honest.
            log.warning("grounded verifier failed; treating claim as supported", exc_info=True)
            return VerifierResult(supported=True, reason="verifier_error")

        return VerifierResult(
            supported=bool(data.get("supported", True)),
            policy_id=(data.get("policy_id") or None),
            reason=str(data.get("reason", "")),
        )


__all__ = ["LLMJudgeVerifier"]
