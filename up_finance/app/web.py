"""HTTP servers: the dashboard (via HA ingress) and the read-only data API (LAN).

Two listeners share one handler:
- ingress  (port 8099, internal): only accepts the Supervisor ingress proxy
  (172.30.32.2). No auth needed - Home Assistant already authenticated the user.
- lan      (port 8098, exposed): every request needs `Authorization: Bearer <api_key>`.
  Adds /api/query (read-only SQL) and /api/export.csv for ad-hoc analysis.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .db import Database
from .metrics import Metrics
from .rules import parse_rules, preview_rules

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
INGRESS_PROXY = "172.30.32.2"
MAX_ROWS = 5000
QUERY_TIMEOUT_S = 10

_ALLOWED_SQL_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def run_readonly_query(db: Database, sql: str, params: Optional[list] = None) -> dict:
    """Run a single SELECT against a read-only connection, with a time and row cap."""
    conn = db.connect_readonly()
    try:
        conn.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_OK if action in _ALLOWED_SQL_ACTIONS else sqlite3.SQLITE_DENY)
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        cur = conn.execute(sql, params or [])
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        return {"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": truncated}
    finally:
        conn.close()


def schema_description(db: Database) -> dict:
    conn = db.connect_readonly()
    try:
        objs = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        return {"objects": [dict(o) for o in objs]}
    finally:
        conn.close()


class App:
    """Shared state for the handlers."""

    def __init__(self, db: Database, metrics: Metrics, api_key: str = "",
                 trigger_sync: Optional[Callable[[], dict]] = None):
        self.db = db
        self.metrics = metrics
        self.api_key = api_key or ""
        self.trigger_sync = trigger_sync


class Handler(BaseHTTPRequestHandler):
    server_version = "up-finance/1"
    app: App
    mode: str  # "ingress" | "lan" | "dev"

    def log_message(self, fmt, *args):  # quieter logs
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, code: int, msg: str):
        self._json({"error": msg}, code)

    def _authorised(self) -> bool:
        if self.mode == "dev":
            return True
        if self.mode == "ingress":
            return self.client_address[0] in (INGRESS_PROXY, "127.0.0.1")
        # lan
        if len(self.app.api_key) < 16:
            return False
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth.encode(), f"Bearer {self.app.api_key}".encode())

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        if not self._authorised():
            return self._error(401 if self.mode == "lan" else 403, "Not authorised")
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        m = self.app.metrics
        try:
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            routes = {
                "/api/overview": lambda: m.overview(),
                "/api/monthly": lambda: m.monthly(int(q.get("months", 13))),
                "/api/categories": lambda: m.categories(q.get("month") or None),
                "/api/budgets": lambda: m.budgets_status(),
                "/api/recurring": lambda: m.recurring(),
                "/api/savers": lambda: m.savers(int(q.get("months", 6))),
                "/api/merchants": lambda: m.merchants(int(q.get("days", 90)), int(q.get("limit", 25))),
                "/api/large": lambda: m.large_transactions(int(q.get("days", 30)), int(q.get("limit", 15))),
                "/api/new_merchants": lambda: m.new_merchants(int(q.get("days", 30))),
                "/api/balances": lambda: m.balance_history(int(q.get("days", 365))),
                "/api/transactions": lambda: m.transactions(
                    q.get("q", ""), q.get("month", ""), q.get("category", ""), q.get("kind", ""),
                    int(q.get("limit", 200)), int(q.get("offset", 0))),
                "/api/category_list": lambda: [
                    {"id": k, "name": v} for k, v in sorted(m.category_names().items(), key=lambda x: x[1])],
                "/api/report": lambda: m.report(),
                "/api/rules": lambda: self._rules(),
                "/api/uncategorised": lambda: self._uncategorised(int(q.get("days", 365)), int(q.get("limit", 30))),
                "/api/health": lambda: {"ok": True, "last_sync_at": self.app.db.get_state("last_sync_at"),
                                        "last_error": self.app.db.get_state("last_error")},
            }
            if self.mode != "ingress":
                routes["/api/schema"] = lambda: schema_description(self.app.db)
            if path in routes:
                return self._json(routes[path]())
            if path == "/api/export.csv" and self.mode != "ingress":
                return self._export_csv()
            return self._error(404, "Not found")
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:  # pragma: no cover
            log.exception("Request failed")
            return self._error(500, str(e))

    def do_POST(self):
        if not self._authorised():
            return self._error(401 if self.mode == "lan" else 403, "Not authorised")
        path = urlparse(self.path).path.rstrip("/")
        length = min(int(self.headers.get("Content-Length") or 0), 100_000)
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._error(400, "Body must be JSON")
        if path == "/api/sync":
            if not self.app.trigger_sync:
                return self._error(503, "Sync not available")
            try:
                return self._json(self.app.trigger_sync())
            except Exception as e:
                return self._error(502, str(e))
        if path == "/api/rules/preview":
            conn = self.app.db.connect_readonly()
            try:
                rules = parse_rules(data.get("payee_rules"), data.get("ignore_payees"))
                return self._json({"rules": preview_rules(conn, rules)})
            finally:
                conn.close()
        if path == "/api/query" and self.mode != "ingress":
            sql = (data.get("sql") or "").strip()
            if not sql:
                return self._error(400, "Provide {\"sql\": \"SELECT ...\"}")
            try:
                return self._json(run_readonly_query(self.app.db, sql, data.get("params")))
            except sqlite3.Error as e:
                return self._error(400, f"SQL error: {e}")
        return self._error(404, "Not found")

    def _rules(self) -> dict:
        """The rules currently in force (from the add-on options) and what each matches."""
        conn = self.app.db.connect_readonly()
        try:
            return {"rules": preview_rules(conn, self.app.db.rules),
                    "how_to_change": "Edit payee_rules / ignore_payees in the add-on options, then restart it. "
                                     "POST /api/rules/preview with {payee_rules, ignore_payees} to test first."}
        finally:
            conn.close()

    def _uncategorised(self, days: int, limit: int) -> list[dict]:
        """Biggest uncategorised payees: the best candidates for new rules."""
        return [dict(r) for r in self._ro(
            """SELECT description AS payee, COUNT(*) AS count, ROUND(-SUM(amount_cents)/100.0, 2) AS spent,
                      MIN(local_date) AS first, MAX(local_date) AS last,
                      SUBSTR(GROUP_CONCAT(DISTINCT message), 1, 300) AS messages
               FROM flows WHERE kind='spending' AND category='uncategorised'
               AND local_date > date('now', ?) GROUP BY description ORDER BY spent DESC LIMIT ?""",
            (f"-{max(1, days)} day", max(1, min(limit, 200))))]

    def _ro(self, sql: str, params=()) -> list:
        conn = self.app.db.connect_readonly()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _export_csv(self):
        conn = self.app.db.connect_readonly()
        try:
            cur = conn.execute(
                "SELECT local_date, created_at, account_name, description, amount_cents/100.0 AS amount, "
                "kind, category, parent_category, status, message, note, raw_text, id "
                "FROM flows ORDER BY created_at")
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([c[0] for c in cur.description])
            w.writerows(cur.fetchall())
        finally:
            conn.close()
        self._send(200, buf.getvalue().encode(), "text/csv; charset=utf-8",
                   {"Content-Disposition": "attachment; filename=up-transactions.csv"})


def serve(app: App, port: int, mode: str, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type(f"{mode.title()}Handler", (Handler,), {"app": app, "mode": mode})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name=f"http-{mode}", daemon=True).start()
    log.info("%s server listening on %s:%d", mode, host, port)
    return httpd
