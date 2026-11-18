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
                 large_threshold: float = 200.0, today: Optional[date] = None):
        self.db = db
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
            topups = self._q(
                "SELECT local_date, amount_cents FROM transactions WHERE account_id=? AND amount_cents>0 "
                "AND transfer_account_id IS NOT NULL AND description LIKE 'Auto Transfer%' "
                "ORDER BY created_at DESC LIMIT 8", (aid,))
            cadence, per_year = None, 0
            if len(topups) >= 2:
                gaps = [_gap(b["local_date"], a["local_date"]) for a, b in zip(topups, topups[1:])]
                med = statistics.median(gaps)
                c = next((c for c in CADENCES if c[1] <= med <= c[2]), None)
                if c:
                    cadence, per_year = c[0], c[3]
            topup = topups[0]["amount_cents"] if topups and cadence else None
            if topups and (t - date.fromisoformat(topups[0]["local_date"])).days > 45:
                topup, cadence = None, None  # auto top-up has stopped
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
        }


def _gap(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days
