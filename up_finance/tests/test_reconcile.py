"""Run with:  cd up_finance && python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.covers import link_covers  # noqa: E402
from app.db import Database  # noqa: E402
from app.ha import build_sensors  # noqa: E402
from app.main import check_button  # noqa: E402
from app.metrics import Metrics  # noqa: E402
from app.reconcile import Reconciler, subset_sum  # noqa: E402
from app.web import App, serve  # noqa: E402

TZ = ZoneInfo("Australia/Sydney")
TODAY = date(2026, 11, 22)
SP, SAV, BILLS, DE, KLONG = "sp", "sav", "bills", "de", "klong"  # KLONG: personal account, not synced


def build(tmp: str) -> Database:
    """A household that keeps $1,000 in 2Up Spending, with the usual things slipping through."""
    db = Database(Path(tmp) / "r.db")
    rows: list[tuple] = []

    def t(day: str, desc: str, dollars: float, hhmm: str = "12:00", account: str = SP, transfer=None,
          category=None):
        when = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=TZ)
        rows.append((f"t{len(rows):04d}", account, "SETTLED", desc, int(round(dollars * 100)), category,
                     transfer, when.isoformat(), day, day[:7]))

    t("2026-08-01", "Transfer from Savings", 1000, transfer=SAV)  # the float
    t("2026-09-15", "Brindabella GP", -110, "10:00", category="health-and-medical")
    t("2026-09-15", "Cover from Savings", 110, "10:00", transfer=SAV)
    cycle = date(2026, 9, 10)
    while cycle <= TODAY:
        d = cycle.isoformat()
        t(d, "Auto Transfer from Savings", 5100, "07:00", transfer=SAV)
        t(d, "Auto Transfer to Bills", -3150, "07:01", transfer=BILLS)
        t(d, "Auto Transfer to Daily Expenses", -1800, "07:02", transfer=DE)
        if cycle < date(2026, 11, 5):  # stops running from the 5 Nov cycle
            t(d, "Auto Transfer to $klongwinter", -150, "07:03", transfer=KLONG)
        cycle += timedelta(days=14)
    t("2026-10-23", "Cover from Daily Expenses", 54.97, "13:22", transfer=DE)  # pre-cover, Costco pending
    # --- since the last time the account was on its float (end of 24 Oct) ---
    t("2026-10-26", "Transfer from Bills", 2366, "13:49", transfer=BILLS)  # paid the rates, 60c short
    t("2026-10-26", "QPRC", -2366.60, "13:51")
    t("2026-10-26", "Transfer from Savings", 291.32, "14:00", transfer=SAV)  # top-up for the second bill
    t("2026-10-26", "QPRC", -291.32, "14:02")
    t("2026-10-26", "Queanbeyan Dental", -530, "11:00", category="health-and-medical")
    t("2026-10-27", "Woolworths", -100, "15:00", category="groceries")
    t("2026-10-27", "Cover from Daily Expenses", 100, "15:00", transfer=DE)
    t("2026-10-28", "Cover from Bills", 13.99, "06:00", transfer=BILLS)  # double cover
    t("2026-11-03", "Costco", -54.97, "03:37", category="groceries")
    t("2026-11-04", "IT Lab Solutions", 9311.10, "12:19")
    t("2026-11-04", "Transfer to Savings", -8379.99, "12:20", transfer=SAV)
    t("2026-11-04", "Transfer to Bills", -931.11, "12:20", transfer=BILLS)
    t("2026-11-10", "MCARE BENEFITS", 45.05, "00:12")
    t("2026-11-12", "Big W", -23.45, "13:17", category="homeware")
    t("2026-11-12", "Coles", -30, "13:30", category="groceries")
    t("2026-11-17", "Medicare", 108.95, "00:15", category="health-and-medical")
    t("2026-11-18", "Brindabella GP", -206, "13:13", category="health-and-medical")
    t("2026-11-20", "Cover from Savings", 53.45, "15:00", transfer=SAV)  # one cover for Big W + Coles
    t("2026-11-21", "Transfer to $klongwinter", -150, "09:00", transfer=KLONG)  # catches up one cycle

    balance = sum(r[4] for r in rows)
    with db.writer() as conn:
        conn.executemany("INSERT INTO accounts(id, name, type, ownership, balance_cents) VALUES (?,?,?,?,?)", [
            (SP, "2Up Spending", "TRANSACTIONAL", "JOINT", balance), (SAV, "💰 Savings", "SAVER", "JOINT", 0),
            (BILLS, "🧾 Bills", "SAVER", "JOINT", 0), (DE, "🛒 Daily Expenses", "SAVER", "JOINT", 0)])
        conn.executemany(
            "INSERT INTO transactions(id, account_id, status, description, amount_cents, category_id, "
            "transfer_account_id, created_at, local_date, month) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        link_covers(conn)
    return db


class SubsetSumTests(unittest.TestCase):
    def test_subset_sum(self):
        self.assertEqual(subset_sum(500, [("a", 200), ("b", 500)]), ["b"])
        self.assertEqual(sorted(subset_sum(5345, [("a", 2345), ("b", 999), ("c", 3000)])), ["a", "c"])
        self.assertIsNone(subset_sum(100, [("a", 30), ("b", 30)]))
        self.assertIsNone(subset_sum(0, [("a", 0)]))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = build(self.tmp.name)
        self.rec = Reconciler(self.db, TZ, 1000, "2up spending", reserve_saver="Savings", today=TODAY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_what_needs_doing(self):
        r = self.rec.report()
        self.assertEqual(r["since_source"], "auto")
        self.assertTrue(r["since"].startswith("2026-10-24T00:00"))  # end of 23 Oct, the last day on the float
        got = [(i["action"], i["payee"], i["amount"]) for i in r["items"]]
        self.assertEqual(got, [
            ("cover", "Queanbeyan Dental", 530.0),
            ("return", "Transfer from Bills", 2366.0),
            ("cover", "QPRC", 2366.6),
            ("return", "Cover from Bills", 13.99),
            ("forward", "MCARE BENEFITS", 45.05),
            ("forward", "Medicare", 108.95),
            ("cover", "Brindabella GP", 206.0),
            ("transfer", "Transfer to $klongwinter", 150.0),
        ])
        by_payee = {i["payee"]: i for i in r["items"]}
        self.assertEqual(by_payee["Brindabella GP"]["suggested"], "💰 Savings")  # covered from Savings before
        self.assertEqual(by_payee["MCARE BENEFITS"]["suggested"], "💰 Savings")  # the reserve
        self.assertEqual(by_payee["Cover from Bills"]["suggested"], "🧾 Bills")
        self.assertIn("19 Nov", by_payee["Transfer to $klongwinter"]["note"])
        self.assertEqual(r["totals"], {"cover": 3102.6, "forward": 154.0, "return": 2379.99,
                                       "transfer": 150.0})
        # doing every action puts the account exactly back on its float
        self.assertEqual(r["projected_balance"], 1000.0)
        self.assertEqual(r["off_by"], 0.0)
        self.assertEqual(r["gap"], round(1000 - r["balance"], 2))

    def test_mark_reconciled_clears_the_list(self):
        with self.db.writer() as conn:
            self.db.set_state(conn, "reconciled_at", "2026-11-22T10:00:00+11:00")
        r = self.rec.report()
        self.assertEqual((r["since_source"], r["count"], r["items"]), ("marked", 0, []))

    def test_late_cover_settles_a_purchase(self):
        with self.db.writer() as conn:
            when = datetime(2026, 11, 22, 9, 0, tzinfo=TZ).isoformat()
            conn.execute("INSERT INTO transactions(id, account_id, status, description, amount_cents, "
                         "transfer_account_id, created_at, local_date, month) VALUES "
                         "('late', ?, 'SETTLED', 'Cover from Savings', 53000, ?, ?, '2026-11-22', '2026-11')",
                         (SP, SAV, when))
            link_covers(conn)  # 27 days after the purchase: inside the 60-day manual window
        payees = [i["payee"] for i in self.rec.report()["items"]]
        self.assertNotIn("Queanbeyan Dental", payees)

    def test_sensors(self):
        s = build_sensors(Metrics(self.db, TZ, today=TODAY), self.rec)
        state, attrs = s["sensor.up_reconcile"]
        self.assertEqual(state, 8)
        self.assertEqual(attrs["items"][0]["payee"], "Queanbeyan Dental")
        self.assertEqual(s["sensor.up_float_gap"][0], round(1000 - attrs["balance"], 2))
        # no float configured: still lists items, no gap sensor
        s = build_sensors(Metrics(self.db, TZ, today=TODAY), Reconciler(self.db, TZ, 0, today=TODAY))
        self.assertIn("sensor.up_reconcile", s)
        self.assertNotIn("sensor.up_float_gap", s)

    def test_button_marks_only_on_a_new_press(self):
        class FakeHA:
            state = "2026-11-01T00:00:00+00:00"

            def get_state(self, _):
                return self.state

        ha = FakeHA()
        self.assertFalse(check_button(self.db, ha, "input_button.x"))  # first sight: just remember
        self.assertIsNone(self.db.get_state("reconciled_at"))
        self.assertFalse(check_button(self.db, ha, "input_button.x"))
        ha.state = "2026-11-22T01:00:00+00:00"
        self.assertTrue(check_button(self.db, ha, "input_button.x"))
        self.assertEqual(self.db.get_state("reconciled_at"), "2026-11-22T01:00:00+00:00")
        ha.state = "unavailable"
        self.assertFalse(check_button(self.db, ha, "input_button.x"))


class ReconcileWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = build(self.tmp.name)
        rec = Reconciler(self.db, TZ, 1000, today=TODAY)
        self.published = 0

        def bump():
            self.published += 1

        app = App(self.db, Metrics(self.db, TZ, today=TODAY), "k" * 24, reconciler=rec, on_reconciled=bump)
        self.ingress = serve(app, 0, "ingress", host="127.0.0.1")
        self.lan = serve(app, 0, "lan", host="127.0.0.1")

    def tearDown(self):
        self.ingress.shutdown()
        self.lan.shutdown()
        self.tmp.cleanup()

    def call(self, server, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_reconcile_endpoints(self):
        code, body = self.call(self.ingress, "/api/reconcile")
        self.assertEqual((code, body["count"]), (200, 8))
        key = {"Authorization": "Bearer " + "k" * 24}
        self.assertEqual(self.call(self.lan, "/api/reconcile", headers=key)[0], 200)
        self.assertEqual(self.call(self.lan, "/api/reconcile/mark", {}, key)[0], 403)  # LAN stays read-only
        code, body = self.call(self.ingress, "/api/reconcile/mark", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["reconciled_at"])
        self.assertEqual(self.published, 1)
        self.assertEqual(self.call(self.ingress, "/api/reconcile")[1]["count"], 0)
        self.assertEqual(self.call(self.ingress, "/api/reconcile/mark", {"clear": True})[1],
                         {"reconciled_at": None})
        self.assertEqual(self.call(self.ingress, "/api/reconcile")[1]["count"], 8)


if __name__ == "__main__":
    unittest.main()
