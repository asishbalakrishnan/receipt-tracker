"""Business logic: ingest receipts, apply rules and validation, edit, summarise, export."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from datetime import date, datetime, timezone

from . import classify, db
from .config import Settings
from .crypto import Vault
from .emailparse import parse_eml
from .extractor import ExtractInput, Extractor
from .money import to_paise
from .validate import parse_date, validate

PAYMENT_METHODS = {"upi", "card", "cash", "other"}
KINDS = {"business", "personal"}
EDITABLE = {
    "amount_paise", "currency", "date", "merchant", "category_id", "kind", "payment_method",
    "tax_amount_paise", "gstin", "notes", "tags", "line_items",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sniff(data: bytes, filename: str) -> tuple[str, str] | None:
    """Return (source, media_type) from file content, or None if unsupported."""
    if data[:4] == b"%PDF":
        return "pdf", "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "photo", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "photo", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "photo", "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "photo", "image/gif"
    if filename.lower().endswith(".eml") or data[:5] in (b"From:", b"Recei", b"Retur", b"MIME-", b"Date:", b"Subje"):
        return "email", "message/rfc822"
    return None


def row_to_txn(row: sqlite3.Row, paths: dict[int, str] | None = None) -> dict:
    d = dict(row)
    for key in ("line_items", "field_origin", "confidence", "flags", "tags"):
        d[key] = json.loads(d[key] or ("{}" if key in ("field_origin", "confidence") else "[]"))
    d["has_file"] = bool(d.get("receipt_file"))
    d.pop("receipt_file", None)
    if paths is not None:
        d["category"] = paths.get(d["category_id"]) if d["category_id"] else None
    return d


def get_txn(conn: sqlite3.Connection, txn_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return row_to_txn(row, db.category_paths(conn)) if row else None


# ------------------------------------------------------------------ ingest

def normalise(raw: dict) -> tuple[dict, dict]:
    """Convert the model's JSON into our record shape. Returns (record, confidence)."""
    payment = str(raw.get("payment_method") or "other").lower()
    kind = str(raw.get("kind") or "personal").lower()
    items = []
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        items.append({
            "description": str(li.get("description") or "")[:200],
            "quantity": li.get("quantity"),
            "amount_paise": to_paise(li.get("amount")),
        })
    conf = {k: v for k, v in (raw.get("confidence") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}
    gstin = (raw.get("gstin") or None)
    rec = {
        "merchant": classify.clean_merchant(raw.get("merchant")),
        "date": str(raw["date"])[:10] if raw.get("date") else None,
        "amount_paise": to_paise(raw.get("total")),
        "currency": str(raw.get("currency") or "INR").upper()[:3],
        "tax_amount_paise": to_paise(raw.get("tax_total")),
        "gstin": gstin.strip().upper() if isinstance(gstin, str) and gstin.strip() else None,
        "payment_method": payment if payment in PAYMENT_METHODS else "other",
        "kind": kind if kind in KINDS else "personal",
        "line_items": items,
        "notes": str(raw.get("notes") or "")[:500],
        "category_label": raw.get("category"),
    }
    return rec, conf


def ingest_file(conn: sqlite3.Connection, settings: Settings, vault: Vault, extractor: Extractor,
                data: bytes, filename: str) -> list[dict]:
    """Store one uploaded file and create one transaction per receipt found in it."""
    kind = sniff(data, filename)
    if kind is None:
        raise ValueError("Unsupported file type. Use JPG, PNG, WebP, PDF, or a forwarded .eml file.")
    source, media_type = kind

    if source == "email":
        parts = parse_eml(data)
        if not parts:
            raise ValueError("The email has no PDF, image attachment, or readable text.")
        out = [_ingest_one(conn, settings, vault, extractor, p.data, p.media_type, p.text, "email", p.name)
               for p in parts]
        return out
    return [_ingest_one(conn, settings, vault, extractor, data, media_type, None, source, filename)]


def _ingest_one(conn, settings, vault, extractor, data, media_type, text, source, filename) -> dict:
    paths = db.category_paths(conn)
    sha = hashlib.sha256(data if data is not None else (text or "").encode()).hexdigest()
    flags: list[str] = []
    origin: dict[str, str] = {}
    rec = {"merchant": "", "date": None, "amount_paise": None, "currency": "INR", "tax_amount_paise": None,
           "gstin": None, "payment_method": "other", "kind": "personal", "line_items": [], "notes": "",
           "category_label": None}
    conf: dict = {}

    try:
        examples = classify.recent_examples(conn, paths, settings.few_shot_corrections)
        raw = extractor.extract(ExtractInput(data, media_type, text), sorted(paths.values()), examples)
        rec, conf = normalise(raw)
        for key in ("merchant", "date", "amount_paise", "currency", "tax_amount_paise", "gstin",
                    "payment_method", "kind", "line_items"):
            origin[key] = "ai"
    except Exception as exc:  # never lose the upload: keep the file and ask the user to fill it in
        flags.append(f"extraction_failed: {str(exc)[:120]}")

    category_id = classify.resolve_category(paths, rec["category_label"])
    if category_id:
        origin["category_id"] = "ai"
    rule = classify.find_rule(conn, rec["merchant"])
    if rule:
        if rule["category_id"]:
            category_id = rule["category_id"]
            origin["category_id"] = "rule"
            conf["category"] = 1.0
        if rule["kind"] in KINDS:
            rec["kind"] = rule["kind"]
            origin["kind"] = "rule"
    if category_id is None and "extraction_failed" not in " ".join(flags):
        flags.append("category_unknown")

    flags += validate(rec, conf, settings.confidence_threshold)
    flags += _duplicate_flags(conn, rec, sha)

    txn_id = uuid.uuid4().hex[:12]
    stored = vault.save(data if data is not None else (text or "").encode())
    ts = now()
    conn.execute(
        """INSERT INTO transactions (id, amount_paise, currency, date, merchant, category_id, kind,
            payment_method, tax_amount_paise, gstin, line_items, source, receipt_file, receipt_name,
            receipt_type, receipt_sha256, field_origin, confidence, flags, status, notes, tags,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (txn_id, rec["amount_paise"], rec["currency"], rec["date"], rec["merchant"], category_id, rec["kind"],
         rec["payment_method"], rec["tax_amount_paise"], rec["gstin"], json.dumps(rec["line_items"]), source,
         stored, filename[:200], media_type or "text/plain", sha, json.dumps(origin), json.dumps(conf),
         json.dumps(flags), "needs_review" if flags else "accepted", rec["notes"], "[]", ts, ts),
    )
    conn.commit()
    return get_txn(conn, txn_id)


def _duplicate_flags(conn: sqlite3.Connection, rec: dict, sha: str) -> list[str]:
    flags = []
    if conn.execute("SELECT 1 FROM transactions WHERE receipt_sha256 = ? LIMIT 1", (sha,)).fetchone():
        flags.append("duplicate_file")
    if rec.get("amount_paise") and rec.get("date") and rec.get("merchant"):
        key = classify.merchant_key(rec["merchant"])
        rows = conn.execute(
            "SELECT merchant FROM transactions WHERE date = ? AND amount_paise = ?",
            (rec["date"], rec["amount_paise"]),
        ).fetchall()
        if any(classify.merchant_key(r["merchant"]) == key for r in rows):
            flags.append("possible_duplicate")
    return flags


# ------------------------------------------------------------------ manual entry and edits

def create_manual(conn: sqlite3.Connection, body: dict) -> dict:
    amount = to_paise(body.get("amount"))
    if amount is None or amount <= 0:
        raise ValueError("amount must be a positive number of rupees")
    d = parse_date(body.get("date"))
    if d is None:
        raise ValueError("date must be YYYY-MM-DD")
    merchant = classify.clean_merchant(body.get("merchant"))
    if not merchant:
        raise ValueError("merchant is required")
    paths = db.category_paths(conn)
    category_id = body.get("category_id")
    if category_id is not None and category_id not in paths:
        raise ValueError("unknown category")
    rule = classify.find_rule(conn, merchant)
    if category_id is None and rule and rule["category_id"]:
        category_id = rule["category_id"]
    kind = body.get("kind") if body.get("kind") in KINDS else (rule["kind"] if rule and rule["kind"] in KINDS else "personal")
    payment = body.get("payment_method") if body.get("payment_method") in PAYMENT_METHODS else "cash"
    txn_id = uuid.uuid4().hex[:12]
    ts = now()
    origin = {k: "manual" for k in ("merchant", "date", "amount_paise", "kind", "payment_method")}
    conn.execute(
        """INSERT INTO transactions (id, amount_paise, currency, date, merchant, category_id, kind, payment_method,
            source, field_origin, status, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (txn_id, amount, "INR", d.isoformat(), merchant, category_id, kind, payment, "manual",
         json.dumps(origin), "reviewed", str(body.get("notes") or "")[:500], ts, ts),
    )
    conn.commit()
    return get_txn(conn, txn_id)


def update_txn(conn: sqlite3.Connection, txn_id: str, changes: dict, make_rule: bool = False) -> dict:
    """Apply user edits, log each change as a correction, and mark the record reviewed."""
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    paths = db.category_paths(conn)
    origin = json.loads(row["field_origin"])
    sets: dict = {}

    if "amount" in changes:
        changes["amount_paise"] = to_paise(changes.pop("amount"))
    if "tax" in changes:
        changes["tax_amount_paise"] = to_paise(changes.pop("tax"))

    for field, value in changes.items():
        if field not in EDITABLE:
            continue
        if field == "category_id" and value is not None and value not in paths:
            raise ValueError("unknown category")
        if field == "kind" and value not in KINDS:
            raise ValueError("kind must be business or personal")
        if field == "payment_method" and value not in PAYMENT_METHODS:
            raise ValueError("payment_method must be upi, card, cash or other")
        if field == "date" and value and parse_date(value) is None:
            raise ValueError("date must be YYYY-MM-DD")
        if field == "amount_paise" and value is not None and value <= 0:
            raise ValueError("amount must be positive")
        if field == "merchant":
            value = classify.clean_merchant(value)
        if field == "gstin":
            value = value.strip().upper() if isinstance(value, str) and value.strip() else None
        if field in ("tags", "line_items"):
            sets[field] = json.dumps(value or [])
            continue
        if value != row[field]:
            conn.execute(
                "INSERT INTO corrections (txn_id, field, old_value, new_value, merchant, created_at) VALUES (?,?,?,?,?,?)",
                (txn_id, field, None if row[field] is None else str(row[field]),
                 None if value is None else str(value), changes.get("merchant") or row["merchant"], now()),
            )
            origin[field] = "manual"
        sets[field] = value

    sets["field_origin"] = json.dumps(origin)
    sets["status"] = "reviewed"
    sets["flags"] = "[]"
    sets["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE transactions SET {cols} WHERE id = ?", (*sets.values(), txn_id))

    if make_rule:
        merchant = sets.get("merchant", row["merchant"])
        key = classify.merchant_key(merchant)
        category_id = sets.get("category_id", row["category_id"])
        kind = sets.get("kind", row["kind"])
        if key and (category_id or kind):
            conn.execute(
                """INSERT INTO merchant_rules (pattern, category_id, kind, created_at) VALUES (?,?,?,?)
                   ON CONFLICT(pattern) DO UPDATE SET category_id = excluded.category_id, kind = excluded.kind""",
                (key, category_id, kind, now()),
            )
    conn.commit()
    return get_txn(conn, txn_id)


def approve(conn: sqlite3.Connection, txn_id: str) -> dict:
    """Accept a record as it stands."""
    if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn_id,)).fetchone() is None:
        raise KeyError(txn_id)
    conn.execute("UPDATE transactions SET status = 'reviewed', flags = '[]', updated_at = ? WHERE id = ?",
                 (now(), txn_id))
    conn.commit()
    return get_txn(conn, txn_id)


def delete_txn(conn: sqlite3.Connection, vault: Vault, txn_id: str) -> bool:
    row = conn.execute("SELECT receipt_file FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        return False
    if row["receipt_file"]:
        vault.delete(row["receipt_file"])
    conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    conn.execute("DELETE FROM corrections WHERE txn_id = ?", (txn_id,))
    conn.commit()
    return True


def wipe_all(conn: sqlite3.Connection, vault: Vault) -> int:
    files = [r["receipt_file"] for r in conn.execute("SELECT receipt_file FROM transactions") if r["receipt_file"]]
    for name in files:
        vault.delete(name)
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM corrections")
    conn.commit()
    return n


# ------------------------------------------------------------------ queries

def list_txns(conn: sqlite3.Connection, month: str | None = None, status: str | None = None,
              q: str | None = None, category_id: int | None = None, kind: str | None = None) -> list[dict]:
    where, args = [], []
    if month:
        where.append("substr(date, 1, 7) = ?")
        args.append(month)
    if status:
        where.append("status = ?")
        args.append(status)
    if category_id:
        where.append("category_id = ?")
        args.append(category_id)
    if kind in KINDS:
        where.append("kind = ?")
        args.append(kind)
    if q:
        where.append("(merchant LIKE ? OR notes LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    sql = "SELECT * FROM transactions" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY COALESCE(date, '0000') DESC, created_at DESC LIMIT 1000"
    paths = db.category_paths(conn)
    return [row_to_txn(r, paths) for r in conn.execute(sql, args)]


def summary(conn: sqlite3.Connection, month: str) -> dict:
    paths = db.category_paths(conn)
    rows = conn.execute(
        "SELECT * FROM transactions WHERE substr(date, 1, 7) = ? AND currency = 'INR'", (month,)
    ).fetchall()
    total = sum(r["amount_paise"] or 0 for r in rows)
    by_cat: dict[str, dict] = {}
    by_kind = {"business": 0, "personal": 0}
    merchants: dict[str, int] = {}
    for r in rows:
        amt = r["amount_paise"] or 0
        path = paths.get(r["category_id"], "Uncategorised") if r["category_id"] else "Uncategorised"
        parent, _, child = path.partition(" > ")
        node = by_cat.setdefault(parent, {"category": parent, "total_paise": 0, "children": {}})
        node["total_paise"] += amt
        if child:
            node["children"][child] = node["children"].get(child, 0) + amt
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + amt
        if r["merchant"]:
            merchants[r["merchant"]] = merchants.get(r["merchant"], 0) + amt
    categories = sorted(by_cat.values(), key=lambda n: -n["total_paise"])
    for n in categories:
        n["children"] = [{"category": k, "total_paise": v}
                         for k, v in sorted(n["children"].items(), key=lambda kv: -kv[1])]
    y, m = int(month[:4]), int(month[5:7])
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    prev_total = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM transactions WHERE substr(date, 1, 7) = ? AND currency = 'INR'",
        (prev,),
    ).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM transactions WHERE status = 'needs_review'").fetchone()[0]
    return {
        "month": month,
        "total_paise": total,
        "count": len(rows),
        "previous_month": prev,
        "previous_total_paise": prev_total,
        "by_category": categories,
        "by_kind": by_kind,
        "top_merchants": [{"merchant": k, "total_paise": v}
                          for k, v in sorted(merchants.items(), key=lambda kv: -kv[1])[:5]],
        "needs_review_total": pending,
    }


# ------------------------------------------------------------------ exports

def _csv_safe(value) -> str:
    """Stop spreadsheet formula injection: receipt text is untrusted and ends up in merchant names."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv(conn: sqlite3.Connection, month: str | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "merchant", "amount_inr", "currency", "category", "kind", "payment_method",
                "tax_inr", "gstin", "source", "status", "notes", "id"])
    for t in list_txns(conn, month=month):
        w.writerow([
            t["date"] or "", _csv_safe(t["merchant"]),
            f"{(t['amount_paise'] or 0) / 100:.2f}" if t["amount_paise"] is not None else "",
            t["currency"], _csv_safe(t.get("category")), t["kind"], t["payment_method"],
            f"{t['tax_amount_paise'] / 100:.2f}" if t["tax_amount_paise"] is not None else "",
            t["gstin"] or "", t["source"], t["status"], _csv_safe(t["notes"]), t["id"],
        ])
    return buf.getvalue()


def export_all(conn: sqlite3.Connection) -> dict:
    """Everything the app holds about you, as JSON (your data-access right, and a backup)."""
    paths = db.category_paths(conn)
    return {
        "exported_at": now(),
        "transactions": [row_to_txn(r, paths) for r in conn.execute("SELECT * FROM transactions ORDER BY created_at")],
        "categories": [dict(r) for r in conn.execute("SELECT * FROM categories")],
        "merchant_rules": [dict(r) for r in conn.execute("SELECT * FROM merchant_rules")],
        "corrections": [dict(r) for r in conn.execute("SELECT * FROM corrections")],
        "note": "Original receipt files are not included here; download them individually from each transaction.",
    }
