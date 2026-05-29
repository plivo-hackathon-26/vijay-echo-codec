"""Supervised OpenAI tool-use loop.

Wraps the standard OpenAI chat-completions tool-use pattern with
Mirror's tool-gate INLINE. The result: irreversible tools never fire
until Mirror has approved the agent's intent.

The customer's agent code becomes:

    result = await run_supervised_openai_loop(
        llm_client=AsyncOpenAI(...),
        model="gpt-4o-mini",
        system_prompt="...",
        transcript=[...],
        tool_specs=[...],                         # OpenAI tools schema
        tool_executors={"place_order": _place_order, ...},
        supervisor=call_supervisor,               # the per-call CallSupervisor
        customer_text="<latest customer utterance>",
        irreversible=("place_order", "charge_card"),
    )

    if result.blocked:
        # Mirror's tool-gate said no. The CallSupervisor has already
        # intervened (spoke the correction); the caller's `result.text`
        # is the correction text.
        ...
    else:
        # All tools executed successfully. `result.text` is the agent's
        # final spoken response.
        ...

The loop is what the user-facing pizza_plivo agent uses internally,
and is the recommended path for any new Plivo voice agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from plivo_mirror.context import HistoryTurn, ToolCallIntent, Verdict

if TYPE_CHECKING:
    from plivo_mirror.supervisor import CallSupervisor

log = logging.getLogger("plivo_mirror.agents.openai_loop")


ToolExecutor = Callable[[dict[str, Any]], Awaitable[Any] | Any]


@dataclass
class AgentResult:
    """What ``run_supervised_openai_loop`` returns."""

    text: str
    tool_intents: list[ToolCallIntent] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    block_verdict: Verdict | None = None


async def run_supervised_openai_loop(
    *,
    llm_client: Any,                       # openai.AsyncOpenAI
    model: str,
    system_prompt: str,
    transcript: list[dict[str, str]] | list[HistoryTurn],
    tool_specs: list[dict[str, Any]],
    tool_executors: dict[str, ToolExecutor],
    supervisor: "CallSupervisor",
    customer_text: str,
    extra_system_note: str | None = None,
    irreversible: tuple[str, ...] = (),
    max_rounds: int = 3,
) -> AgentResult:
    """Run an OpenAI chat-completions tool-use loop with Mirror gating
    every tool call BEFORE it executes.

    Per round:
      1. Call ``llm_client.chat.completions.create`` with tools.
      2. If the LLM picked tool calls:
         - Build ``ToolCallIntent`` for each.
         - Ask ``supervisor.gate_tool_call(...)`` whether to allow them.
         - If blocked → run ``supervisor.intervene(...)`` and return
           ``AgentResult(blocked=True, text=correction_text)``. Tools
           did NOT fire.
         - If approved → execute via ``tool_executors`` and feed
           results back to the LLM.
      3. If the LLM returned plain text → return ``AgentResult(text=...)``.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if extra_system_note:
        messages.append({"role": "system", "content": extra_system_note})

    # Tolerate either raw transcript dicts or HistoryTurn dataclasses.
    for t in transcript:
        if isinstance(t, HistoryTurn):
            role = "user" if t.role == "customer" else "assistant"
            messages.append({"role": role, "content": t.text})
        else:
            role = "user" if t.get("role") == "customer" else "assistant"
            messages.append({"role": role, "content": t.get("text") or ""})

    irreversible_set = {n.lower() for n in irreversible}
    tool_intents: list[ToolCallIntent] = []
    tool_results: list[dict[str, Any]] = []

    for _round_idx in range(max_rounds):
        resp = await llm_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tool_specs,
            tool_choice="auto",
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            # 1. Build intents.
            pending: list[ToolCallIntent] = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                pending.append(
                    ToolCallIntent(
                        name=tc.function.name,
                        args=args,
                        irreversible=tc.function.name.lower() in irreversible_set,
                        tool_call_id=tc.id,
                    )
                )

            # 2. Mirror's tool-gate.
            gate_verdict = await supervisor.gate_tool_call(
                customer_text=customer_text,
                intents=pending,
            )
            log.info(
                "tool-gate verdict score=%.2f intervene=%s tools=%s",
                gate_verdict.score,
                gate_verdict.should_intervene,
                [tc.name for tc in pending],
            )

            if gate_verdict.should_intervene:
                # 3a. Blocked — run intervention. Tools NEVER fire.
                result = await supervisor.intervene(gate_verdict)
                return AgentResult(
                    text=result.correction_text,
                    tool_intents=pending,
                    tool_results=[],
                    blocked=True,
                    block_verdict=gate_verdict,
                )

            # 3b. Approved — record + execute.
            tool_intents.extend(pending)

            # Append the assistant turn carrying the tool_calls.
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            for intent in pending:
                # v0.1.0a4: dedupe for irreversible tools. If the agent
                # already fired this exact call (same args) earlier in
                # this Plivo call, return the cached result instead of
                # re-executing. Prevents duplicate place_order/charges
                # when the LLM re-runs the tool on a confirmation turn.
                args_json = json.dumps(intent.args, sort_keys=True, ensure_ascii=False)
                is_irrev = intent.name.lower() in irreversible_set
                cached = (
                    supervisor.already_committed(intent.name, args_json)
                    if is_irrev else None
                )
                if cached is not None:
                    log.info(
                        "dedupe: %s(%s) already committed; returning cached result",
                        intent.name,
                        args_json[:80],
                    )
                    result_dict: Any = {**cached, "_dedupe_skipped": True}
                else:
                    executor = tool_executors.get(intent.name)
                    if executor is None:
                        log.warning("no executor for tool %r — returning {error}", intent.name)
                        result_dict = {"error": f"unknown tool: {intent.name}"}
                    else:
                        try:
                            ret = executor(intent.args)
                            result_dict = await ret if asyncio.iscoroutine(ret) else ret
                        except Exception as e:
                            log.exception("tool %r executor raised", intent.name)
                            result_dict = {"error": str(e)}
                    # Memoize successful irreversible commits so a later
                    # turn with the same args is a no-op.
                    if (
                        is_irrev
                        and isinstance(result_dict, dict)
                        and not result_dict.get("error")
                        and (
                            result_dict.get("status") == "placed"
                            or "order_id" in result_dict
                            or "booking_id" in result_dict
                            or "transaction_id" in result_dict
                        )
                    ):
                        supervisor.note_committed(intent.name, args_json, dict(result_dict))
                tool_results.append({"intent": intent, "result": result_dict})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": intent.tool_call_id or "",
                        "content": json.dumps(result_dict),
                    }
                )

            # Loop continues — LLM gets tool results, generates final text.
            continue

        # No tool calls → done.
        return AgentResult(
            text=(msg.content or "").strip(),
            tool_intents=tool_intents,
            tool_results=tool_results,
            blocked=False,
        )

    # Exhausted max rounds without a final text reply.
    return AgentResult(
        text="Sorry, can you say that again?",
        tool_intents=tool_intents,
        tool_results=tool_results,
        blocked=False,
    )


__all__ = ["AgentResult", "run_supervised_openai_loop", "ToolExecutor"]
