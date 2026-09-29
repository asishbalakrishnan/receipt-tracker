"""Deterministic checks run on every extracted record. Failures send it to the review queue."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

KEY_FIELDS = ("merchant", "date", "total")


def gstin_checksum_ok(gstin: str) -> bool:
    """Validate the 15th character of a GSTIN using the published mod-36 algorithm."""
    total = 0
    for i, ch in enumerate(gstin[:14]):
        product = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36] == gstin[14]


def gstin_valid(gstin: str | None) -> bool:
    if not gstin:
        return False
    g = gstin.strip().upper()
    return bool(GSTIN_RE.match(g)) and gstin_checksum_ok(g)


def parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def validate(rec: dict, confidence: dict, threshold: float, today: date | None = None) -> list[str]:
    """Return a list of flag strings; an empty list means the record can be auto-accepted.

    rec keys used: merchant, date, amount_paise, currency, tax_amount_paise, gstin, line_items
    (each line item has amount_paise).
    """
    today = today or date.today()
    flags: list[str] = []

    if not (rec.get("merchant") or "").strip():
        flags.append("merchant_missing")

    total = rec.get("amount_paise")
    if total is None:
        flags.append("total_missing")
    elif total <= 0:
        flags.append("total_not_positive")

    d = parse_date(rec.get("date"))
    if rec.get("date") in (None, ""):
        flags.append("date_missing")
    elif d is None:
        flags.append("date_unreadable")
    elif d > today + timedelta(days=1):
        flags.append("date_in_future")
    elif d < today - timedelta(days=5 * 365):
        flags.append("date_over_5_years_old")

    if (rec.get("currency") or "INR").upper() != "INR":
        flags.append("non_inr_currency")

    gstin = rec.get("gstin")
    if gstin and not gstin_valid(gstin):
        flags.append("gstin_invalid")

    items = [li for li in rec.get("line_items", []) if li.get("amount_paise") is not None]
    if items and total:
        items_sum = sum(li["amount_paise"] for li in items)
        tax = rec.get("tax_amount_paise") or 0
        tolerance = max(200, int(abs(total) * 0.01))
        if abs(items_sum - total) > tolerance and abs(items_sum + tax - total) > tolerance:
            flags.append("line_items_mismatch")

    tax_paise = rec.get("tax_amount_paise")
    if tax_paise is not None and total and tax_paise > total:
        flags.append("tax_exceeds_total")

    for name in KEY_FIELDS + ("category",):
        score = confidence.get(name)
        if score is not None and score < threshold:
            flags.append(f"low_confidence:{name}")
    for name, present in (("tax", tax_paise is not None), ("gstin", bool(gstin))):
        score = confidence.get(name)
        if present and score is not None and score < threshold:
            flags.append(f"low_confidence:{name}")

    return flags
