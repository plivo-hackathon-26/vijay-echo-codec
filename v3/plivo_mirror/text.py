"""Text-quality helpers used by the supervisor + judge layers.

These are public — customers writing custom judges or adapters can
reuse them to guarantee the spoken corrections sound like the agent,
not the customer.

Why these exist:

  • LLM judges (even strong ones like gpt-5.4-mini) sometimes write
    ``suggested_correction`` in CUSTOMER voice ("I'd like X please")
    instead of AGENT voice ("Got it — one X for you."). Speaking
    that verbatim out the agent's mouth sounds bizarre.

  • They also sometimes write the ``customer_intent`` field as a
    META-DESCRIPTION ("The customer said they want X") instead of
    a concrete order summary ("one cheese sandwich"). Templating
    that into a confirmation line ("Got it — The customer said they
    want X. Anything else?") sounds robotic.

Both patterns were observed in production with Azure OpenAI's
gpt-5.4-mini during the LiveKit v0.2.0 integration. v0.3.0 ships
these filters as first-class library helpers so consumers don't
have to rebuild them.
"""

from __future__ import annotations

import re


_CUSTOMER_VOICE_RE = re.compile(
    r"""
    ^\s*(?:
        i\s*(?:'d\s*like|would\s*like|want|need|wanted|'ll\s*have|am\s+looking)\b
      | can\s+i\s+
      | could\s+i\s+
      | may\s+i\s+
      | give\s+me\b
      | (?:i'm|im)\s+(?:looking\s+for|hoping)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


_META_VERBS = r"(?:said|wants|wanted|asked|told|is|requested|mentioned|stated)"

_META_DESCRIPTION_RE = re.compile(
    rf"""
    ^\s*(?:
        the\s+customer\s+{_META_VERBS}
      | they\s+{_META_VERBS}
      | customer\s+{_META_VERBS}
      | (?:the\s+)?caller\s+{_META_VERBS}
      | (?:the\s+)?user\s+{_META_VERBS}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_customer_voice(text: str) -> bool:
    """Return True when ``text`` reads as a customer ordering, not an
    agent replying — e.g. *"I'd like X please"*, *"Can I get Y"*.

    Use this to validate ``Verdict.suggested_correction`` before
    speaking it out the agent's mouth. When it returns True, you
    should fall back to a synthesized agent-voice correction instead.

    Empty / whitespace-only text returns False (nothing to check).
    """
    if not text:
        return False
    return bool(_CUSTOMER_VOICE_RE.match(text.strip()))


def is_meta_description(text: str) -> bool:
    """Return True when ``text`` reads as a third-person description of
    what the customer said — e.g. *"The customer said they want X"*.

    Use this to validate ``Verdict.evidence.customer_intent`` before
    templating it into an agent confirmation line. When True, the
    intent field is unusable as a confirmation ("Got it — *The customer
    said they want X*. Anything else?") and you should fall back to
    something generic or the judge's supplied correction.

    Empty / whitespace-only text returns False.
    """
    if not text:
        return False
    return bool(_META_DESCRIPTION_RE.match(text.strip()))


__all__ = ["is_customer_voice", "is_meta_description"]
