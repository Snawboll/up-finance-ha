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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import Database  # noqa: E402
from app.ha import build_sensors  # noqa: E402
from app.metrics import Metrics  # noqa: E402
from app.rules import parse_rules  # noqa: E402
from app.sync import Syncer  # noqa: E402
from app.web import App, run_readonly_query, serve  # noqa: E402
from fake_up import FakeUpClient, PERSONAL  # noqa: E402

TZ = ZoneInfo("Australia/Sydney")
TODAY = datetime.now(TZ).date()


def _prev_month(d):
    return f"{d.year - 1}-12" if d.month == 1 else f"{d.year}-{d.month - 1:02d}"


def setup_db(tmp: str):
    db = Database(Path(tmp) / "t.db")
    client = FakeUpClient(TODAY)
    Syncer(db, client, TZ, scope="joint").sync()
    return db, client


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.client = setup_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def count(self, sql="SELECT COUNT(*) FROM transactions", params=()):
        conn = self.db.connect()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def test_full_sync_stores_everything(self):
        joint = [t for t in self.client.txns if t["relationships"]["account"]["data"]["id"] != PERSONAL]
        self.assertEqual(set(self.client.calls), {None})
        self.assertNotIn(PERSONAL, self.client.account_calls)  # individual account never fetched
        self.assertEqual(self.count(), len(joint))
        self.assertEqual(self.count("SELECT COUNT(*) FROM accounts"), 5)
        self.assertEqual(self.count("SELECT COUNT(*) FROM transactions WHERE description='Secret Personal Shop'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM balance_snapshots"), 5)
        self.assertIsNotNone(self.db.get_state("full_sync_at"))

    def test_incremental_sync_uses_window_and_updates(self):
        syncer = Syncer(self.db, self.client, TZ, scope="joint")
        # settle the held Bunnings transaction and cancel the newest cafe one
        held = next(t for t in self.client.txns if t["attributes"]["status"] == "HELD")
        held["attributes"]["status"] = "SETTLED"
        cafe = next(t for t in self.client.txns if t["attributes"]["description"] == "New Cafe Co")
        self.client.txns.remove(cafe)
        # an old transaction outside the window disappears from the fake API: must be kept
        oldest = next(t for t in reversed(self.client.txns)
                      if t["relationships"]["account"]["data"]["id"] != PERSONAL)
        self.client.txns.remove(oldest)
        result = syncer.sync()
        self.assertIsNotNone(self.client.calls[-1])
        self.assertNotIn("acc-personal", self.client.account_calls)
        self.assertIn("window", result["mode"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM transactions WHERE id=?", (cafe["id"],)), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM transactions WHERE id=?", (oldest["id"],)), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM transactions WHERE id=? AND status='SETTLED'",
                                    (held["id"],)), 1)
        self.assertEqual(result["removed"], 1)

    def test_scope_change_removes_individual_data(self):
        Syncer(self.db, self.client, TZ, scope="all").sync()
        self.assertGreater(self.count("SELECT COUNT(*) FROM transactions WHERE account_id=?", (PERSONAL,)), 0)
        Syncer(self.db, self.client, TZ, scope="joint").sync()
        for table in ("transactions", "balance_snapshots"):
            self.assertEqual(self.count(f"SELECT COUNT(*) FROM {table} WHERE account_id=?", (PERSONAL,)), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM accounts WHERE id=?", (PERSONAL,)), 0)

    def test_flows_classification(self):
        conn = self.db.connect()
        try:
            kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM flows GROUP BY kind").fetchall())
            rent = conn.execute("SELECT kind, category FROM flows WHERE description LIKE 'Rent%' LIMIT 1").fetchone()
            sal = conn.execute("SELECT kind FROM flows WHERE description='ACME PTY LTD' LIMIT 1").fetchone()
            tr = conn.execute("SELECT kind FROM flows WHERE transfer_account_id='acc-saver' LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(set(kinds), {"spending", "income", "transfer", "allowance", "loan"})
        self.assertEqual(tuple(rent), ("spending", "uncategorised"))
        self.assertEqual(sal[0], "income")
        self.assertEqual(tr[0], "transfer")


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db, _ = setup_db(cls.tmp.name)
        cls.m = Metrics(cls.db, TZ, {"groceries": 600, "good-life": 400}, 200, today=TODAY)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_overview(self):
        ov = self.m.overview()
        self.assertGreater(ov["spent_last_month"], 2200)  # rent alone is $2,200
        self.assertGreater(ov["income_last_month"], 6000)
        self.assertEqual(ov["total_balance"], round((342150 + 1250000 + 210000 + 12000) / 100, 2))
        self.assertEqual(ov["total_savers"], 14720.0)
        self.assertEqual(ov["home_loan_balance"], 490000.0)
        self.assertIsNotNone(ov["savings_rate_last_month_pct"])
        self.assertEqual(ov["allowances_last_month"], 300.0)  # 2Up -> personal is not spending
        self.assertEqual(ov["net_last_month"],
                         round(ov["income_last_month"] - ov["spent_last_month"] - 300, 2))

    def test_monthly_excludes_transfers(self):
        mo = self.m.monthly(13)
        self.assertEqual(len(mo["months"]), 13)
        self.assertEqual(mo["months"][-1], TODAY.strftime("%Y-%m"))
        names = {s["category"] for s in mo["spending"]}
        self.assertTrue({"home", "good-life", "uncategorised"} <= names)
        # $500 saver transfer each month must not appear anywhere
        last = mo["total_spending"][-2]
        conn = self.db.connect()
        try:
            expected = -conn.execute(
                "SELECT SUM(amount_cents) FROM transactions WHERE month=? AND transfer_account_id IS NULL "
                "AND (category_id IS NOT NULL OR amount_cents<0) AND account_id != 'acc-homeloan'",
                (mo["months"][-2],)).fetchone()[0] / 100 + 1500  # + home loan interest
        finally:
            conn.close()
        self.assertAlmostEqual(last, expected, places=1)

    def test_recurring_detection(self):
        rec = {r["merchant"]: r for r in self.m.recurring()}
        self.assertEqual(rec["Netflix"]["cadence"], "monthly")
        self.assertEqual(rec["Netflix"]["typical_amount"], 22.99)
        self.assertTrue(rec["Netflix"]["active"])
        self.assertEqual(rec["Telstra"]["cadence"], "monthly")
        self.assertTrue(rec["Spotify"]["price_changed"] is False)  # latest two months both $14.99
        self.assertEqual(rec["Woolworths"]["cadence"], "weekly")
        self.assertNotIn("Seven Seeds", rec)  # irregular cafe visits are not subscriptions

    def test_budgets_and_categories(self):
        b = {x["category"]: x for x in self.m.budgets_status()}
        self.assertEqual(set(b), {"groceries", "good-life"})
        self.assertEqual(b["groceries"]["budget"], 600)
        cats = self.m.categories()
        self.assertTrue(all("avg_6m" in c for c in cats))

    def test_home_loan(self):
        conn = self.db.connect()
        try:
            rows = dict(conn.execute("SELECT description, kind FROM flows WHERE account_type='HOME_LOAN' "
                                     "GROUP BY description").fetchall())
        finally:
            conn.close()
        self.assertEqual(rows, {"Drawdown": "loan", "Interest": "spending"})
        cats = {c["category"]: c for c in self.m.categories(_prev_month(TODAY))}
        self.assertEqual(cats["home-loan-interest"]["spent"], 1500.0)

    def test_covers_linked_to_purchases(self):
        conn = self.db.connect()
        try:
            total, matched = conn.execute(
                "SELECT COUNT(*), COUNT(spend_id) FROM cover_links").fetchone()
            linked = conn.execute("SELECT DISTINCT description FROM flows WHERE covered_by IS NOT NULL").fetchall()
        finally:
            conn.close()
        self.assertGreater(total, 50)
        self.assertEqual(total, matched)
        self.assertEqual([r[0] for r in linked], ["Woolworths"])

    def test_savers(self):
        sv = {s["saver"]: s for s in self.m.savers()}
        daily = sv["🛒 Daily Expenses"]
        self.assertEqual(daily["top_up"], 600.0)
        self.assertEqual(daily["top_up_cadence"], "fortnightly")
        self.assertEqual(daily["monthly_budget"], 1300.0)
        self.assertEqual(daily["covered_categories_90d"][0]["category"], "Groceries")
        self.assertGreater(daily["covered_avg_3m"], 300)
        self.assertIsNone(sv["Holiday"]["monthly_budget"])

    def test_plan(self):
        p = Metrics(self.db, TZ, reserve_saver="emergency fund", today=TODAY).plan()
        self.assertEqual(p["reserve"]["saver"], "Emergency Fund")
        self.assertEqual(p["reserve"]["balance"], 12500.0)
        self.assertEqual(len(p["fortnights"]), 13)
        # fortnights line up with the Daily Expenses top-up day and today is in the last one
        cur = p["fortnights"][-1]
        self.assertLessEqual(cur["start"], TODAY.isoformat())
        self.assertLessEqual(TODAY.isoformat(), cur["end"])
        daily = next(s for s in p["savers"] if s["saver"] == "🛒 Daily Expenses")
        self.assertEqual(daily["role"], "spending")
        self.assertEqual(daily["per_fortnight"], 600.0)
        self.assertEqual(next(s for s in p["savers"] if s["saver"] == "Holiday")["role"], "goal")
        sm = p["summary"]
        self.assertEqual(sm["plan_per_fortnight"], 600.0)
        self.assertEqual(sm["income_avg_per_fortnight"], 3100.0)  # salary every fortnight
        self.assertAlmostEqual(sm["outgoings_per_fortnight"],
                               sm["plan_per_fortnight"] + sm["outside_plan_avg"] + sm["personal_avg"], places=2)
        self.assertGreater(p["reserve"]["runway_fortnights"], 0)
        # groceries are paid by Daily Expenses, so they're never outside the plan
        cats = {c["category"] for c in p["outside_categories"]}
        self.assertNotIn("groceries", cats)
        self.assertIn("restaurants-and-cafes", cats)
        self.assertTrue(all(x >= 0 for x in p["outside_plan"]))
        # no reserve configured: still works, just without the reserve block
        self.assertIsNone(Metrics(self.db, TZ, reserve_saver="", today=TODAY).plan()["reserve"])

    def test_large_and_new(self):
        large = self.m.large_transactions(30)
        self.assertIn("Apple Store", [x["merchant"] for x in large])
        self.assertIn("New Cafe Co", [x["merchant"] for x in self.m.new_merchants(30)])

    def test_sensors_are_small_enough_for_ha(self):
        sensors = build_sensors(self.m)
        self.assertIn("sensor.up_spending_this_month", sensors)
        self.assertIn("sensor.up_balance_emergency_fund", sensors)
        self.assertIn("sensor.up_budget_groceries", sensors)
        self.assertIn("sensor.up_saver_daily_expenses", sensors)
        self.assertIn("sensor.up_report_savers", sensors)
        self.assertIn("sensor.up_home_loan_owing", sensors)
        self.assertIn("sensor.up_outgoings_per_fortnight", sensors)
        self.assertIn("sensor.up_report_plan", sensors)
        for eid, (state, attrs) in sensors.items():
            size = len(json.dumps(attrs, default=str))
            self.assertLess(size, 16384, f"{eid} attributes are {size} bytes")
            self.assertLess(len(str(state)), 255, eid)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.client = setup_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self):
        conn = self.db.connect()
        try:
            return {(r[0], r[1]): (r[2], r[3], r[4]) for r in conn.execute(
                "SELECT description, COALESCE(message,''), kind, category, parent_category FROM flows "
                "WHERE description LIKE 'Rent%' OR description='ACME PTY LTD' OR description='Netflix'")}
        finally:
            conn.close()

    def test_rules(self):
        rules = parse_rules([
            {"payee": "rent - j", "message": "^rent$", "category": "utilities"},
            {"payee": "ACME", "category": "Side Hustle", "parent": "work", "treat_as": "income"},
        ], ["Netflix"])
        self.assertGreater(self.db.set_rules(rules), 10)
        k = self.kinds()
        self.assertEqual(k[("Rent - J Smith", "Rent")], ("spending", "utilities", "home"))
        self.assertEqual(k[("ACME PTY LTD", "Salary")], ("income", "side-hustle", "work"))
        self.assertEqual(k[("Netflix", "")][0], "transfer")
        names = Metrics(self.db, TZ, today=TODAY).category_names()
        self.assertEqual(names["side-hustle"], "Side Hustle")
        # rules survive a sync, and clearing them restores Up's classification
        Syncer(self.db, self.client, TZ, scope="joint").sync()
        self.assertEqual(self.kinds()[("Netflix", "")][0], "transfer")
        self.db.set_rules([])
        self.assertEqual(self.kinds()[("Netflix", "")], ("spending", "tv-and-music", "good-life"))

    def test_bad_rules_are_skipped(self):
        self.assertEqual(parse_rules([{"payee": "x", "message": "("}, {"payee": "y", "treat_as": "nope"},
                                      {"payee": ""}]), [])


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db, _ = setup_db(cls.tmp.name)
        m = Metrics(cls.db, TZ, {"groceries": 600}, 200, today=TODAY)
        cls.key = "k" * 24
        app = App(cls.db, m, cls.key, lambda: {"ok": True})
        cls.dev = serve(app, 0, "dev", host="127.0.0.1")
        cls.lan = serve(app, 0, "lan", host="127.0.0.1")
        cls.ingress = serve(app, 0, "ingress", host="127.0.0.1")

    @classmethod
    def tearDownClass(cls):
        for s in (cls.dev, cls.lan, cls.ingress):
            s.shutdown()
        cls.tmp.cleanup()

    def get(self, server, path, headers=None, data=None):
        url = f"http://127.0.0.1:{server.server_address[1]}{path}"
        req = urllib.request.Request(url, headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_dashboard_and_endpoints(self):
        code, body = self.get(self.dev, "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Monthly spending by category", body)
        for path in ["/api/overview", "/api/monthly", "/api/categories", "/api/budgets", "/api/recurring", "/api/savers",
                     "/api/merchants", "/api/large", "/api/new_merchants", "/api/balances",
                     "/api/transactions?q=netflix", "/api/category_list", "/api/report", "/api/health"]:
            code, body = self.get(self.dev, path)
            self.assertEqual(code, 200, path)
            json.loads(body)
        code, body = self.get(self.dev, "/api/transactions?q=netflix")
        self.assertTrue(all("Netflix" in r["description"] for r in json.loads(body)))

    def test_rule_endpoints_over_ingress(self):
        code, body = self.get(self.ingress, "/api/uncategorised")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)[0]["payee"], "Rent - J Smith")
        self.assertEqual(json.loads(self.get(self.ingress, "/api/rules")[1])["rules"], [])
        rules = {"payee_rules": [{"payee": "rent", "message": "^rent$", "category": "utilities"},
                                 {"payee": "nobody", "category": "family"}], "ignore_payees": ["Netflix"]}
        code, body = self.get(self.ingress, "/api/rules/preview", {"Content-Type": "application/json"},
                              json.dumps(rules).encode())
        self.assertEqual(code, 200)
        out = json.loads(body)["rules"]
        self.assertGreater(out[0]["matched"], 10)
        self.assertLess(out[0]["total"], 0)
        self.assertEqual(out[0]["examples"][0]["description"], "Rent - J Smith")
        self.assertEqual(out[1]["matched"], 0)
        self.assertEqual(out[2]["rule"], {"payee": "Netflix", "treat_as": "transfer"})
        # preview must not change anything
        self.assertEqual(json.loads(self.get(self.ingress, "/api/uncategorised")[1])[0]["payee"], "Rent - J Smith")

    def test_lan_requires_key(self):
        self.assertEqual(self.get(self.lan, "/api/overview")[0], 401)
        self.assertEqual(self.get(self.lan, "/api/overview", {"Authorization": "Bearer wrong"})[0], 401)
        code, _ = self.get(self.lan, "/api/overview", {"Authorization": f"Bearer {self.key}"})
        self.assertEqual(code, 200)

    def test_ingress_has_read_only_sql(self):
        # test client connects from 127.0.0.1, which the ingress listener allows
        self.assertEqual(self.get(self.ingress, "/api/overview")[0], 200)
        code, body = self.get(self.ingress, "/api/query", data=b'{"sql":"SELECT 1 AS x"}')
        self.assertEqual((code, json.loads(body)["rows"]), (200, [[1]]))
        code, body = self.get(self.ingress, "/api/query", data=b'{"sql":"DELETE FROM transactions"}')
        self.assertEqual(code, 400)

    def test_query_and_export(self):
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        code, body = self.get(self.lan, "/api/query", h,
                              json.dumps({"sql": "SELECT kind, COUNT(*) n FROM flows GROUP BY kind"}).encode())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["columns"], ["kind", "n"])
        code, body = self.get(self.lan, "/api/export.csv", h)
        self.assertEqual(code, 200)
        self.assertTrue(body.startswith(b"local_date,"))

    def test_query_is_read_only(self):
        for sql in ["DELETE FROM transactions", "INSERT INTO sync_state VALUES ('a','b')",
                    "DROP TABLE transactions", f"ATTACH DATABASE '{self.tmp.name}/x.db' AS x",
                    "PRAGMA query_only=OFF", "SELECT 1; DELETE FROM transactions"]:
            with self.assertRaises(Exception, msg=sql):
                run_readonly_query(self.db, sql)
        self.assertGreater(run_readonly_query(self.db, "SELECT COUNT(*) FROM transactions")["rows"][0][0], 100)
        self.assertFalse(Path(self.tmp.name, "x.db").exists())


if __name__ == "__main__":
    unittest.main()
