"""SkyPlivo — flight booking agent.

Same shape as agent/primary.py (pizza). Different tools, different
vocabulary, same rigged item-capture failure mode. Mirror watches
this agent through the exact same machinery; no Mirror code needs
to know what flights are.

The two tools (book_flights + calculate_price) are reachable via the
standard OpenAI tool-use loop, identical to pizza. Bookings get
persisted into the existing `orders` table via db.place_order — the
shared dashboard renderer handles the items_json list either way.
"""

import json
import logging
import os

from openai import AsyncOpenAI

import db
from agents.travel.prompts import (
    CORRECTION_AGENT_SYSTEM_PROMPT_TRAVEL,
    PRIMARY_AGENT_SYSTEM_PROMPT_TRAVEL,
)

log = logging.getLogger("mirror.travel.agent")

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
            "name": "book_flights",
            "description": (
                "Submit a list of flight bookings to the reservation "
                "system. Each entry is one booking. Call this only "
                "after you have at least one destination + date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "destination": {"type": "string"},
                                "departure_date": {"type": "string"},
                                "class": {
                                    "type": "string",
                                    "description": "economy | business | first",
                                },
                                "passengers": {"type": "integer"},
                            },
                            "required": ["destination", "departure_date"],
                        },
                    }
                },
                "required": ["flights"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_price",
            "description": (
                "Calculate the price for one or more flights. Pass the "
                "same flights list you'd send to book_flights."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "flights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "destination": {"type": "string"},
                                "class": {"type": "string"},
                                "passengers": {"type": "integer"},
                            },
                            "required": ["destination"],
                        },
                    }
                },
                "required": ["flights"],
            },
        },
    },
]

# Pricing by destination keyword. Prices in INR.
_BASE_PRICES = {
    "mumbai": 4500,
    "delhi": 5200,
    "bangalore": 4800,
    "goa": 3900,
    "chennai": 4700,
    "kolkata": 5800,
    "hyderabad": 4300,
    "pune": 4100,
    "jaipur": 4400,
    "kochi": 5100,
}
_DEFAULT_PRICE = 5000


def _price_one(destination: str, class_: str | None, passengers: int) -> int:
    dest_l = (destination or "").lower()
    base = _DEFAULT_PRICE
    for city, p in _BASE_PRICES.items():
        if city in dest_l:
            base = p
            break
    cls = (class_ or "economy").lower()
    if "business" in cls:
        base *= 2
    elif "first" in cls:
        base *= 4
    return base * max(1, int(passengers or 1))


def _flight_to_item(flight: dict) -> str:
    dest = flight.get("destination", "?")
    date = flight.get("departure_date", "?")
    cls = flight.get("class") or "economy"
    pax = flight.get("passengers") or 1
    return f"{dest} {date} {cls} x{pax}"


_BAD_DATE_TOKENS = {"unknown", "tbd", "n/a", "none", "null", ""}


def _is_valid_flight(f: dict) -> bool:
    """Reject flights missing real destination or date.

    The rigged primary will sometimes fabricate args when it doesn't have
    the info (e.g. departure_date='unknown'). Those shouldn't end up in
    the customer's booking record."""
    dest = (f.get("destination") or "").strip().lower()
    date = (f.get("departure_date") or "").strip().lower()
    if not dest or dest in _BAD_DATE_TOKENS:
        return False
    if not date or date in _BAD_DATE_TOKENS:
        return False
    return True


def _execute_tool(name: str, args: dict, call_uuid: str) -> dict:
    if name == "book_flights":
        raw_flights = args.get("flights") or []
        flights = [f for f in raw_flights if isinstance(f, dict) and _is_valid_flight(f)]
        dropped = len(raw_flights) - len(flights)
        if dropped:
            log.info(
                "book_flights call=%s dropped %d invalid flights (missing dest/date)",
                call_uuid,
                dropped,
            )
        if not flights:
            # Nothing left to book — tell the agent to ask the customer.
            return {
                "status": "incomplete",
                "error": "missing destination or date",
                "count": 0,
            }
        items = [_flight_to_item(f) for f in flights]
        order_id = db.place_order(call_uuid, items)
        log.info(
            "book_flights call=%s id=%s count=%d items=%s",
            call_uuid,
            order_id,
            len(items),
            items,
        )
        return {"booking_id": order_id, "status": "booked", "count": len(items)}
    if name == "calculate_price":
        flights = args.get("flights") or []
        total = sum(
            _price_one(f.get("destination", ""), f.get("class"), f.get("passengers") or 1)
            for f in flights
        )
        return {"total": total, "currency": "INR"}
    return {"error": f"unknown tool: {name}"}


async def run_turn(
    call_uuid: str,
    transcript_history: list[dict],
    extra_system_note: str | None = None,
    return_details: bool = False,
):
    """Travel agent — same signature as agent.primary.run_turn so the
    dispatcher can swap them transparently."""
    messages: list[dict] = [
        {"role": "system", "content": PRIMARY_AGENT_SYSTEM_PROMPT_TRAVEL}
    ]
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
    """Travel correction — same shape as the pizza version. No tools."""
    system = CORRECTION_AGENT_SYSTEM_PROMPT_TRAVEL.format(
        evidence_json=json.dumps(mirror_evidence, ensure_ascii=False)
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in transcript_history:
        role = "user" if turn["role"] == "customer" else "assistant"
        messages.append({"role": role, "content": turn["text"]})

    client = _openai()
    resp = await client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        messages=messages,
    )
    text = (resp.choices[0].message.content or "").strip()
    log.info("correction call=%s text=%s", call_uuid, text)
    return text
