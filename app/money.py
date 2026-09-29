"""Money helpers. Amounts are stored as integer paise, never floats."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_paise(value) -> int | None:
    """Convert a rupee amount (number or string like '1,23,456.50' or '₹249') to integer paise."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            dec = Decimal(str(value))
        except InvalidOperation:
            return None
    else:
        cleaned = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
        if cleaned in ("", "-", ".", "-."):
            return None
        try:
            dec = Decimal(cleaned)
        except InvalidOperation:
            return None
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_inr(paise: int | None) -> str:
    """Format paise like 'Rs 1,23,456.50' using the Indian digit grouping."""
    if paise is None:
        return ""
    sign = "-" if paise < 0 else ""
    rupees, ps = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts + [tail])
    return f"{sign}Rs {digits}.{ps:02d}"
