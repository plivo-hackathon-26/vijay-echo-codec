"""Mirror value model — translates technical interventions into dollars.

Pure functions over the existing tables (calls / orders / mirror_events
/ interventions). No writes. No external dependencies. Constants at the
top are tunable per-deployment; in production these would come from
agent-specific config.

The formula is intentionally simple and explainable on a demo slide:

  dollar_saved =   churn_loss_avoided
                 + support_cost_avoided
                 + reputation_cost_avoided

Where:
  churn_loss_avoided   = order_value
                       × CHURN_PROBABILITY_PER_FAILURE
                       × CUSTOMER_LIFETIME_MULTIPLIER
  support_cost_avoided = intervention_count × SUPPORT_TICKET_COST
  reputation_cost_avoided = ONE_STAR_REVIEW_COST  (once, if any
                            intervention fired — one bad call → one
                            review)
  order_value          = ORDER_VALUE_BASE
                       + max(item_count - 1, 0) × ORDER_VALUE_PER_ITEM

All values are USD.
"""

import json
import logging
from datetime import datetime, timezone

import db

log = logging.getLogger("mirror.value_model")


# ─────────────────────── tunable constants ──────────────────────────────

ORDER_VALUE_BASE = 25.00            # avg pizza order base
ORDER_VALUE_PER_ITEM = 12.00        # each additional item
CHURN_PROBABILITY_PER_FAILURE = 0.18  # 18% chance of churn after bad UX
CUSTOMER_LIFETIME_MULTIPLIER = 6.5    # avg future orders per retained customer
SUPPORT_TICKET_COST = 8.50          # avg cost to resolve one support ticket
ONE_STAR_REVIEW_COST = 12.00        # reputational damage per 1-star review


_ZERO = {
    "order_value": 0.0,
    "item_count": 0,
    "intervention_count": 0,
    "estimated": False,
    "churn_loss_avoided": 0.0,
    "support_cost_avoided": 0.0,
    "reputation_cost_avoided": 0.0,
    "total_saved": 0.0,
    "calculation_breakdown": "No intervention occurred — nothing to save.",
}


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _last_order_item_count(call_uuid: str) -> int:
    """Number of items the agent ended up placing. If multiple orders
    were placed (corrections etc.), use the LATEST one — that's the
    one the customer was actually billed for."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT items_json FROM orders WHERE call_uuid = ? "
            "ORDER BY id DESC LIMIT 1",
            (call_uuid,),
        ).fetchone()
    if not row or not row["items_json"]:
        return 0
    try:
        items = json.loads(row["items_json"])
    except (TypeError, ValueError):
        return 0
    return len(items) if isinstance(items, list) else 0


def _intervention_count(call_uuid: str) -> int:
    """How many intervention-grade mirror_events fired on this call.
    Counted from mirror_events (works for both pattern and semantic
    fires) rather than interventions (which only records
    customer-facing buffer+correction)."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM mirror_events "
            "WHERE call_uuid = ? AND intervention_needed = 1",
            (call_uuid,),
        ).fetchone()
    return int(row["n"]) if row else 0


def _hypothetical_intervention_count(call_uuid: str) -> int:
    """For Mirror-OFF calls that ended up WRONG (caught by the post-hoc
    pattern scan that runs at end_call), treat them as if a single
    intervention WOULD have fired had Mirror been on. This is what
    powers the LOSS column on the compare page — it estimates what
    Mirror would have saved if it had been running."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(mirror_enabled, 1) AS mirror_enabled, final_outcome "
            "FROM calls WHERE call_uuid = ?",
            (call_uuid,),
        ).fetchone()
    if not row:
        return 0
    # Only kicks in when Mirror was OFF — i.e. detection couldn't write
    # real events. ON calls without events truly had no failure.
    if int(row["mirror_enabled"]) == 1:
        return 0
    if row["final_outcome"] == "wrong_order":
        return 1
    return 0


# ─────────────────────── public API ─────────────────────────────────────


def calculate_value_saved(call_uuid: str) -> dict:
    """Per-call savings breakdown.

    Returns a flat dict with all the component values plus a
    human-readable calculation_breakdown string. A call with no
    intervention returns zeros + a clean "nothing to save" message
    (instead of None) so templates can render uniformly.
    """
    if not call_uuid:
        return {**_ZERO}

    intervention_count = _intervention_count(call_uuid)
    estimated = False
    if intervention_count <= 0:
        # Fallback for OFF calls flagged WRONG by the post-hoc pattern
        # scan — let the LOSS column show what Mirror WOULD have saved.
        intervention_count = _hypothetical_intervention_count(call_uuid)
        estimated = intervention_count > 0
    if intervention_count <= 0:
        return {**_ZERO}

    item_count = _last_order_item_count(call_uuid)
    order_value = ORDER_VALUE_BASE + max(item_count - 1, 0) * ORDER_VALUE_PER_ITEM
    churn_loss_avoided = (
        order_value * CHURN_PROBABILITY_PER_FAILURE * CUSTOMER_LIFETIME_MULTIPLIER
    )
    support_cost_avoided = intervention_count * SUPPORT_TICKET_COST
    reputation_cost_avoided = ONE_STAR_REVIEW_COST  # one bad call = one review
    total_saved = (
        churn_loss_avoided + support_cost_avoided + reputation_cost_avoided
    )

    breakdown = (
        f"Order value ${order_value:.2f} "
        f"× {int(CHURN_PROBABILITY_PER_FAILURE * 100)}% churn risk "
        f"× {CUSTOMER_LIFETIME_MULTIPLIER:.1f} lifetime orders "
        f"= ${churn_loss_avoided:.2f} retained.\n"
        f"Plus {intervention_count} support ticket"
        f"{'s' if intervention_count != 1 else ''} avoided "
        f"(${support_cost_avoided:.2f}). "
        f"Plus 1 reputation hit avoided (${reputation_cost_avoided:.2f}).\n"
        f"Total: ${total_saved:.2f}"
    )
    if estimated:
        breakdown = (
            "(Estimated — Mirror was off; this is what would have been saved "
            "had it been running.)\n"
        ) + breakdown

    return {
        "order_value": round(order_value, 2),
        "item_count": item_count,
        "intervention_count": intervention_count,
        "estimated": estimated,
        "churn_loss_avoided": round(churn_loss_avoided, 2),
        "support_cost_avoided": round(support_cost_avoided, 2),
        "reputation_cost_avoided": round(reputation_cost_avoided, 2),
        "total_saved": round(total_saved, 2),
        "calculation_breakdown": breakdown,
    }


def calculate_total_value_saved_today() -> dict:
    """Sum of total_saved across all today's calls that had interventions.

    Returns:
      {
        "total_saved": float,
        "intervention_count": int,    # total interventions today
        "calls_count": int,           # calls with at least one intervention today
        "last_call_saved": float,     # most-recent intervention-bearing call's saved
        "last_call_uuid": str | None,
      }
    """
    today = _today_prefix()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT c.call_uuid, c.started_at "
            "FROM calls c "
            "WHERE c.started_at LIKE ? "
            "  AND EXISTS ("
            "    SELECT 1 FROM mirror_events m "
            "    WHERE m.call_uuid = c.call_uuid AND m.intervention_needed = 1"
            "  ) "
            "ORDER BY c.started_at DESC",
            (f"{today}%",),
        ).fetchall()

    total_saved = 0.0
    total_interv = 0
    last_call_saved = 0.0
    last_call_uuid: str | None = None

    for i, row in enumerate(rows):
        s = calculate_value_saved(row["call_uuid"])
        total_saved += s["total_saved"]
        total_interv += s["intervention_count"]
        if i == 0:
            last_call_saved = s["total_saved"]
            last_call_uuid = row["call_uuid"]

    return {
        "total_saved": round(total_saved, 2),
        "intervention_count": total_interv,
        "calls_count": len(rows),
        "last_call_saved": round(last_call_saved, 2),
        "last_call_uuid": last_call_uuid,
    }


def calculate_timeseries_today() -> dict:
    """Chronologically-ordered running totals for today's calls, split
    by Mirror ON (saved) vs Mirror OFF + wrong_order (would-have-been
    lost). Powers the chart modal on the dollar-saved stat card.

    Returns:
      {
        "points": [
          {"t": "13:05:42", "saved": 0.0, "lost": 68.20, "call_uuid": "..."},
          {"t": "13:10:11", "saved": 124.50, "lost": 68.20, "call_uuid": "..."},
          ...
        ],
        "totals": {"saved": float, "lost": float},
      }
    """
    today = _today_prefix()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT call_uuid, started_at, COALESCE(mirror_enabled, 1) AS me "
            "FROM calls WHERE started_at LIKE ? "
            "ORDER BY started_at ASC",
            (f"{today}%",),
        ).fetchall()

    points: list[dict] = []
    cum_saved = 0.0
    cum_lost = 0.0
    for r in rows:
        v = calculate_value_saved(r["call_uuid"])
        if v["total_saved"] <= 0:
            continue
        if int(r["me"]) == 1:
            cum_saved += v["total_saved"]
        else:
            # Mirror was off and the call ended wrong — count as money
            # Mirror would have saved had it been running.
            cum_lost += v["total_saved"]
        ts = (r["started_at"] or "")[11:19]
        points.append(
            {
                "t": ts,
                "saved": round(cum_saved, 2),
                "lost": round(cum_lost, 2),
                "call_uuid": r["call_uuid"],
            }
        )

    return {
        "points": points,
        "totals": {
            "saved": round(cum_saved, 2),
            "lost": round(cum_lost, 2),
        },
    }


def calculate_value_saved_for_compare(
    call_uuid_off: str | None, call_uuid_on: str | None
) -> dict:
    """Side-by-side: what would have been lost vs what was saved.

    Both numbers use the same formula. The OFF call's number is what
    the customer would have cost the business (LOSS). The ON call's
    number is what Mirror saved (SAVED). For the demo they're typically
    similar because both calls follow the same scenario — that's the
    point: Mirror saved exactly what would have been lost.
    """
    loss = calculate_value_saved(call_uuid_off) if call_uuid_off else {**_ZERO}
    saved = calculate_value_saved(call_uuid_on) if call_uuid_on else {**_ZERO}
    return {
        "loss_without": loss,
        "saved_with": saved,
        "delta": round(saved["total_saved"] - loss["total_saved"], 2),
    }
