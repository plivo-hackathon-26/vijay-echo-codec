import json
import logging
import os

from openai import AsyncOpenAI

import db
from prompts import CORRECTION_AGENT_SYSTEM_PROMPT, PRIMARY_AGENT_SYSTEM_PROMPT

log = logging.getLogger("mirror.agent")

_client: AsyncOpenAI | None = None


def _openai() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        base_url = os.getenv("OPENAI_API_URL", "").strip().rstrip("/") or None
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        _client = AsyncOpenAI(api_key=key, base_url=base_url)
    return _client


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": (
                "Submit the customer's pizza order to the kitchen. "
                "Call this only after the customer has confirmed their order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of pizza items being ordered, "
                            "e.g. ['large pepperoni', 'small mushroom']"
                        ),
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_total",
            "description": "Calculate the total cost in dollars for a list of pizza items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["items"],
            },
        },
    },
]

_PRICES = {
    "cheese": 8.0,
    "pepperoni": 12.0,
    "mushroom": 10.0,
}
_DEFAULT_PRICE = 11.0
_LARGE_MODIFIER = 3.0


def _price_item(item: str) -> float:
    item_l = item.lower()
    base = _DEFAULT_PRICE
    for key, price in _PRICES.items():
        if key in item_l:
            base = price
            break
    if "large" in item_l:
        base += _LARGE_MODIFIER
    return round(base, 2)


def _execute_tool(name: str, args: dict, call_uuid: str) -> dict:
    if name == "place_order":
        items = args.get("items") or []
        order_id = db.place_order(call_uuid, items)
        log.info("place_order call=%s order=%s items=%s", call_uuid, order_id, items)
        return {"status": "placed", "order_id": order_id}
    if name == "calculate_total":
        items = args.get("items") or []
        total = round(sum(_price_item(i) for i in items), 2)
        return {"total": total}
    return {"error": f"unknown tool: {name}"}


async def run_turn(
    call_uuid: str,
    transcript_history: list[dict],
    extra_system_note: str | None = None,
    return_details: bool = False,
):
    """Run one agent turn against the conversation history.

    transcript_history: list of {"role": "customer" | "agent", "text": str}.
    extra_system_note: optional additional system message appended after
        the base system prompt — used by Mirror to install a one-shot
        post-correction override on the turn immediately following an
        intervention.
    return_details: when True, return a dict with the final text plus
        every tool call the agent made during this turn (with parsed
        args + the tool's result). Mirror's semantic reviewer needs
        this to judge whether the agent's plan matches the customer's
        intent. When False (default), just returns the final text
        string — keeps backward compatibility with callers that only
        want the spoken response.
    """
    messages: list[dict] = [{"role": "system", "content": PRIMARY_AGENT_SYSTEM_PROMPT}]
    if extra_system_note:
        messages.append({"role": "system", "content": extra_system_note})
    for turn in transcript_history:
        role = "user" if turn["role"] == "customer" else "assistant"
        messages.append({"role": role, "content": turn["text"]})

    client = _openai()
    final_text = ""
    tool_calls_made: list[dict] = []

    for _ in range(3):
        resp = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
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
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _execute_tool(tc.function.name, args, call_uuid)
                tool_calls_made.append(
                    {"name": tc.function.name, "args": args, "result": result}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )
            continue

        final_text = (msg.content or "").strip()
        break

    if not final_text:
        final_text = "Sorry, can you say that again?"

    db.add_turn(call_uuid, "agent", final_text)
    if return_details:
        return {"text": final_text, "tool_calls": tool_calls_made}
    return final_text


async def run_correction_turn(
    call_uuid: str,
    transcript_history: list[dict],
    mirror_evidence: dict,
) -> str:
    """Generate a single corrective response given Mirror's evidence.

    Tool use is disabled — this turn must only produce text. The caller
    is responsible for persisting the result to the turns table.
    """
    system = CORRECTION_AGENT_SYSTEM_PROMPT.format(
        evidence_json=json.dumps(mirror_evidence, ensure_ascii=False)
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in transcript_history:
        role = "user" if turn["role"] == "customer" else "assistant"
        messages.append({"role": role, "content": turn["text"]})

    client = _openai()
    # No `tools` and no `tool_choice` — Azure rejects tool_choice
    # when tools is absent ("'tool_choice' is only allowed when
    # 'tools' are specified"). Without tools the model can't call
    # any tool anyway, which is what we want for this turn.
    resp = await client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        messages=messages,
    )
    text = (resp.choices[0].message.content or "").strip()
    log.info("correction call=%s text=%s", call_uuid, text)
    return text
