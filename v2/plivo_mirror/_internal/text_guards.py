"""Guards that defend against the LLM emitting instruction-format
text where customer-facing speech is expected.

We've observed real production traces where Azure GPT-5.4-mini returned
``suggested_correction`` values like:

    "Please confirm the order as chicken sandwich only before placing it."
    "Before placing anything, read the order back to the customer:
     'Just to confirm, you want the chicken sandwich only, correct?'"
    "Tell the customer: 'I can help with that, but I'll need to transfer
     you to a human supervisor.'"

These are *instructions to the agent* — not the words the agent should
say. Speaking them verbatim is bad UX: callers hear the LLM read its
own scripting out loud.

The fix has two layers:

  1. Tighten the prompts (scorer/tool_gate) to require first-person
     customer-facing speech in the suggested_correction field.

  2. Validate the LLM's output here and DROP suggested_correction if
     it matches an instruction-format pattern — falling back to the
     CorrectionGenerator's prompt which is more constrained.
"""

from __future__ import annotations

import re


# Matches the most common ways an LLM slips into instruction format
# instead of speech. Tested against real production samples.
_INSTRUCTION_PATTERNS = re.compile(
    r"""
    # Starts with a directive verb
    ^(?:please|first|then|now|before|after|next|step|step\s*\d)\s*[,:\s]
    |
    # "tell the customer:" or similar quoting-the-line patterns
    \btell\s+(?:the\s+)?(?:customer|caller|user)\s*[,:]
    |
    \bsay\s+(?:to\s+)?(?:the\s+)?(?:customer|caller|user)\s*[,:]
    |
    \bread\s+(?:the\s+)?(?:order|back|.{0,20}\s+back)\s+(?:to\s+(?:the\s+)?(?:customer|caller)|aloud)
    |
    ^read\s+(?:the\s+)?order\s+back
    |
    # "the agent should" / "you should ..." (third-person scripting)
    \b(?:the\s+)?agent\s+(?:should|must|needs?\s+to)\s+\w+
    |
    \byou\s+should\s+(?:say|tell|confirm|ask|transfer|reply|respond)
    |
    # "if X is involved, transfer..." (conditional scripting)
    \bif\s+\w+.{0,40}\bis\s+involved\b
    |
    # "before placing/calling" — meta-process narration
    \bbefore\s+(?:placing|calling|confirming|charging|sending)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_instruction(text: str) -> bool:
    """Return True if ``text`` reads like instructions to the agent
    rather than something the agent would say to the customer.

    Returns False on empty input.
    """
    if not text:
        return False
    return bool(_INSTRUCTION_PATTERNS.search(text.strip()))


def sanitise_suggested_correction(text: str) -> str:
    """If ``text`` looks like an instruction, drop it (return empty
    string). The caller is expected to fall back to a clean
    customer-facing generator when this returns empty."""
    if not text:
        return ""
    if looks_like_instruction(text):
        return ""
    return text.strip()


__all__ = ["looks_like_instruction", "sanitise_suggested_correction"]
