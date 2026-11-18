"""Spending metrics computed from the SQLite store.

All money values returned are in dollars (float, 2dp) unless a key ends in `_cents`.
Spending is reported as a positive number.
"""
from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .db import Database

CADENCES = [  # (name, min_days, max_days, per_year)
    ("weekly", 6, 8, 52),
    ("fortnightly", 13, 16, 26),
    ("monthly", 26, 35, 12),
    ("quarterly", 84, 98, 4),
    ("yearly", 350, 380, 1),
]


def d(cents: Optional[int]) -> float:
    return round((cents or 0) / 100, 2)


def _month_add(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


class Metrics:
    def __init__(self, db: Database, tz: ZoneInfo, budgets: Optional[dict[str, float]] = None,
                 large_threshold: float = 200.0, today: Optional[date] = None,
                 reserve_saver: str = "Savings"):
        self.db = db
        self.reserve_saver = reserve_saver
        self.tz = tz
        self.budgets = budgets or {}
        self.large_threshold = large_threshold
        self._today = today

    @property
    def today(self) -> date:
        return self._today or datetime.now(self.tz).date()

    def _q(self, sql: str, params=()) -> list[dict]:
        conn = self.db.connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def category_names(self) -> dict[str, str]:
        names = {r["id"]: r["name"] for r in self._q("SELECT id, name FROM categories")}
        names.setdefault("uncategorised", "Uncategorised")
        names.setdefault("home-loan-interest", "Home Loan Interest")
        names.setdefault("home-loan-repayments", "Home Loan Repayments")
        for r in self._q("SELECT DISTINCT category FROM txn_overrides WHERE category IS NOT NULL "
                         "UNION SELECT DISTINCT parent_category FROM txn_overrides WHERE parent_category IS NOT NULL"):
            names.setdefault(r["category"], r["category"].replace("-", " ").title())
        return names

    # -- headline numbers --------------------------------------------------
    def overview(self) -> dict:
        t = self.today
        month = t.strftime("%Y-%m")
        last_month = _month_add(month, -1)
        days_in_month = calendar.monthrange(t.year, t.month)[1]
        # same day-of-month last month (clamped)
        ly, lm = map(int, last_month.split("-"))
        same_day_last = date(ly, lm, min(t.day, calendar.monthrange(ly, lm)[1])).isoformat()

        rows = self._q(
            """SELECT month, kind, SUM(amount_cents) AS total FROM flows
               WHERE month IN (?, ?) AND kind != 'transfer' GROUP BY month, kind""",
            (month, last_month),
        )
        tot = {(r["month"], r["kind"]): r["total"] for r in rows}
        spent_mtd = -(tot.get((month, "spending")) or 0)
        spent_last = -(tot.get((last_month, "spending")) or 0)
        income_mtd = tot.get((month, "income")) or 0
        income_last = tot.get((last_month, "income")) or 0
        allow_mtd = -(tot.get((month, "allowance")) or 0)
        allow_last = -(tot.get((last_month, "allowance")) or 0)
        spent_last_same = -(self._q(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM flows WHERE kind='spending' "
            "AND month=? AND local_date <= ?", (last_month, same_day_last))[0]["s"])

        savings_rate = None
        if income_last > 0:
            savings_rate = round((income_last - spent_last - allow_last) / income_last * 100, 1)

        accounts = self._q("SELECT id, name, type, ownership, balance_cents FROM accounts ORDER BY type, name")
        loans = [a for a in accounts if a["type"] == "HOME_LOAN"]
        cash = [a for a in accounts if a["type"] != "HOME_LOAN"]
        return {
            "month": month,
            "day_of_month": t.day,
            "days_in_month": days_in_month,
            "spent_this_month": d(spent_mtd),
            "spent_last_month": d(spent_last),
            "spent_last_month_to_same_day": d(spent_last_same),
            "projected_this_month": d(spent_mtd + self._expected_rest_of_month(t)),
            "income_this_month": d(income_mtd),
            "income_last_month": d(income_last),
            "allowances_this_month": d(allow_mtd),
            "allowances_last_month": d(allow_last),
            "net_last_month": d(income_last - spent_last - allow_last),
            "savings_rate_last_month_pct": savings_rate,
            "accounts": [
                {"id": a["id"], "name": a["name"], "type": a["type"], "ownership": a["ownership"],
                 "balance": d(a["balance_cents"])} for a in accounts
            ],
            "total_balance": d(sum(a["balance_cents"] or 0 for a in cash)),
            "total_savers": d(sum(a["balance_cents"] or 0 for a in cash if a["type"] == "SAVER")),
            "home_loan_balance": d(-sum(a["balance_cents"] or 0 for a in loans)) if loans else None,
            "last_sync_at": self.db.get_state("last_sync_at"),
        }

    def _expected_rest_of_month(self, t: date) -> int:
        """Typical spend after this day-of-month, averaged over the last 3 full months.

        Better than a straight-line projection when big bills (rent) land early in the month.
        Falls back to straight-line when there is no history yet.
        """
        month = t.strftime("%Y-%m")
        rests = []
        for k in range(1, 4):
            m = _month_add(month, -k)
            y, mo = map(int, m.split("-"))
            cutoff = date(y, mo, min(t.day, calendar.monthrange(y, mo)[1])).isoformat()
            r = self._q(
                "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN local_date > ? THEN -amount_cents END), 0) AS rest "
                "FROM flows WHERE kind='spending' AND month=?", (cutoff, m))[0]
            if r["n"]:
                rests.append(r["rest"])
        if rests:
            return max(0, round(sum(rests) / len(rests)))
        spent = -(self._q("SELECT COALESCE(SUM(amount_cents),0) AS s FROM flows WHERE kind='spending' AND month=?",
                          (month,))[0]["s"])
        days = calendar.monthrange(t.year, t.month)[1]
        return round(spent / t.day * (days - t.day))

    # -- category breakdowns ----------------------------------------------
    def monthly(self, months: int = 13) -> dict:
        """Spending per parent category and income per month, oldest first."""
        current = self.today.strftime("%Y-%m")
        first = _month_add(current, -(months - 1))
        month_list = [_month_add(first, i) for i in range(months)]
        rows = self._q(
            """SELECT month, kind, parent_category, SUM(amount_cents) AS total FROM flows
               WHERE month >= ? AND kind IN ('spending', 'income', 'allowance') GROUP BY month, kind, parent_category""",
            (first,),
        )
        names = self.category_names()
        spend: dict[str, dict[str, float]] = defaultdict(dict)
        income: dict[str, float] = defaultdict(float)
        allowance: dict[str, float] = defaultdict(float)
        for r in rows:
            if r["kind"] == "spending":
                spend[r["parent_category"]][r["month"]] = d(-r["total"])
            elif r["kind"] == "allowance":
                allowance[r["month"]] += d(-r["total"])
            else:
                income[r["month"]] += d(r["total"])
        parents = sorted(spend, key=lambda p: -sum(spend[p].values()))
        return {
            "months": month_list,
            "spending": [
                {"category": p, "name": names.get(p, p),
                 "values": [spend[p].get(m, 0.0) for m in month_list]}
                for p in parents
            ],
            "income": [round(income.get(m, 0.0), 2) for m in month_list],
            "allowances": [round(allowance.get(m, 0.0), 2) for m in month_list],
            "total_spending": [round(sum(spend[p].get(m, 0.0) for p in parents), 2) for m in month_list],
        }

    def categories(self, month: Optional[str] = None) -> list[dict]:
        """Per-category spend for a month vs the trailing 6 full-month average."""
        month = month or self.today.strftime("%Y-%m")
        start = _month_add(month, -6)
        rows = self._q(
            """SELECT category, parent_category, month, SUM(amount_cents) AS total, COUNT(*) AS n
               FROM flows WHERE kind='spending' AND month >= ? AND month <= ?
               GROUP BY category, parent_category, month""",
            (start, month),
        )
        names = self.category_names()
        out: dict[str, dict] = {}
        for r in rows:
            c = out.setdefault(r["category"], {
                "category": r["category"], "name": names.get(r["category"], r["category"]),
                "parent": r["parent_category"], "parent_name": names.get(r["parent_category"], r["parent_category"]),
                "spent": 0.0, "count": 0, "history": {},
            })
            if r["month"] == month:
                c["spent"] = d(-r["total"])
                c["count"] = r["n"]
            else:
                c["history"][r["month"]] = d(-r["total"])
        result = []
        for c in out.values():
            c["avg_6m"] = round(sum(c.pop("history").values()) / 6, 2)
            c["budget"] = self.budgets.get(c["category"])
            result.append(c)
        return sorted(result, key=lambda c: -c["spent"])

    def budgets_status(self) -> list[dict]:
        if not self.budgets:
            return []
        t = self.today
        month = t.strftime("%Y-%m")
        days_in_month = calendar.monthrange(t.year, t.month)[1]
        rows = self._q(
            """SELECT category, parent_category, SUM(amount_cents) AS total FROM flows
               WHERE kind='spending' AND month=? GROUP BY category, parent_category""",
            (month,),
        )
        names = self.category_names()
        out = []
        for key, budget in self.budgets.items():
            spent = -sum(r["total"] for r in rows if key in (r["category"], r["parent_category"]))
            projected = spent / t.day * days_in_month
            out.append({
                "category": key,
                "name": names.get(key, key),
                "budget": budget,
                "spent": d(spent),
                "remaining": round(budget - d(spent), 2),
                "projected": d(round(projected)),
                "used_pct": round(d(spent) / budget * 100, 1) if budget else None,
                "pace_pct": round(d(round(projected)) / budget * 100, 1) if budget else None,
            })
        return sorted(out, key=lambda b: -(b["pace_pct"] or 0))

    # -- merchants & transactions -----------------------------------------
    def merchants(self, days: int = 90, limit: int = 25) -> list[dict]:
        since = (self.today - timedelta(days=days)).isoformat()
        rows = self._q(
            """SELECT description, category, COUNT(*) AS n, SUM(amount_cents) AS total,
                      MAX(local_date) AS last_date
               FROM flows WHERE kind='spending' AND amount_cents < 0 AND local_date > ?
               GROUP BY description ORDER BY total ASC LIMIT ?""",
            (since, limit),
        )
        names = self.category_names()
        return [{"merchant": r["description"], "category": names.get(r["category"], r["category"]),
                 "count": r["n"], "spent": d(-r["total"]), "last_date": r["last_date"]} for r in rows]

    def large_transactions(self, days: int = 30, limit: int = 15) -> list[dict]:
        since = (self.today - timedelta(days=days)).isoformat()
        rows = self._q(
            """SELECT local_date, description, category, amount_cents, account_name FROM flows
               WHERE kind='spending' AND amount_cents <= ? AND local_date > ?
               ORDER BY amount_cents ASC LIMIT ?""",
            (-int(self.large_threshold * 100), since, limit),
        )
        names = self.category_names()
        return [{"date": r["local_date"], "merchant": r["description"],
                 "category": names.get(r["category"], r["category"]), "amount": d(-r["amount_cents"]),
                 "account": r["account_name"]} for r in rows]

    def new_merchants(self, days: int = 30) -> list[dict]:
        since = (self.today - timedelta(days=days)).isoformat()
        rows = self._q(
            """SELECT description, MIN(local_date) AS first_date, COUNT(*) AS n, SUM(amount_cents) AS total
               FROM flows WHERE kind='spending' AND amount_cents < 0
               GROUP BY description HAVING first_date > ? ORDER BY total ASC""",
            (since,),
        )
        return [{"merchant": r["description"], "first_date": r["first_date"], "count": r["n"],
                 "spent": d(-r["total"])} for r in rows]

    def recurring(self, lookback_days: int = 400) -> list[dict]:
        """Detect subscriptions / bills: same merchant, regular interval, similar amount."""
        since = (self.today - timedelta(days=lookback_days)).isoformat()
        rows = self._q(
            """SELECT description, category, local_date, amount_cents FROM flows
               WHERE kind='spending' AND amount_cents < 0 AND local_date > ?
               ORDER BY description, local_date""",
            (since,),
        )
        names = self.category_names()
        by_merchant: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_merchant[r["description"]].append(r)

        found = []
        for merchant, txns in by_merchant.items():
            # collapse same-day duplicates
            per_day: dict[str, int] = defaultdict(int)
            for t in txns:
                per_day[t["local_date"]] += -t["amount_cents"]
            dates = sorted(per_day)
            if len(dates) < 3 and not (len(dates) == 2 and _gap(dates[0], dates[1]) >= 350):
                continue
            gaps = [_gap(a, b) for a, b in zip(dates, dates[1:])]
            med_gap = statistics.median(gaps)
            cadence = next((c for c in CADENCES if c[1] <= med_gap <= c[2]), None)
            if not cadence:
                continue
            # most gaps should fit the cadence
            fitting = sum(1 for g in gaps if cadence[1] * 0.8 <= g <= cadence[2] * 1.25)
            if fitting / len(gaps) < 0.7:
                continue
            amounts = [per_day[x] for x in dates[-6:]]
            mean_amt = statistics.mean(amounts)
            if mean_amt <= 0:
                continue
            spread = (statistics.pstdev(amounts) / mean_amt) if len(amounts) > 1 else 0
            if spread > 0.25:
                continue
            last = date.fromisoformat(dates[-1])
            expected_next = last + timedelta(days=round(med_gap))
            active = (self.today - last).days <= cadence[2] * 1.5 + 7
            found.append({
                "merchant": merchant,
                "category": names.get(txns[-1]["category"], txns[-1]["category"]),
                "cadence": cadence[0],
                "typical_amount": d(round(statistics.median(amounts))),
                "last_amount": d(per_day[dates[-1]]),
                "last_date": dates[-1],
                "next_expected": expected_next.isoformat(),
                "occurrences": len(dates),
                "annual_cost": d(round(statistics.median(amounts) * cadence[3])),
                "active": active,
                # only meaningful for fixed-price charges: earlier amounts identical, latest differs
                "price_changed": len(dates) >= 3 and len(set(per_day[x] for x in dates[-4:-1])) == 1
                and per_day[dates[-1]] != per_day[dates[-2]],
            })
        return sorted(found, key=lambda r: (not r["active"], -r["annual_cost"]))

    def _daily_balances(self, account_id: str, current_cents: int, since: date) -> dict[str, int]:
        """End-of-day balances rebuilt backwards from today's balance and the transactions."""
        rows = self._q("SELECT local_date, SUM(amount_cents) AS amt FROM transactions WHERE account_id=? "
                       "AND local_date > ? GROUP BY local_date", (account_id, since.isoformat()))
        per_day = {r["local_date"]: r["amt"] for r in rows}
        out, bal, day = {}, current_cents, self.today
        while day >= since:
            out[day.isoformat()] = bal
            bal -= per_day.get(day.isoformat(), 0)
            day -= timedelta(days=1)
        return out

    def balance_history(self, days: int = 365, step_days: int = 7) -> dict:
        """Weekly balance per account (home loans excluded), rebuilt from transactions."""
        since = self.today - timedelta(days=days)
        accounts = self._q("SELECT id, name, balance_cents, created_at FROM accounts "
                           "WHERE type != 'HOME_LOAN' ORDER BY name")
        series = []
        for a in accounts:
            daily = self._daily_balances(a["id"], a["balance_cents"] or 0, since)
            opened = (a["created_at"] or "")[:10]
            points = []
            for i in range(0, days + 1, step_days):
                dt = (self.today - timedelta(days=days - i)).isoformat()
                if dt >= opened:
                    points.append({"date": dt, "balance": d(daily.get(dt, 0))})
            if points and points[-1]["date"] != self.today.isoformat():
                points.append({"date": self.today.isoformat(), "balance": d(a["balance_cents"])})
            if points:
                series.append({"account": a["name"], "points": points})
        return {"series": series}

    # -- savers (envelopes) -------------------------------------------------
    def _topup(self, account_id: str) -> tuple[Optional[int], Optional[str], int]:
        """A saver's regular automatic top-up: (amount in cents, cadence, times per year)."""
        topups = self._q(
            "SELECT local_date, amount_cents FROM transactions WHERE account_id=? AND amount_cents>0 "
            "AND transfer_account_id IS NOT NULL AND description LIKE 'Auto Transfer%' "
            "ORDER BY created_at DESC LIMIT 8", (account_id,))
        cadence, per_year = None, 0
        if len(topups) >= 2:
            gaps = [_gap(b["local_date"], a["local_date"]) for a, b in zip(topups, topups[1:])]
            med = statistics.median(gaps)
            c = next((c for c in CADENCES if c[1] <= med <= c[2]), None)
            if c:
                cadence, per_year = c[0], c[3]
        topup = topups[0]["amount_cents"] if topups and cadence else None
        if topups and (self.today - date.fromisoformat(topups[0]["local_date"])).days > 45:
            return None, None, 0  # auto top-up has stopped
        return topup, cadence, (per_year if topup else 0)

    def savers(self, months: int = 6) -> list[dict]:
        """Per saver: regular top-up (its budget), what it covered, and where that money went."""
        t = self.today
        cur = t.strftime("%Y-%m")
        month_list = [_month_add(cur, -i) for i in range(months - 1, -1, -1)]
        since90 = (t - timedelta(days=90)).isoformat()
        names = self.category_names()
        out = []
        for acc in self._q("SELECT id, name, balance_cents FROM accounts WHERE type='SAVER' ORDER BY name"):
            aid = acc["id"]
            topup, cadence, per_year = self._topup(aid)
            monthly_budget = round(topup * per_year / 12) if topup else None

            covered = {r["month"]: r["total"] for r in self._q(
                "SELECT c.month, SUM(cl.amount_cents) AS total FROM cover_links cl "
                "JOIN transactions c ON c.id = cl.cover_id WHERE cl.saver_id=? AND c.month >= ? GROUP BY c.month",
                (aid, month_list[0]))}
            funded = {r["month"]: r["total"] for r in self._q(
                "SELECT month, SUM(amount_cents) AS total FROM transactions WHERE account_id=? AND amount_cents>0 "
                "AND transfer_account_id IN (SELECT id FROM accounts) AND month >= ? GROUP BY month",
                (aid, month_list[0]))}
            spent_from = self._q(
                "SELECT COALESCE(f.category, 'unmatched') AS category, f.description AS merchant, "
                "SUM(cl.amount_cents) AS total FROM cover_links cl JOIN transactions c ON c.id = cl.cover_id "
                "LEFT JOIN flows f ON f.id = cl.spend_id WHERE cl.saver_id=? AND c.local_date > ? "
                "GROUP BY 1, 2", (aid, since90))
            # Direct repayments from the saver into a home loan count as paid out too.
            for r in self._q(
                    "SELECT month, -SUM(amount_cents) AS total FROM transactions WHERE account_id=? "
                    "AND amount_cents<0 AND transfer_account_id IN (SELECT id FROM accounts WHERE type='HOME_LOAN') "
                    "AND month >= ? GROUP BY month", (aid, month_list[0])):
                covered[r["month"]] = covered.get(r["month"], 0) + r["total"]
            loan90 = self._q(
                "SELECT -COALESCE(SUM(amount_cents),0) AS total FROM transactions WHERE account_id=? AND amount_cents<0 "
                "AND transfer_account_id IN (SELECT id FROM accounts WHERE type='HOME_LOAN') AND local_date > ?",
                (aid, since90))[0]["total"]
            by_cat: dict[str, int] = defaultdict(int)
            by_merchant: dict[str, int] = defaultdict(int)
            if loan90:
                by_cat["home-loan-repayments"] = loan90
                by_merchant["Home loan repayments"] = loan90
            for r in spent_from:
                by_cat[r["category"]] += r["total"]
                by_merchant[r["merchant"] or "Unmatched covers"] += r["total"]
            full_months = month_list[-4:-1]
            avg3 = sum(covered.get(m, 0) for m in full_months) / 3
            bal90 = self._daily_balances(aid, acc["balance_cents"] or 0, t - timedelta(days=90)).get(
                (t - timedelta(days=90)).isoformat())
            out.append({
                "saver": acc["name"],
                "balance": d(acc["balance_cents"]),
                "balance_90d_ago": d(bal90),
                "top_up": d(topup) if topup else None,
                "top_up_cadence": cadence,
                "monthly_budget": d(monthly_budget) if monthly_budget else None,
                "covered_this_month": d(covered.get(cur, 0)),
                "covered_avg_3m": d(round(avg3)),
                "budget_used_avg_pct": round(avg3 / monthly_budget * 100, 1) if monthly_budget else None,
                "months": month_list,
                "covered_by_month": [d(covered.get(m, 0)) for m in month_list],
                "funded_by_month": [d(funded.get(m, 0)) for m in month_list],
                "covered_categories_90d": [
                    {"category": names.get(k, "Unmatched covers" if k == "unmatched" else k), "amount": d(v)}
                    for k, v in sorted(by_cat.items(), key=lambda x: -x[1])][:8],
                "covered_merchants_90d": [
                    {"merchant": k, "amount": d(v)} for k, v in sorted(by_merchant.items(), key=lambda x: -x[1])][:8],
            })
        return sorted(out, key=lambda x: (x["monthly_budget"] is None, -(x["monthly_budget"] or 0), -x["balance"]))

    # -- the fortnightly plan --------------------------------------------------
    def _find_reserve(self, savers: list[dict]) -> Optional[dict]:
        """The saver all income collects in and the fortnightly top-ups are paid from."""
        want = _alnum(self.reserve_saver)
        if not want:
            return None
        exact = [a for a in savers if _alnum(a["name"]) == want]
        return (exact or [a for a in savers if want in _alnum(a["name"])] or [None])[0]

    def _fortnights(self, n: int) -> list[tuple[date, date]]:
        """The last n fortnights (oldest first), lined up with the savers' top-up day.

        Each is (start, end) inclusive; the last one is the current, partial fortnight.
        """
        r = self._q("SELECT MAX(local_date) AS d FROM transactions WHERE amount_cents>0 "
                    "AND description LIKE 'Auto Transfer%' AND account_id IN "
                    "(SELECT id FROM accounts WHERE type='SAVER')")
        t = self.today
        anchor = date.fromisoformat(r[0]["d"]) if r and r[0]["d"] else t - timedelta(days=t.weekday())
        start = anchor + timedelta(days=14 * ((t - anchor).days // 14))
        return [(start - timedelta(days=14 * k), start - timedelta(days=14 * k - 13)) for k in range(n - 1, -1, -1)]

    def plan(self, fortnights: int = 13) -> dict:
        """How the household runs: all income lands in the reserve saver, fixed fortnightly
        top-ups go from it to the other savers, and anything no topped-up saver pays for comes
        out of the reserve ("outside the plan"). Everything is per fortnight."""
        t = self.today
        periods = self._fortnights(fortnights)
        first = periods[0][0].isoformat()
        starts = [p[0].isoformat() for p in periods]

        def bucket(day: str) -> Optional[int]:
            for i in range(len(starts) - 1, -1, -1):
                if day >= starts[i]:
                    return i
            return None

        def per_fn(rows, key="local_date", val="amt") -> list[int]:
            out = [0] * len(periods)
            for r in rows:
                i = bucket(r[key])
                if i is not None:
                    out[i] += r[val] or 0
            return out

        accounts = self._q("SELECT id, name, balance_cents FROM accounts WHERE type='SAVER' ORDER BY name")
        reserve = self._find_reserve(accounts)
        rid = reserve["id"] if reserve else None
        others = [a for a in accounts if a["id"] != rid]
        other_ids = {a["id"] for a in others}

        # Money moved in from a personal account counts as income to the household; personal money
        # is only what goes out to personal accounts.
        income = per_fn(self._q("SELECT local_date, SUM(amount_cents) AS amt FROM flows WHERE (kind='income' "
                                "OR (kind='allowance' AND amount_cents>0)) AND local_date >= ? GROUP BY local_date",
                                (first,)))
        personal = per_fn(self._q("SELECT local_date, -SUM(amount_cents) AS amt FROM flows WHERE kind='allowance' "
                                  "AND amount_cents<0 AND local_date >= ? GROUP BY local_date", (first,)))
        spending = per_fn(self._q("SELECT local_date, -SUM(amount_cents) AS amt FROM flows WHERE kind='spending' "
                                  "AND account_type != 'HOME_LOAN' AND local_date >= ? GROUP BY local_date", (first,)))

        savers, into_savers = [], [0] * len(periods)
        covered_by_plan = [0] * len(periods)
        for a in others:
            aid = a["id"]
            topup, cadence, per_year = self._topup(aid)
            used = per_fn(self._q(
                "SELECT c.local_date, SUM(cl.amount_cents) AS amt FROM cover_links cl JOIN transactions c "
                "ON c.id = cl.cover_id WHERE cl.saver_id=? AND c.local_date >= ? GROUP BY c.local_date",
                (aid, first)))
            for i, v in enumerate(used):
                covered_by_plan[i] += v
            loan = per_fn(self._q(
                "SELECT local_date, -SUM(amount_cents) AS amt FROM transactions WHERE account_id=? AND amount_cents<0 "
                "AND transfer_account_id IN (SELECT id FROM accounts WHERE type='HOME_LOAN') AND local_date >= ? "
                "GROUP BY local_date", (aid, first)))
            # Money put in from outside the other savers (top-ups and one-off moves from the reserve/spending).
            put_in = per_fn(self._q(
                "SELECT local_date, SUM(amount_cents) AS amt FROM transactions WHERE account_id=? AND amount_cents>0 "
                "AND transfer_account_id IN (SELECT id FROM accounts) AND description NOT LIKE 'Cover%' "
                f"AND transfer_account_id NOT IN ({','.join('?' * len(other_ids))}) AND local_date >= ? "
                "GROUP BY local_date", (aid, *other_ids, first)))
            for i, v in enumerate(put_in):
                into_savers[i] += v
            paid = [u + l for u, l in zip(used, loan)]
            daily = self._daily_balances(aid, a["balance_cents"] or 0, periods[0][0] - timedelta(days=1))
            full = paid[:-1]
            savers.append({
                "saver": a["name"],
                "role": "spending" if any(paid) else "goal",
                "top_up": d(topup) if topup else None,
                "top_up_cadence": cadence,
                "per_fortnight": d(round(topup * per_year / 26)) if topup else None,
                "paid_out": [d(v) for v in paid],
                "put_in": [d(v) for v in put_in],
                # over all full fortnights: monthly items (loan repayments) land 2 or 3 times in 6
                "paid_out_avg": d(round(sum(full) / max(1, len(full)))),
                "balance": d(a["balance_cents"]),
                "balance_at_start": d(daily.get((periods[0][0] - timedelta(days=1)).isoformat(), 0)),
                "balance_by_fortnight": [d(daily.get(min(e, t).isoformat(), 0)) for _, e in periods],
            })
        savers.sort(key=lambda x: (x["role"] != "spending", -(x["per_fortnight"] or 0), -x["balance"]))

        # Outside the plan = spending no topped-up saver paid for. Measured from cover totals, so a
        # cover that couldn't be matched to its purchase still counts for its saver.
        outside = [max(0, s - c) for s, c in zip(spending, covered_by_plan)]
        names = self.category_names()
        parents = {r["id"]: r["parent_id"] for r in self._q("SELECT id, parent_id FROM categories")}
        excl = f"AND (covered_by_id IS NULL OR covered_by_id NOT IN ({','.join('?' * len(other_ids))}))" \
            if other_ids else ""
        rows = self._q(
            "SELECT local_date, category, parent_category, description, -amount_cents AS amt, covered_by_id, message "
            f"FROM flows WHERE kind='spending' AND account_type != 'HOME_LOAN' AND local_date >= ? {excl}",
            (first, *other_ids))
        by_cat: dict[str, list[int]] = defaultdict(lambda: [0] * len(periods))
        cat_parent: dict[str, str] = {}
        for r in rows:
            cat_parent.setdefault(r["category"], r["parent_category"] or "uncategorised")
            i = bucket(r["local_date"])
            if i is not None:
                by_cat[r["category"]][i] += r["amt"]
        full_n = max(1, len(periods) - 1)
        categories = sorted(({
            "category": c, "name": names.get(c, c),
            "parent": parents.get(c) or cat_parent.get(c) or "uncategorised",
            "this_fortnight": d(v[-1]), "per_fortnight": d(round(sum(v[:-1]) / full_n)),
            "total": d(sum(v[:-1])), "by_fortnight": [d(x) for x in v],
        } for c, v in by_cat.items()), key=lambda x: -x["total"])
        for c in categories:
            c["parent_name"] = names.get(c["parent"], c["parent"].replace("-", " ").title())
        recent_from = periods[-6][0].isoformat() if len(periods) >= 6 else first
        purchases = sorted((r for r in rows if r["local_date"] >= recent_from and r["amt"] > 0),
                           key=lambda r: -r["amt"])[:20]

        reserve_out = None
        full_out = outside[:-1]
        avg = lambda xs: round(sum(xs) / max(1, len(xs)))  # noqa: E731
        plan_fn = sum(round((sv["per_fortnight"] or 0) * 100) for sv in savers)
        outgoings = plan_fn + avg(full_out) + avg(personal[:-1])
        if reserve:
            daily = self._daily_balances(rid, reserve["balance_cents"] or 0, periods[0][0])
            bal = reserve["balance_cents"] or 0
            reserve_out = {
                "saver": reserve["name"],
                "balance": d(bal),
                "balance_by_fortnight": [d(daily.get(min(e, t).isoformat(), 0)) for _, e in periods],
                "balance_at_start_of_fortnight": d(daily.get((periods[-1][0] - timedelta(days=1)).isoformat(),
                                                             daily.get(periods[-1][0].isoformat(), bal))),
                "runway_fortnights": round(bal / outgoings, 1) if outgoings > 0 else None,
            }
        income_26 = self._q("SELECT COALESCE(SUM(amount_cents),0) AS s, MIN(local_date) AS f FROM flows "
                            "WHERE (kind='income' OR (kind='allowance' AND amount_cents>0)) "
                            "AND local_date >= ? AND local_date < ?",
                            ((periods[-1][0] - timedelta(days=364)).isoformat(), periods[-1][0].isoformat()))[0]
        first_txn = self._q("SELECT MIN(local_date) AS f FROM transactions")[0]["f"]
        n26 = 26
        if first_txn:
            n26 = max(1, min(26, (periods[-1][0] - date.fromisoformat(first_txn)).days // 14))
        income_avg = round(income_26["s"] / n26)
        return {
            "reserve": reserve_out,
            "reserve_saver_setting": self.reserve_saver,
            "fortnights": [{"start": s_.isoformat(), "end": e.isoformat()} for s_, e in periods],
            "current": {"start": periods[-1][0].isoformat(), "end": periods[-1][1].isoformat(),
                        "day": (t - periods[-1][0]).days + 1},
            "income": [d(v) for v in income],
            "into_savers": [d(v) for v in into_savers],
            "outside_plan": [d(v) for v in outside],
            "personal": [d(v) for v in personal],
            "summary": {
                "plan_per_fortnight": d(plan_fn),
                "outside_plan_avg": d(avg(full_out)),
                "personal_avg": d(avg(personal[:-1])),
                "outgoings_per_fortnight": d(outgoings),
                "income_avg_per_fortnight": d(income_avg),
                "income_fortnights_averaged": n26,
                "income_covers_pct": round(income_avg / outgoings * 100) if outgoings else None,
            },
            "savers": savers,
            "outside_categories": categories,
            "outside_purchases": [{
                "date": r["local_date"], "merchant": r["description"], "category": names.get(r["category"], r["category"]),
                "amount": d(r["amt"]), "message": r["message"],
                "paid_from": "reserve" if rid and r["covered_by_id"] == rid else "no saver",
            } for r in purchases],
        }

    def transactions(self, q: str = "", month: str = "", category: str = "",
                     kind: str = "", limit: int = 200, offset: int = 0) -> list[dict]:
        where, params = ["1=1"], []
        if q:
            where.append("(description LIKE ? OR raw_text LIKE ? OR message LIKE ? OR note LIKE ?)")
            params += [f"%{q}%"] * 4
        if month:
            where.append("month = ?")
            params.append(month)
        if category:
            where.append("(category = ? OR parent_category = ?)")
            params += [category, category]
        if kind:
            where.append("kind = ?")
            params.append(kind)
        params += [max(1, min(limit, 2000)), max(0, offset)]
        rows = self._q(
            f"""SELECT id, local_date, description, amount_cents, category, parent_category, kind,
                       status, account_name, message, note FROM flows WHERE {' AND '.join(where)}
                ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            params,
        )
        names = self.category_names()
        for r in rows:
            r["amount"] = d(r.pop("amount_cents"))
            r["category_name"] = names.get(r["category"], r["category"])
        return rows

    def report(self) -> dict:
        """Everything an analysis needs, sized for Home Assistant sensor attributes."""
        return {
            "overview": self.overview(),
            "monthly": self.monthly(13),
            "categories_this_month": self.categories(),
            "budgets": self.budgets_status(),
            "recurring": self.recurring(),
            "top_merchants_90d": self.merchants(90, 20),
            "large_transactions_30d": self.large_transactions(30, 15),
            "new_merchants_30d": self.new_merchants(30),
            "savers": self.savers(),
            "plan": self.plan(),
        }


def _alnum(text: str) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def _gap(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days
