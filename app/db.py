"""SQLite schema, connection helper and default categories."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE
);
-- SQLite treats NULLs as distinct in UNIQUE constraints, so top-level names need an expression index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_unique ON categories(name, COALESCE(parent_id, 0));

CREATE TABLE IF NOT EXISTS merchant_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE,          -- lowercase alphanumerics, matched as a substring of the merchant key
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    kind TEXT,                             -- business | personal | NULL (leave to the model)
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    amount_paise INTEGER,
    currency TEXT NOT NULL DEFAULT 'INR',
    date TEXT,
    merchant TEXT,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'personal',
    payment_method TEXT NOT NULL DEFAULT 'other',
    tax_amount_paise INTEGER,
    gstin TEXT,
    line_items TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    receipt_file TEXT,
    receipt_name TEXT,
    receipt_type TEXT,
    receipt_sha256 TEXT,
    context TEXT,                          -- email headers passed to the model with an attachment
    field_origin TEXT NOT NULL DEFAULT '{}',
    confidence TEXT NOT NULL DEFAULT '{}',
    flags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,                  -- processing | accepted | needs_review | reviewed
    notes TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_txn_sha ON transactions(receipt_sha256);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    merchant TEXT,
    created_at TEXT NOT NULL
);
"""

DEFAULT_CATEGORIES = {
    "Food": ["Restaurants", "Groceries", "Food delivery", "Coffee and snacks"],
    "Transport": ["Cab and auto", "Fuel", "Public transport", "Parking and tolls"],
    "Shopping": ["Clothing", "Electronics", "Home", "Personal care"],
    "Bills and utilities": ["Electricity", "Mobile and internet", "Rent", "Subscriptions"],
    "Health": ["Doctor", "Pharmacy", "Fitness"],
    "Travel": ["Flights and trains", "Hotels", "Activities"],
    "Entertainment": [],
    "Office and business": ["Software", "Equipment", "Client expenses"],
    "Other": [],
}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")   # the web server, worker and mail poller write from different threads
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        if "context" not in cols:  # databases created before background processing
            conn.execute("ALTER TABLE transactions ADD COLUMN context TEXT")
        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            for parent, children in DEFAULT_CATEGORIES.items():
                cur = conn.execute("INSERT INTO categories(name, parent_id) VALUES (?, NULL)", (parent,))
                for child in children:
                    conn.execute(
                        "INSERT INTO categories(name, parent_id) VALUES (?, ?)", (child, cur.lastrowid)
                    )
        conn.commit()
    finally:
        conn.close()


def category_paths(conn: sqlite3.Connection) -> dict[int, str]:
    """Map category id to 'Parent > Child' (or just 'Parent')."""
    rows = conn.execute("SELECT id, name, parent_id FROM categories").fetchall()
    names = {r["id"]: r for r in rows}
    out: dict[int, str] = {}
    for r in rows:
        parent = names.get(r["parent_id"]) if r["parent_id"] else None
        out[r["id"]] = f"{parent['name']} > {r['name']}" if parent else r["name"]
    return out
