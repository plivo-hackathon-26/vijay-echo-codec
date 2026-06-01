"""Phase 1 — entity validators (pure, conservative)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from plivo_mirror.state.entities import (
    validate,
    validate_amount,
    validate_date,
    validate_item,
    validate_name,
)


# ── amount ──────────────────────────────────────────────────────────


def test_amount_currency_symbol():
    e = validate_amount("$12.50")
    assert e is not None and e.value == Decimal("12.50") and e.kind == "amount"


def test_amount_words_and_commas():
    assert validate_amount("12 dollars").value == Decimal("12")
    assert validate_amount("1,299.00").value == Decimal("1299.00")
    assert validate_amount("7").value == Decimal("7")


def test_amount_rejects_negative_and_garbage():
    assert validate_amount("-5") is None
    assert validate_amount("free") is None
    assert validate_amount(None) is None


# ── date ────────────────────────────────────────────────────────────


def test_date_iso_and_us_numeric():
    assert validate_date("2026-06-15").value == date(2026, 6, 15)
    assert validate_date("06/15/2026").value == date(2026, 6, 15)


def test_date_month_name():
    assert validate_date("Jun 15 2026").value == date(2026, 6, 15)


def test_date_rejects_garbage():
    assert validate_date("someday") is None
    assert validate_date("") is None
    assert validate_date(None) is None


# ── name ────────────────────────────────────────────────────────────


def test_name_valid_and_normalized():
    e = validate_name("  Jane   Doe ")
    assert e is not None and e.value == "Jane Doe"


def test_name_rejects_numeric_and_empty():
    assert validate_name("123") is None
    assert validate_name("") is None
    assert validate_name(None) is None


# ── item ────────────────────────────────────────────────────────────


def test_item_normalizes_case_and_space():
    e = validate_item("  Large   Pepperoni ")
    assert e is not None and e.value == "large pepperoni"


def test_item_catalog_enforced():
    catalog = {"large pepperoni", "medium mushroom"}
    assert validate_item("large pepperoni", catalog=catalog) is not None
    assert validate_item("unicorn topping", catalog=catalog) is None


def test_item_rejects_empty():
    assert validate_item("   ") is None
    assert validate_item(None) is None


# ── dispatch ────────────────────────────────────────────────────────


def test_validate_dispatch():
    assert validate("amount", "$5").value == Decimal("5")
    assert validate("name", "Bob").value == "Bob"
    assert validate("date", "bad") is None
