"""PolicyPack — per-agent policy configuration for the deterministic L2
policy checks. This is where v4's remaining defenses land in v5, ALL as
code/config (never prompts), all µs-cheap, all running in parallel with
the claims diff inside L2:

- ``arg_bindings``       wrong-action-vs-intent: tool ARGUMENTS diffed
                         against validated session state (the audit-side
                         of v4's zero-argument principle).
- ``tool_authorization`` prompt-injection defense: authorization
                         SEPARATION — a tool may only fire when state
                         carries the authorizing fact, which only host
                         code can write. The model never authorizes.
- ``commitments``        unauthorized verbal commitments: commitment
                         language (refund/waive/guarantee…) must be backed
                         by an authorizing state fact.
- ``disclosures``        compliance gaps: v4's REQUIRE checks — when the
                         agent talks about X it must also say Y (turn
                         scope), or must have said Y by agent-turn N
                         (call scope).
- ``persona_forbidden``  persona drift: things this agent must never say
                         (system-prompt leaks ship as defaults).

Loadable from a plain dict / JSON file so it lives next to the reference
data in the agent's config repo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Persona-drift defaults: prompt/instruction leakage and AI self-reveal.
DEFAULT_PERSONA_FORBIDDEN = [
    r"\bsystem prompt\b",
    r"\bmy (?:instructions|prompt|guidelines) (?:say|tell|are)\b",
    r"\bas an ai\b",
    r"\b(?:i am|i'm) (?:an? )?(?:ai|language model|llm)\b",
    r"\bignore (?:the |my )?previous instructions\b",
]


@dataclass
class CommitmentRule:
    id: str
    pattern: str                      # regex over the agent's words
    allowed_if: str | None = None    # session key that authorizes it
    severity: str = "high"

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass
class DisclosureRule:
    id: str
    must_include: str                 # regex that must appear
    when: str | None = None          # turn scope: trigger regex on the turn
    by_agent_turn: int | None = None  # call scope: must appear by turn N
    severity: str = "med"


@dataclass
class PolicyPack:
    # {tool_name: {arg_name: "session.<key>"}}
    arg_bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    # {tool_name: "session.<key>"} — key must be truthy for the tool to fire
    tool_authorization: dict[str, str] = field(default_factory=dict)
    commitments: list[CommitmentRule] = field(default_factory=list)
    disclosures: list[DisclosureRule] = field(default_factory=list)
    persona_forbidden: list[str] = field(
        default_factory=lambda: list(DEFAULT_PERSONA_FORBIDDEN))
    persona_severity: str = "med"

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyPack":
        pack = cls(
            arg_bindings=data.get("arg_bindings", {}),
            tool_authorization=data.get("tool_authorization", {}),
            commitments=[CommitmentRule(**c) for c in data.get("commitments", [])],
            disclosures=[DisclosureRule(**d) for d in data.get("disclosures", [])],
            persona_severity=data.get("persona_severity", "med"),
        )
        if "persona_forbidden" in data:
            pack.persona_forbidden = list(data["persona_forbidden"])
            if data.get("include_default_persona", True):
                pack.persona_forbidden += [p for p in DEFAULT_PERSONA_FORBIDDEN
                                           if p not in pack.persona_forbidden]
        return pack

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyPack":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
