import json
import logging
import os

from openai import AsyncOpenAI

import db
from prompts import PRIMARY_AGENT_SYSTEM_PROMPT

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


async def run_turn(call_uuid: str, transcript_history: list[dict]) -> str:
    """Run one agent turn against the conversation history.

    transcript_history: list of {"role": "customer" | "agent", "text": str}.
    Returns the agent's final text response and persists it to `turns`.
    """
    messages: list[dict] = [{"role": "system", "content": PRIMARY_AGENT_SYSTEM_PROMPT}]
    for turn in transcript_history:
        role = "user" if turn["role"] == "customer" else "assistant"
        messages.append({"role": role, "content": turn["text"]})

    client = _openai()
    final_text = ""

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
    return final_text
