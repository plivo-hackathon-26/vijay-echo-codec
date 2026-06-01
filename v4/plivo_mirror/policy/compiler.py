"""Policy compiler — plain-English policy → runnable ``Policy`` check.

Two compilation paths, both keeping business logic in CODE (never in a
prompt):

  * **Directive policies** carry an explicit, domain-agnostic prefix that
    compiles to a deterministic check:
        ``"FORBID: <phrase>"``  → block when ``<phrase>`` appears in the reply.
        ``"REQUIRE: <phrase>"`` → block when ``<phrase>`` is absent from a
                                   non-empty reply (a missing disclosure).
  * **Plain-English policies** (no directive) compile to a *verifier-only*
    ``Policy`` (``check is None``): the text becomes evidence for the
    grounded verifier in Phase 2.

The deterministic checks here are the primitives the speech guard's
deterministic layer (Phase 2) reuses; a hard hit routes straight to
``block`` per the router design.
"""

from __future__ import annotations

import re
from typing import Callable

from plivo_mirror.contracts import Policy, TurnContext, Verdict

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DIRECTIVE_RE = re.compile(r"(?i)^(FORBID|REQUIRE):\s*(.+)$", re.DOTALL)


def _slug(text: str, n: int = 5) -> str:
    words = _SLUG_RE.sub(" ", text.lower()).split()
    return "_".join(words[:n]) or "policy"


def _forbid_check(
    phrase: str, policy_id: str
) -> Callable[[TurnContext], "Verdict | None"]:
    needle = phrase.lower().strip()

    def check(ctx: TurnContext) -> "Verdict | None":
        if needle and needle in (ctx.planned_reply or "").lower():
            return Verdict.block(
                reason=f"forbidden phrase present: {phrase!r}",
                policy_id=policy_id,
                span=phrase,
            )
        return None

    return check


def _require_check(
    phrase: str, policy_id: str
) -> Callable[[TurnContext], "Verdict | None"]:
    needle = phrase.lower().strip()

    def check(ctx: TurnContext) -> "Verdict | None":
        reply = (ctx.planned_reply or "").strip()
        if not reply:  # nothing being said this turn — disclosure not due
            return None
        if needle and needle not in reply.lower():
            return Verdict.block(
                reason=f"required disclosure missing: {phrase!r}",
                policy_id=policy_id,
                span=phrase,
            )
        return None

    return check


def compile_policy(text: str, policy_id: str) -> Policy:
    """Compile one policy string into a ``Policy``."""
    stripped = (text or "").strip()
    m = _DIRECTIVE_RE.match(stripped)
    check: Callable[[TurnContext], "Verdict | None"] | None = None
    if m:
        kind, phrase = m.group(1).upper(), m.group(2).strip()
        check = (
            _forbid_check(phrase, policy_id)
            if kind == "FORBID"
            else _require_check(phrase, policy_id)
        )
    return Policy(id=policy_id, text=stripped, check=check)


def compile_policies(texts: list[str]) -> list[Policy]:
    """Compile a list of policy strings, assigning stable, unique,
    slugified ids. Collisions get a numeric suffix."""
    out: list[Policy] = []
    seen: dict[str, int] = {}
    for t in texts:
        body = _DIRECTIVE_RE.sub(r"\2", (t or "").strip())
        base = _slug(body)
        seen[base] = seen.get(base, 0) + 1
        pid = base if seen[base] == 1 else f"{base}_{seen[base]}"
        out.append(compile_policy(t, pid))
    return out


__all__ = ["compile_policy", "compile_policies"]
