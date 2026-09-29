"""Merchant normalisation, merchant rules, and category lookup."""
from __future__ import annotations

import re
import sqlite3


def merchant_key(name: str | None) -> str:
    """Lowercase alphanumerics only, so 'SWIGGY  Ltd.' and 'swiggy ltd' match the same rule."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def clean_merchant(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).strip(" .,-*")[:120]


def find_rule(conn: sqlite3.Connection, merchant: str | None):
    key = merchant_key(merchant)
    if not key:
        return None
    rows = conn.execute("SELECT * FROM merchant_rules ORDER BY LENGTH(pattern) DESC").fetchall()
    for row in rows:
        if row["pattern"] and row["pattern"] in key:
            return row
    return None


def resolve_category(paths: dict[int, str], label: str | None) -> int | None:
    """Match a model-provided category label to an id: exact path, then leaf name, case-insensitive."""
    if not label:
        return None
    want = label.strip().lower()
    for cid, path in paths.items():
        if path.lower() == want:
            return cid
    leaf = want.split(">")[-1].strip()
    matches = [cid for cid, path in paths.items() if path.lower().split(">")[-1].strip() == leaf]
    return matches[0] if len(matches) == 1 else None


def recent_examples(conn: sqlite3.Connection, paths: dict[int, str], limit: int) -> list[str]:
    """Turn the user's latest category/kind corrections into short few-shot lines for the prompt."""
    rows = conn.execute(
        "SELECT merchant, field, new_value FROM corrections WHERE field IN ('category_id', 'kind') "
        "AND merchant IS NOT NULL AND merchant != '' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        value = r["new_value"]
        if r["field"] == "category_id":
            try:
                value = paths.get(int(value), value)
            except (TypeError, ValueError):
                pass
            out.append(f"{r['merchant']} -> category {value}")
        else:
            out.append(f"{r['merchant']} -> kind {value}")
    return out
