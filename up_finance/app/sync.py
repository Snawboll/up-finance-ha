"""Pull data from Up into SQLite.

Strategy
- First run: download the full transaction history.
- Later runs: re-fetch a recent window (default 14 days, 90 days once a day) and
  upsert it. Re-fetching a window picks up HELD -> SETTLED changes and category
  edits; any transaction inside the window that Up no longer returns was
  cancelled, so it is deleted.
- Accounts and categories are refreshed every run; a balance snapshot is kept
  per account per day.
- Scope: with account_scope="joint" only 2Up (JOINT) accounts are fetched and stored.
  Individual accounts are never downloaded, and anything stored from them earlier
  is deleted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .covers import link_covers
from .db import Database
from .rules import apply_rules
from .up_client import UpClient

log = logging.getLogger(__name__)

SHORT_WINDOW_DAYS = 14
LONG_WINDOW_DAYS = 90


def _cents(money: Optional[dict]) -> Optional[int]:
    return int(money["valueInBaseUnits"]) if money else None


def _rel_id(rel: dict, name: str) -> Optional[str]:
    data = (rel.get(name) or {}).get("data")
    return data["id"] if isinstance(data, dict) else None


def parse_transaction(raw: dict, tz: ZoneInfo) -> tuple[dict, list[str]]:
    a = raw["attributes"]
    rel = raw.get("relationships", {})
    created = datetime.fromisoformat(a["createdAt"].replace("Z", "+00:00"))
    local = created.astimezone(tz)
    foreign = a.get("foreignAmount")
    row = {
        "id": raw["id"],
        "account_id": _rel_id(rel, "account"),
        "status": a.get("status"),
        "description": a.get("description"),
        "raw_text": a.get("rawText"),
        "message": a.get("message"),
        "amount_cents": _cents(a["amount"]),
        "currency": a["amount"].get("currencyCode"),
        "foreign_amount_cents": _cents(foreign),
        "foreign_currency": foreign.get("currencyCode") if foreign else None,
        "round_up_cents": _cents((a.get("roundUp") or {}).get("amount")),
        "cashback_cents": _cents((a.get("cashback") or {}).get("amount")),
        "category_id": _rel_id(rel, "category"),
        "parent_category_id": _rel_id(rel, "parentCategory"),
        "transfer_account_id": _rel_id(rel, "transferAccount"),
        "created_at": a["createdAt"],
        "settled_at": a.get("settledAt"),
        "local_date": local.strftime("%Y-%m-%d"),
        "month": local.strftime("%Y-%m"),
        "card_method": (a.get("cardPurchaseMethod") or {}).get("method"),
        "txn_type": a.get("transactionType"),
        "note": (a.get("note") or {}).get("text"),
    }
    tags = [t["id"] for t in ((rel.get("tags") or {}).get("data") or [])]
    return row, tags


TXN_COLUMNS = [
    "id", "account_id", "status", "description", "raw_text", "message", "amount_cents",
    "currency", "foreign_amount_cents", "foreign_currency", "round_up_cents",
    "cashback_cents", "category_id", "parent_category_id", "transfer_account_id",
    "created_at", "settled_at", "local_date", "month", "card_method", "txn_type", "note",
]
_UPSERT_TXN = (
    f"INSERT INTO transactions ({', '.join(TXN_COLUMNS)}, updated_at) "
    f"VALUES ({', '.join('?' * len(TXN_COLUMNS))}, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in TXN_COLUMNS[1:])
    + ", updated_at=excluded.updated_at"
)


class Syncer:
    def __init__(self, db: Database, client: UpClient, tz: ZoneInfo, scope: str = "all"):
        if scope not in ("all", "joint"):
            raise ValueError(f"account_scope must be 'all' or 'joint', not {scope!r}")
        self.db = db
        self.client = client
        self.tz = tz
        self.scope = scope

    def in_scope(self, account: dict) -> bool:
        return self.scope == "all" or account["attributes"].get("ownershipType") == "JOINT"

    def sync(self, full: bool = False) -> dict:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat(timespec="seconds")
        today = now.astimezone(self.tz).strftime("%Y-%m-%d")

        accounts = [a for a in self.client.accounts() if self.in_scope(a)]
        categories = self.client.categories()
        if not accounts:
            raise RuntimeError(f"No Up accounts match account_scope={self.scope!r}")

        # A scope change invalidates the history window: do a full sync.
        if self.db.get_state("account_scope") != self.scope:
            full = True
        has_history = self.db.get_state("full_sync_at") is not None
        last_long = self.db.get_state("long_window_date")
        since: Optional[datetime] = None
        if has_history and not full:
            days = LONG_WINDOW_DAYS if last_long != today else SHORT_WINDOW_DAYS
            since = now - timedelta(days=days)

        since_param = since.isoformat(timespec="seconds") if since else None
        rows: list[tuple[dict, list[str]]] = []
        if self.scope == "all":
            rows = [parse_transaction(t, self.tz) for t in self.client.transactions(since_param)]
        else:
            for acc in accounts:
                rows += [parse_transaction(t, self.tz)
                         for t in self.client.transactions(since_param, account_id=acc["id"])]
        scope_ids = [a["id"] for a in accounts]

        with self.db.writer() as conn:
            # Remove anything stored from accounts that are no longer in scope.
            marks = ",".join("?" * len(scope_ids))
            conn.execute(f"DELETE FROM transaction_tags WHERE transaction_id IN "
                         f"(SELECT id FROM transactions WHERE account_id NOT IN ({marks}))", scope_ids)
            conn.execute(f"DELETE FROM transactions WHERE account_id NOT IN ({marks})", scope_ids)
            conn.execute(f"DELETE FROM balance_snapshots WHERE account_id NOT IN ({marks})", scope_ids)
            conn.execute(f"DELETE FROM accounts WHERE id NOT IN ({marks})", scope_ids)

            for acc in accounts:
                a = acc["attributes"]
                bal = _cents(a["balance"])
                conn.execute(
                    "INSERT INTO accounts(id, name, type, ownership, balance_cents, currency, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "type=excluded.type, ownership=excluded.ownership, balance_cents=excluded.balance_cents, "
                    "currency=excluded.currency, updated_at=excluded.updated_at",
                    (acc["id"], a["displayName"], a.get("accountType"), a.get("ownershipType"),
                     bal, a["balance"].get("currencyCode"), a.get("createdAt"), now_iso),
                )
                conn.execute(
                    "INSERT INTO balance_snapshots(date, account_id, balance_cents) VALUES (?,?,?) "
                    "ON CONFLICT(date, account_id) DO UPDATE SET balance_cents=excluded.balance_cents",
                    (today, acc["id"], bal),
                )

            for cat in categories:
                parent = (((cat.get("relationships") or {}).get("parent") or {}).get("data") or {})
                conn.execute(
                    "INSERT INTO categories(id, name, parent_id) VALUES (?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, parent_id=excluded.parent_id",
                    (cat["id"], cat["attributes"]["name"], parent.get("id")),
                )

            seen = set()
            for row, tags in rows:
                seen.add(row["id"])
                conn.execute(_UPSERT_TXN, [row[c] for c in TXN_COLUMNS] + [now_iso])
                conn.execute("DELETE FROM transaction_tags WHERE transaction_id=?", (row["id"],))
                conn.executemany(
                    "INSERT OR IGNORE INTO transaction_tags(transaction_id, tag) VALUES (?,?)",
                    [(row["id"], t) for t in tags],
                )

            # Anything in the fetched window that Up no longer returns was removed.
            if since is not None:
                stale = conn.execute(
                    "SELECT id, created_at FROM transactions WHERE created_at >= ?",
                    # created_at strings carry mixed UTC offsets, so pre-filter
                    # with a day of margin and compare exactly below.
                    ((since - timedelta(days=1)).astimezone(self.tz).isoformat(timespec="seconds"),),
                ).fetchall()
            else:
                stale = conn.execute("SELECT id, created_at FROM transactions").fetchall()
            removed = 0
            for r in stale:
                if r["id"] in seen:
                    continue
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                if since is None or created >= since:
                    conn.execute("DELETE FROM transactions WHERE id=?", (r["id"],))
                    conn.execute("DELETE FROM transaction_tags WHERE transaction_id=?", (r["id"],))
                    removed += 1

            cover_stats = link_covers(conn)
            apply_rules(conn, self.db.rules)
            self.db.set_state(conn, "account_scope", self.scope)
            if since is None:
                self.db.set_state(conn, "full_sync_at", now_iso)
                self.db.set_state(conn, "long_window_date", today)
            elif (now - since).days >= LONG_WINDOW_DAYS:
                self.db.set_state(conn, "long_window_date", today)
            self.db.set_state(conn, "last_sync_at", now_iso)

        result = {
            "mode": "full" if since is None else f"{(now - since).days}d window",
            "accounts": len(accounts),
            "transactions_fetched": len(rows),
            "removed": removed,
            "covers_matched": f"{cover_stats['matched']}/{cover_stats['covers']}",
            "synced_at": now_iso,
        }
        log.info("Sync complete: %s", result)
        return result
