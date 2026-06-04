"""L2 policy checks — the v4 defenses, ported as PARALLEL deterministic
checks that run alongside the claims diff inside Layer 2. All regex /
dict-lookup against the ``PolicyPack`` + the state snapshot: µs each, no
model, fully explainable evidence.

Coverage added (the four ❌/⚠️ of the six target failures):

- wrong-action-vs-intent  → ``check_tool_args`` (args vs validated state)
- prompt injection        → ``check_tool_authorization`` (authz separation)
- unauthorized commitments → ``check_commitments``
- compliance/disclosure   → ``check_disclosures`` (turn + call scope)
- persona drift           → ``check_persona``
"""

from __future__ import annotations

import re

from plivo_mirror_v5.engine.layers.base import LayerContext
from plivo_mirror_v5.engine.layers.l2_deterministic import values_match
from plivo_mirror_v5.engine.policy import PolicyPack
from plivo_mirror_v5.engine.session_state import SessionState
from plivo_mirror_v5.engine.verdict import Evidence, TurnInput, Verdict, new_verdict_id

_AGENT_TURNS_KEY = "mirror.agent_turn_count"
_DISCLOSURE_SEEN_PREFIX = "mirror.disclosure_seen."


def _verdict(detector: str, fired: bool, severity: str, claim_type: str,
             spoken, truth, source: str, **extra) -> Verdict:
    return Verdict(
        verdict_id=new_verdict_id(), detector=detector, fired=fired,
        severity=severity if fired else "info", latency_ms=0.0,
        evidence=Evidence(
            claim_type=claim_type,
            spoken_value=None if spoken is None else str(spoken),
            truth_value=None if truth is None else str(truth),
            source=source, extra=extra,
        ),
    )


def check_tool_args(turn: TurnInput, ctx: LayerContext,
                    pack: PolicyPack, detector: str) -> list[Verdict]:
    """Wrong-action-vs-intent: a tool's ARGUMENTS must match the validated
    session facts they are bound to. The agent cancelling the wrong account
    with a confident readback dies here, not at the customer's bank."""
    verdicts = []
    for tc in turn.tool_calls:
        bindings = pack.arg_bindings.get(tc.get("name", ""), {})
        args = tc.get("args") or {}
        for arg_name, ref in bindings.items():
            key = ref.removeprefix("session.")
            if arg_name not in args or not ctx.snapshot.has(key):
                continue  # nothing validated to diff against
            truth = ctx.snapshot.get(key)
            ok = values_match(args[arg_name], truth)
            verdicts.append(_verdict(
                detector, not ok, "high", "action_args",
                args[arg_name], truth, ref,
                tool=tc.get("name"), arg=arg_name,
            ))
    return verdicts


def check_tool_authorization(turn: TurnInput, ctx: LayerContext,
                             pack: PolicyPack, detector: str) -> list[Verdict]:
    """Authorization SEPARATION — the prompt-injection defense. A guarded
    tool may only fire when the authorizing fact exists in state, and only
    host code can write state. Whatever the caller (or an injected
    instruction) talks the model into, the model cannot authorize itself."""
    verdicts = []
    for tc in turn.tool_calls:
        rule = pack.tool_authorization.get(tc.get("name", ""))
        if rule is None:
            continue
        if isinstance(rule, dict):
            # Conditional: authorization is demanded only when the named
            # argument is truthy (waive_fee=true), so the tool's NORMAL use
            # never flags. Phrasing-proof: this reads the executed call's
            # args, not the agent's words.
            ref = rule["requires"]
            gate_arg = rule.get("when_arg_truthy")
            if gate_arg and not (tc.get("args") or {}).get(gate_arg):
                continue  # condition not triggered → nothing to authorize
            spoken = f"{tc.get('name')} fired with {gate_arg}=true"
        else:
            ref = rule
            spoken = f"{tc.get('name')} fired"
        key = ref.removeprefix("session.")
        authorized = bool(ctx.snapshot.get(key))
        verdicts.append(_verdict(
            detector, not authorized, "high", "authorization",
            spoken,
            f"requires {ref} (present)" if authorized else f"requires {ref} (ABSENT)",
            ref, tool=tc.get("name"),
        ))
    return verdicts


# A commitment-word match preceded by negation/limitation language is a
# RETRACTION or a statement of requirements, not a promise ("I cannot waive
# the fee", "unless the system has fee-waiver authorization"). Live finding:
# without this, the agent's own correction re-flags and corrections cascade.
_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:not|n't|never|cannot|can'?t|won'?t|unable to|no longer|unless|"
    r"without|requires?|would need|isn'?t able|not able|only(?:\s+\w+){0,2}\s+"
    r"standard)\b[^.?!]{0,40}$",
    re.IGNORECASE,
)
# ... or follows it: "a full refund REQUIRES verified authorization",
# "the fee cannot be waived". Limitation after the phrase, same exemption.
_NEGATION_AFTER_RE = re.compile(
    r"^[^.?!]{0,50}?\b(?:requires?|would need|needs?|is not|isn'?t|cannot|"
    r"can'?t|won'?t|not (?:allowed|permitted|possible|available))\b",
    re.IGNORECASE,
)


def _negated_context(text: str, match_start: int, match_end: int) -> bool:
    before = text[max(0, match_start - 60):match_start]
    after = text[match_end:match_end + 60]
    return (_NEGATION_BEFORE_RE.search(before) is not None
            or _NEGATION_AFTER_RE.search(after) is not None)


def check_commitments(turn: TurnInput, ctx: LayerContext,
                      pack: PolicyPack, detector: str) -> list[Verdict]:
    """Unauthorized verbal commitments: commitment language must be backed
    by an authorizing state fact ('refund'/'waive'/'guarantee' are cheap to
    say and expensive to honor). Negated/limitation contexts are exempt —
    retracting a promise must never re-flag as making one."""
    verdicts = []
    for rule in pack.commitments:
        m = rule.compiled().search(turn.transcript)
        if m is None:
            continue
        if _negated_context(turn.transcript, m.start(), m.end()):
            continue  # "I cannot waive…" / "unless authorized…" — not a promise
        authorized = bool(ctx.snapshot.get(rule.allowed_if.removeprefix("session."))) \
            if rule.allowed_if else False
        verdicts.append(_verdict(
            detector, not authorized, rule.severity, "commitment",
            m.group(0),
            (f"authorized by {rule.allowed_if}" if authorized
             else f"no authorization in state ({rule.allowed_if or 'none defined'})"),
            f"policy.{rule.id}",
        ))
    return verdicts


def check_disclosures(turn: TurnInput, state: SessionState, ctx: LayerContext,
                      pack: PolicyPack, detector: str) -> list[Verdict]:
    """Compliance/disclosure gaps (v4's REQUIRE):
    - turn scope: a turn matching ``when`` must also match ``must_include``;
    - call scope: ``must_include`` must have been said by agent-turn N
      (tracked in reserved ``mirror.*`` state keys; fires once)."""
    verdicts = []
    agent_turns = int(state.get_fact(_AGENT_TURNS_KEY, 0)) + 1
    state.set_fact(_AGENT_TURNS_KEY, agent_turns, source="mirror")

    for rule in pack.disclosures:
        required = re.compile(rule.must_include, re.IGNORECASE)
        said_now = bool(required.search(turn.transcript))
        seen_key = f"{_DISCLOSURE_SEEN_PREFIX}{rule.id}"
        if said_now and not state.get_fact(seen_key):
            state.set_fact(seen_key, True, source="mirror",
                           turn_index=turn.turn_index)

        if rule.when is not None:  # turn scope
            if re.search(rule.when, turn.transcript, re.IGNORECASE):
                verdicts.append(_verdict(
                    detector, not said_now, rule.severity, "disclosure",
                    turn.transcript[:120],
                    f"must include /{rule.must_include}/",
                    f"policy.{rule.id}",
                ))
        elif rule.by_agent_turn is not None:  # call scope, fire exactly once
            fired_key = f"{seen_key}.flagged"
            ever_said = said_now or bool(state.get_fact(seen_key))
            if (agent_turns >= rule.by_agent_turn and not ever_said
                    and not state.get_fact(fired_key)):
                state.set_fact(fired_key, True, source="mirror")
                verdicts.append(_verdict(
                    detector, True, rule.severity, "disclosure",
                    f"not said in the first {rule.by_agent_turn} agent turns",
                    f"must include /{rule.must_include}/",
                    f"policy.{rule.id}",
                ))
    return verdicts


def check_persona(turn: TurnInput, ctx: LayerContext,
                  pack: PolicyPack, detector: str) -> list[Verdict]:
    """Persona drift: things this agent must never say (system-prompt /
    instruction leakage ships as the default list)."""
    verdicts = []
    for pattern in pack.persona_forbidden:
        m = re.search(pattern, turn.transcript, re.IGNORECASE)
        if m is not None:
            verdicts.append(_verdict(
                detector, True, pack.persona_severity, "persona",
                m.group(0), f"forbidden /{pattern}/", "policy.persona",
            ))
    return verdicts


def run_policy_checks(turn: TurnInput, state: SessionState, ctx: LayerContext,
                      detector: str) -> list[Verdict]:
    """All five checks, in sequence (each is µs; 'parallel' here means
    independent of the claims diff, not threaded)."""
    pack = ctx.config.policy
    if pack is None or turn.role != "agent":
        return []
    verdicts = [
        *check_tool_args(turn, ctx, pack, detector),
        *check_tool_authorization(turn, ctx, pack, detector),
        *check_commitments(turn, ctx, pack, detector),
        *check_disclosures(turn, state, ctx, pack, detector),
        *check_persona(turn, ctx, pack, detector),
    ]
    if ctx.snapshot.untrusted_input:
        for v in verdicts:
            if v.fired:
                v.severity = "info"
                v.evidence.extra["untrusted_input"] = True
    return verdicts
