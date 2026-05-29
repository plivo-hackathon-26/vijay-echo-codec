"""plivo-mirror replay CLI — run Mirror over a recorded transcript.

Usage:

    python -m plivo_mirror.replay transcript.json \
        --policies policies.txt \
        --threshold 0.7 \
        --model gpt-4o-mini

    # or sweep:
    python -m plivo_mirror.replay transcript.json \
        --policies policies.txt \
        --threshold-sweep 0.4,0.5,0.6,0.7,0.8,0.9

Transcript format — a JSON list of turn objects (oldest first):

    [
      {"role": "customer", "text": "Large pepperoni, actually mushroom"},
      {"role": "agent", "text": "Got it, one large pepperoni and one mushroom.",
       "tool_calls": [{"name": "place_order", "args": {"items": ["pepperoni","mushroom"]}}]
      },
      ...
    ]

For every AGENT turn, Mirror's full pipeline runs against the history
preceding it. The output is a table showing which turns would have
triggered intervention at each threshold.

Helps customers pick the right threshold + policies BEFORE going live.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, ToolCallIntent, TurnPayload
from plivo_mirror.scorer.llm import LLMScorer
from plivo_mirror.scorer.pregate import should_score
from plivo_mirror.scorer.tool_gate import ToolGate
from plivo_mirror.context import SupervisorContext, Verdict


@dataclass
class ReplayResult:
    turn_index: int
    customer_text: str
    primary_text: str
    pregate_reason: str
    scored: bool
    score: float
    should_intervene: bool
    blocked_tool: str | None
    reason: str


def _load_transcript(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise SystemExit("transcript must be a JSON list of turns")
    return data


def _load_policies(path: Path) -> list[str]:
    lines = [l.strip() for l in path.read_text().splitlines()]
    return [l for l in lines if l and not l.startswith("#")]


def _build_config(
    policies: list[str] | None,
    judging_prompt: str | None,
    threshold: float,
    *,
    model: str,
    base_url: str | None,
) -> MirrorConfig:
    from plivo_mirror.llm.openai import OpenAIClient

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required to run replay")

    return MirrorConfig(
        llm=OpenAIClient(api_key=api_key, model=model, base_url=base_url),
        policies=policies,
        judging_prompt=judging_prompt,
        intervention_threshold=threshold,
        # In replay we want every turn scored for visibility — let the
        # pregate run but force the scorer regardless.
        tiered_scoring_enabled=True,
    )


async def _replay_one_pass(
    config: MirrorConfig,
    transcript: list[dict[str, Any]],
) -> list[ReplayResult]:
    scorer = LLMScorer(config)
    tool_gate = ToolGate(config)
    ctx = SupervisorContext(call_uuid="replay")
    history: list[HistoryTurn] = []
    results: list[ReplayResult] = []

    for i, turn in enumerate(transcript):
        role = turn.get("role")
        text = turn.get("text") or ""
        if role == "customer":
            history.append(HistoryTurn(role="customer", text=text))
            continue
        if role != "agent":
            continue

        # Find the customer's most recent utterance.
        customer_text = ""
        for h in reversed(history):
            if h.role == "customer":
                customer_text = h.text
                break

        tool_calls_raw = turn.get("tool_calls") or []
        tool_calls = [
            ToolCallIntent(
                name=tc.get("name", ""),
                args=tc.get("args") or {},
                irreversible=bool(tc.get("irreversible")),
            )
            for tc in tool_calls_raw
        ]

        payload = TurnPayload(
            customer_text=customer_text,
            primary_text=text,
            tool_calls=tool_calls,
            history=list(history),
        )

        run, reason = should_score(payload, config, prev_intervention=False)
        if not run:
            results.append(
                ReplayResult(
                    turn_index=i,
                    customer_text=customer_text,
                    primary_text=text,
                    pregate_reason=reason,
                    scored=False,
                    score=0.0,
                    should_intervene=False,
                    blocked_tool=None,
                    reason="pregate-skipped",
                )
            )
            history.append(HistoryTurn(role="agent", text=text))
            continue

        verdict = await scorer.score(payload, ctx)
        # If speech scorer passed, run tool-gate.
        if not verdict.should_intervene and tool_calls and any(
            tool_gate.is_gated(tc.name) for tc in tool_calls
        ):
            tg_verdict = await tool_gate.review(
                tool_calls, customer_text, list(history), ctx
            )
            if tg_verdict.should_intervene:
                verdict = tg_verdict

        results.append(
            ReplayResult(
                turn_index=i,
                customer_text=customer_text,
                primary_text=text,
                pregate_reason=reason,
                scored=True,
                score=verdict.score,
                should_intervene=verdict.should_intervene,
                blocked_tool=verdict.blocked_tool,
                reason=verdict.reason,
            )
        )
        history.append(HistoryTurn(role="agent", text=text))

    return results


def _print_results(
    results: list[ReplayResult], *, threshold: float, label: str = ""
) -> None:
    print(f"\n{'─' * 72}")
    if label:
        print(f"  Replay results — {label}")
    print(f"{'─' * 72}")
    print(f"  {'#':<4} {'Score':<6} {'Action':<14} Reason")
    print(f"  {'─' * 4} {'─' * 6} {'─' * 14} {'─' * 45}")
    intervened = 0
    for r in results:
        if r.scored:
            action = "INTERVENE" if r.should_intervene else "allow"
            if r.should_intervene:
                intervened += 1
            score_str = f"{r.score:.2f}"
        else:
            action = "skipped"
            score_str = "  —"
        reason = r.reason or "—"
        if len(reason) > 45:
            reason = reason[:42] + "..."
        print(f"  {r.turn_index:<4} {score_str:<6} {action:<14} {reason}")
    total_agent = len(results)
    scored = sum(1 for r in results if r.scored)
    print(f"{'─' * 72}")
    print(
        f"  Agent turns: {total_agent}    Scored by LLM: {scored}    "
        f"Would intervene at threshold {threshold:.2f}: {intervened}"
    )
    if total_agent:
        rate = 100 * intervened / total_agent
        print(f"  Intervention rate: {rate:.1f}%")
    print()


async def _async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="plivo-mirror-replay",
        description="Replay a recorded transcript through Mirror to tune threshold + policies.",
    )
    parser.add_argument("transcript", type=Path, help="JSON transcript file")
    parser.add_argument(
        "--policies",
        type=Path,
        help="Path to a policies file (one policy per line; '#' lines ignored).",
    )
    parser.add_argument(
        "--judging-prompt-file",
        type=Path,
        help="Path to a judging-prompt file (mutually exclusive with --policies).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Intervention threshold (default 0.7).",
    )
    parser.add_argument(
        "--threshold-sweep",
        type=str,
        help="Comma-separated list of thresholds to sweep, e.g. 0.5,0.6,0.7,0.8",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        help="OpenAI/Azure model name (default gpt-4o-mini).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_API_URL"),
        help="OpenAI/Azure base URL (default from OPENAI_API_URL env).",
    )
    args = parser.parse_args(argv)

    if not args.transcript.exists():
        print(f"transcript not found: {args.transcript}", file=sys.stderr)
        return 1

    policies = _load_policies(args.policies) if args.policies else None
    judging_prompt = (
        args.judging_prompt_file.read_text() if args.judging_prompt_file else None
    )
    if not policies and not judging_prompt:
        print(
            "must supply --policies <file> or --judging-prompt-file <file>",
            file=sys.stderr,
        )
        return 1

    transcript = _load_transcript(args.transcript)

    thresholds: list[float] = (
        [float(x.strip()) for x in args.threshold_sweep.split(",")]
        if args.threshold_sweep
        else [args.threshold]
    )

    # Score once at the most-strict threshold; the score doesn't depend
    # on the threshold (only the should_intervene flag does), so we
    # can reuse the scores across the sweep.
    base_config = _build_config(
        policies, judging_prompt, threshold=min(thresholds),
        model=args.model, base_url=args.base_url,
    )
    base_results = await _replay_one_pass(base_config, transcript)

    for t in thresholds:
        adjusted = [
            ReplayResult(
                turn_index=r.turn_index,
                customer_text=r.customer_text,
                primary_text=r.primary_text,
                pregate_reason=r.pregate_reason,
                scored=r.scored,
                score=r.score,
                should_intervene=(r.scored and r.score >= t),
                blocked_tool=r.blocked_tool,
                reason=r.reason,
            )
            for r in base_results
        ]
        _print_results(adjusted, threshold=t, label=f"threshold={t:.2f}")

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
