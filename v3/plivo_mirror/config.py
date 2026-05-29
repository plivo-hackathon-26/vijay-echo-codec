"""MirrorConfig — the single configuration object a customer fills in.

Every scattered env-var / module-constant from the old codebase that
needs to be customer-tunable is a field here.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MirrorConfig(BaseModel):
    """The full supervisor configuration.

    The customer's onboarding is essentially: instantiate this object,
    pass it to ``Supervisor(config)``, and wrap their handler. Everything
    else is internal.

    Exactly one of ``policies`` or ``judging_prompt`` must be provided:
      - ``policies`` (easy path) — plain-English rules; the library
        compiles them into a judging prompt internally.
      - ``judging_prompt`` (power path) — a fully-formed prompt with
        ``{customer_text}`` / ``{primary_response}`` / ``{tool_calls_json}``
        / ``{history_summary}`` slots.

    If both are supplied, ``judging_prompt`` wins (and we log a warning).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    # ---- LLM client ------------------------------------------------------
    # An object implementing the LLMClient protocol from llm.base.
    llm: Any = Field(..., description="An LLMClient implementation.")

    # ---- Policy / judging prompt ----------------------------------------
    policies: list[str] | None = None
    judging_prompt: str | None = None

    # ---- Intervention thresholds & timing -------------------------------
    intervention_threshold: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        description="Verdict.score >= threshold triggers intervention.",
    )
    cooldown_s: float = Field(
        10.0, ge=0.0, description="Seconds to suppress further interventions after one fires."
    )
    semantic_review_timeout_s: float = Field(
        4.0, gt=0.0, description="Per-turn scorer LLM timeout."
    )

    # ---- Toggles for the differentiator features ------------------------
    tiered_scoring_enabled: bool = Field(
        True,
        description="Run the cheap heuristic pre-gate; only call the scorer LLM on flagged turns.",
    )
    tiered_force_score_on_tool_call: bool = Field(
        True,
        description="When tiered scoring is on, always score turns that contain a tool call.",
    )
    streaming_mode: bool = Field(
        False,
        description="Score the first-sentence boundary mid-stream instead of after the full response.",
    )
    tool_gate_enabled: bool = Field(
        True,
        description="Inspect tool_calls BEFORE execution; block irreversible ones that fail policy.",
    )
    irreversible_tools: list[str] = Field(
        default_factory=lambda: [
            "place_order",
            "book_flight",
            "book_flights",
            "charge_card",
            "process_payment",
            "cancel_subscription",
            "send_email",
            "send_sms",
            "transfer_funds",
        ],
        description="Tool names that always go through the tool-gate regardless of tiered scoring.",
    )

    # The list of files the post-call ReportGenerator is allowed to
    # propose as `proposed_file`. When set, the report prompt instructs
    # the LLM to pick ONLY from this list — eliminating the
    # "hallucinates prompts.py when the project only has agent.py" bug.
    fixable_files: list[str] = Field(
        default_factory=list,
        description="Files the ReportGenerator may propose as the fix target. Empty = any.",
    )

    # ---- Intervention text -----------------------------------------------
    buffer_text: str = Field(
        "Sorry, let me make sure I got that right — just a moment...",
        description="Pre-rendered buffer line played while the correction is generated.",
    )

    # ---- Multi-tenant seam (v2; v1 just passes it through) --------------
    tenant_id: str | None = None

    # ---- Optional secrets resolver --------------------------------------
    # Customer can pass a callable (e.g. Vault lookup); default is os.getenv.
    secrets: Callable[[str], str | None] | None = None

    # ---- Optional OpenTelemetry switch ----------------------------------
    telemetry_enabled: bool = False

    # ─────────────────────────── validators ──────────────────────────────

    @model_validator(mode="after")
    def _validate_policy_or_prompt(self) -> "MirrorConfig":
        if not self.policies and not self.judging_prompt:
            raise ValueError(
                "MirrorConfig requires either `policies` (list[str]) or "
                "`judging_prompt` (str). Provide one."
            )
        return self

    # ─────────────────────────── helpers ─────────────────────────────────

    def resolve_secret(self, name: str) -> str | None:
        """Resolve a secret by name. Defaults to environment lookup."""
        if self.secrets is not None:
            return self.secrets(name)
        return os.getenv(name)

    @classmethod
    def from_env(cls, **overrides: Any) -> "MirrorConfig":
        """Convenience: build a config from a handful of env vars,
        useful for the quickstart README.

        Reads:
          PLIVO_MIRROR_THRESHOLD        → intervention_threshold
          PLIVO_MIRROR_COOLDOWN_S       → cooldown_s
          PLIVO_MIRROR_TIMEOUT_S        → semantic_review_timeout_s
          PLIVO_MIRROR_STREAMING        → streaming_mode (bool)
          PLIVO_MIRROR_TIERED           → tiered_scoring_enabled (bool)
          PLIVO_MIRROR_TOOL_GATE        → tool_gate_enabled (bool)
        """
        def _b(name: str, default: bool) -> bool:
            v = os.getenv(name)
            if v is None:
                return default
            return v.lower() in ("1", "true", "yes", "on")

        def _f(name: str, default: float) -> float:
            v = os.getenv(name)
            if v is None:
                return default
            try:
                return float(v)
            except ValueError:
                return default

        data: dict[str, Any] = dict(
            intervention_threshold=_f("PLIVO_MIRROR_THRESHOLD", 0.7),
            cooldown_s=_f("PLIVO_MIRROR_COOLDOWN_S", 10.0),
            semantic_review_timeout_s=_f("PLIVO_MIRROR_TIMEOUT_S", 4.0),
            streaming_mode=_b("PLIVO_MIRROR_STREAMING", False),
            tiered_scoring_enabled=_b("PLIVO_MIRROR_TIERED", True),
            tool_gate_enabled=_b("PLIVO_MIRROR_TOOL_GATE", True),
        )
        data.update(overrides)
        return cls(**data)


__all__ = ["MirrorConfig"]
