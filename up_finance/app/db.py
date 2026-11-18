"""SQLite storage for Up data.

Amounts are stored as integer cents. `local_date` / `month` are computed in the
configured time zone at ingest so SQL grouping is simple.

The `flows` view classifies every transaction:
  transfer  - moves between the synced accounts (round-ups, saver top-ups, covers)
  allowance - money sent from a synced account to one of your Up accounts that is
              outside the sync scope (e.g. 2Up -> your individual account)
  spending  - categorised transactions (refunds reduce spending) and uncategorised
              outflows (BPAY, Pay Anyone, etc.)
  income    - uncategorised inflows (salary, interest, money from others)
  loan      - home loan drawdowns/adjustments (loan interest counts as spending)
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,              -- TRANSACTIONAL | SAVER | HOME_LOAN
    ownership TEXT,         -- INDIVIDUAL | JOINT
    balance_cents INTEGER,
    currency TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    date TEXT NOT NULL,
    account_id TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    PRIMARY KEY (date, account_id)
);

CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    status TEXT,                    -- HELD | SETTLED
    description TEXT,               -- cleaned merchant / payee name
    raw_text TEXT,
    message TEXT,
    amount_cents INTEGER NOT NULL,  -- negative = money out
    currency TEXT,
    foreign_amount_cents INTEGER,
    foreign_currency TEXT,
    round_up_cents INTEGER,
    cashback_cents INTEGER,
    category_id TEXT,
    parent_category_id TEXT,
    transfer_account_id TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    local_date TEXT NOT NULL,       -- YYYY-MM-DD in local time
    month TEXT NOT NULL,            -- YYYY-MM in local time
    card_method TEXT,
    txn_type TEXT,
    note TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(local_date);
CREATE INDEX IF NOT EXISTS idx_txn_month ON transactions(month);
CREATE INDEX IF NOT EXISTS idx_txn_desc ON transactions(description);

CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (transaction_id, tag)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Which saver paid for which purchase. Up doesn't link a "Cover from <saver>"
-- transfer to the purchase it covered, so this is inferred after each sync
-- (same account, same amount, close in time). spend_id is NULL when no match.
CREATE TABLE IF NOT EXISTS cover_links (
    cover_id TEXT PRIMARY KEY,
    spend_id TEXT,
    saver_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    match_type TEXT              -- auto | manual | NULL
);
CREATE INDEX IF NOT EXISTS idx_cover_spend ON cover_links(spend_id);

-- Payees that are really your own accounts at other banks: treated as transfers.
CREATE TABLE IF NOT EXISTS ignored_payees (
    description TEXT PRIMARY KEY
);

DROP VIEW IF EXISTS flows;
CREATE VIEW flows AS
SELECT
    t.*,
    CASE
        WHEN t.transfer_account_id IN (SELECT id FROM accounts) THEN 'transfer'
        WHEN t.transfer_account_id IS NOT NULL THEN 'allowance'
        WHEN t.description IN (SELECT description FROM ignored_payees) THEN 'transfer'
        WHEN a.type = 'HOME_LOAN' AND t.amount_cents < 0 AND t.description LIKE '%Interest%' THEN 'spending'
        WHEN a.type = 'HOME_LOAN' THEN 'loan'   -- drawdowns, adjustments
        WHEN t.category_id IS NOT NULL THEN 'spending'
        WHEN t.amount_cents < 0 THEN 'spending'
        ELSE 'income'
    END AS kind,
    CASE WHEN a.type = 'HOME_LOAN' THEN 'home-loan-interest'
         ELSE COALESCE(t.category_id, 'uncategorised') END AS category,
    CASE WHEN a.type = 'HOME_LOAN' THEN 'home'
         ELSE COALESCE(t.parent_category_id, 'uncategorised') END AS parent_category,
    a.name AS account_name,
    a.type AS account_type,
    cl.saver_id AS covered_by_id,
    sv.name AS covered_by
FROM transactions t
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN cover_links cl ON cl.spend_id = t.id
LEFT JOIN accounts sv ON sv.id = cl.saver_id;
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def connect_readonly(self) -> sqlite3.Connection:
        uri = f"file:{self.path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def writer(self):
        """Context manager: serialise writes and commit on success."""
        db = self

        class _W:
            def __enter__(self_inner):
                db._write_lock.acquire()
                self_inner.conn = db.connect()
                return self_inner.conn

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self_inner.conn.commit()
                    else:
                        self_inner.conn.rollback()
                finally:
                    self_inner.conn.close()
                    db._write_lock.release()

        return _W()

    def set_ignored_payees(self, payees: list[str]) -> None:
        with self.writer() as conn:
            conn.execute("DELETE FROM ignored_payees")
            conn.executemany("INSERT OR IGNORE INTO ignored_payees(description) VALUES (?)",
                             [(p.strip(),) for p in payees if p and p.strip()])

    # -- sync state --------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO sync_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
