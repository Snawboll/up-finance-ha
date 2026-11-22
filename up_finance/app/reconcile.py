"""Reconcile the spending account back to its float.

Many Up households keep a fixed float in the spending account (say $1,000) and pay for
everything from savers with Cover. Over a fortnight a few things slip through. This
module lists each one and the action that puts the account back on its float:

  cover     a purchase nobody covered from a saver                (money in when fixed)
  forward   money that came in and should move on: income, rebates
            and refunds                                             (money out when fixed)
  return    a cover with no purchase to match, e.g. a double cover (money out when fixed)
  transfer  a regular automatic transfer that didn't run this cycle (money out when fixed)

Items count from the last time you marked the account as reconciled (POST
/api/reconcile/mark or the Home Assistant button). With no mark, they count from the end
of the last day the balance was at or above the float.

An item is treated as done when a later transaction settles it:
  - a cover for the same amount (covers.py links those), or one cover for the exact sum
    of several purchases;
  - a transfer out for the same amount or for the exact sum of several items. Missed
    automatic transfers are settled by transfers to the same account; money to forward
    or return by transfers to any saver.

Everything is in cents internally; the report is in dollars.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .db import Database

LOOKBACK_DAYS = 120       # history used to find cycles and the auto start point
COVER_SUM_WINDOW = 60     # a lump cover can pay for purchases up to this many days earlier
SETTLE_WINDOW = 45        # a transfer out can settle items up to this many days earlier
CYCLE_HISTORY = 4         # an automatic transfer is expected if it ran in >= 3 of the last 4 cycles
CYCLE_MIN_HITS = 3
MAX_SUBSET_ITEMS = 24
MAX_ITEMS_REPORTED = 60


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _d(cents: int) -> float:
    return round(cents / 100, 2)


def _alnum(text: str) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def subset_sum(target: int, items: list[tuple[str, int]], max_states: int = 200_000) -> Optional[list[str]]:
    """Ids of items whose amounts (positive cents) add up to exactly target, or None.

    Prefers the earliest items (they are passed oldest first) and a single exact match.
    """
    if target <= 0:
        return None
    for iid, amt in items:
        if amt == target:
            return [iid]
    items = [(i, a) for i, a in items if 0 < a < target][:MAX_SUBSET_ITEMS]
    reach: dict[int, tuple[int, int]] = {0: (-1, -1)}  # sum -> (item index, previous sum)
    for idx, (_, amt) in enumerate(items):
        for s in list(reach):
            n = s + amt
            if n <= target and n not in reach:
                reach[n] = (idx, s)
                if n == target:
                    out, cur = [], n
                    while cur:
                        i, prev = reach[cur]
                        out.append(items[i][0])
                        cur = prev
                    return list(reversed(out))
        if len(reach) > max_states:
            return None
    return None


class Reconciler:
    def __init__(self, db: Database, tz: ZoneInfo, float_target: float = 0.0, account: str = "",
                 reserve_saver: str = "Savings", today: Optional[date] = None):
        self.db = db
        self.tz = tz
        self.float_cents = int(round((float_target or 0) * 100))
        self.account = account or ""
        self.reserve_saver = reserve_saver or ""
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

    # -- set-up ----------------------------------------------------------------
    def _spending_account(self) -> Optional[dict]:
        accts = self._q("SELECT * FROM accounts WHERE type='TRANSACTIONAL' ORDER BY ownership='JOINT' DESC, name")
        want = _alnum(self.account)
        if want:
            match = [a for a in accts if _alnum(a["name"]) == want] or [a for a in accts if want in _alnum(a["name"])]
            if match:
                return match[0]
        return accts[0] if accts else None

    def _reserve_name(self) -> Optional[str]:
        want = _alnum(self.reserve_saver)
        if not want:
            return None
        savers = self._q("SELECT name FROM accounts WHERE type='SAVER'")
        exact = [s["name"] for s in savers if _alnum(s["name"]) == want]
        return (exact or [s["name"] for s in savers if want in _alnum(s["name"])] or [None])[0]

    def _start(self, txns: list[dict], balance: int) -> tuple[float, str]:
        """Timestamp after which items count, and how it was chosen."""
        marked = self.db.get_state("reconciled_at")
        if marked:
            return _ts(marked), "marked"
        fallback = datetime.combine(self.today - timedelta(days=30), time.min, self.tz).timestamp()
        if self.float_cents <= 0 or not txns:
            return fallback, "last 30 days"
        # Walk back from today's balance; find the last day that ended at or above the float.
        bal = balance
        eod: dict[str, int] = {}
        for t in sorted(txns, key=lambda t: (t["_ts"], t["id"]), reverse=True):
            eod.setdefault(t["local_date"], bal)
            bal -= t["amount_cents"]
        for day in sorted(eod, reverse=True):
            if eod[day] >= self.float_cents and day < self.today.isoformat():
                end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time.min, self.tz)
                return end.timestamp(), "auto"
        return fallback, "last 30 days"

    def _saver_guesses(self) -> tuple[dict[str, str], dict[str, str]]:
        """Which saver usually covers each merchant, and each category."""
        rows = self._q(
            "SELECT description, category, covered_by FROM flows WHERE covered_by IS NOT NULL "
            "AND kind='spending' AND local_date >= ?", ((self.today - timedelta(days=365)).isoformat(),))
        by_desc: dict[str, Counter] = defaultdict(Counter)
        by_cat: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            by_desc[_alnum(r["description"])][r["covered_by"]] += 1
            by_cat[r["category"]][r["covered_by"]] += 1
        return ({k: c.most_common(1)[0][0] for k, c in by_desc.items()},
                {k: c.most_common(1)[0][0] for k, c in by_cat.items()})

    # -- the report ------------------------------------------------------------
    def report(self) -> dict:
        acct = self._spending_account()
        if not acct:
            return {"enabled": False, "reason": "No spending account synced yet", "items": [], "count": 0}
        aid = acct["id"]
        balance = acct["balance_cents"] or 0
        lookback = (self.today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        txns = self._q(
            "SELECT f.*, cl.spend_id AS cover_spend_id, "
            "(SELECT 1 FROM cover_links c WHERE c.spend_id = f.id) AS is_covered, "
            "(SELECT name FROM accounts x WHERE x.id = f.transfer_account_id) AS transfer_name "
            "FROM flows f LEFT JOIN cover_links cl ON cl.cover_id = f.id "
            "WHERE f.account_id = ? AND f.local_date >= ?", (aid, lookback))
        for t in txns:
            t["_ts"] = _ts(t["created_at"])
        txns.sort(key=lambda t: (t["_ts"], t["id"]))
        start, start_source = self._start(txns, balance)
        after = [t for t in txns if t["_ts"] > start]

        reserve = self._reserve_name()
        by_desc, by_cat = self._saver_guesses()
        items: list[dict] = []

        def add(t: dict, action: str, cents: int, effect: int, suggested: Optional[str], note: str = "",
                payee: Optional[str] = None, extra: Optional[dict] = None) -> dict:
            item = {"id": t["id"], "date": t["local_date"], "_ts": t["_ts"], "payee": payee or t["description"],
                    "amount": _d(cents), "_cents": cents, "effect": _d(effect), "action": action,
                    "suggested": suggested, "status": t.get("status"), "category": t.get("category"),
                    "note": note}
            item.update(extra or {})
            items.append(item)
            return item

        def is_cover(t):
            return t["amount_cents"] > 0 and t["transfer_account_id"] and \
                str(t["description"]).lower().startswith("cover") and " from " in str(t["description"]).lower()

        # 1. Purchases with no cover.
        uncovered = []
        for t in after:
            if t["kind"] == "spending" and t["amount_cents"] < 0 and not t["is_covered"]:
                guess = by_desc.get(_alnum(t["description"])) or by_cat.get(t["category"])
                uncovered.append(add(t, "cover", -t["amount_cents"], -t["amount_cents"], guess))

        # 2. Covers with no purchase: first try them as one cover for several purchases.
        spare_covers = []
        for t in after:
            if is_cover(t) and not t["cover_spend_id"]:
                window = t["_ts"] - COVER_SUM_WINDOW * 86400
                cands = [(i["id"], i["_cents"]) for i in uncovered
                         if not i.get("_done") and window <= i["_ts"] <= t["_ts"]]
                hit = subset_sum(t["amount_cents"], cands)
                if hit:
                    for i in uncovered:
                        if i["id"] in hit:
                            i["_done"] = True
                    continue
                saver = t["transfer_name"] or str(t["description"]).split(" from ", 1)[-1]
                spare_covers.append(add(t, "return", t["amount_cents"], -t["amount_cents"], saver,
                                        "Cover with no matching purchase"))

        # 3. Money in to pass on: income, rebates and refunds.
        for t in after:
            if t["transfer_account_id"] or t["amount_cents"] <= 0:
                continue
            if t["kind"] == "income":
                add(t, "forward", t["amount_cents"], -t["amount_cents"], reserve, "Money in")
            elif t["kind"] == "spending":
                guess = by_desc.get(_alnum(t["description"])) or reserve
                add(t, "forward", t["amount_cents"], -t["amount_cents"], guess, "Refund or rebate")

        # 4. Regular automatic transfers that didn't run.
        for miss in self._missed_transfers(txns, start):
            add(miss["txn"], "transfer", miss["cents"], -miss["cents"], miss["name"],
                f"Usual automatic transfer missing from the {miss['cycle']} cycle",
                payee=f"Transfer to {miss['name']}", extra={"_dest": miss["dest"], "id": miss["key"]})

        # 5. Settle items with transfers out made after them.
        outs = [t for t in after if t["amount_cents"] < 0 and t["transfer_account_id"]
                and not str(t["description"]).startswith("Auto Transfer")
                and not str(t["description"]).lower().startswith("cover")]
        # Transfers made within a few minutes of each other can split one item (e.g. pay
        # split between two savers), so each is tried alone and then as a group.
        groups: list[list[dict]] = []
        for o in outs:
            if groups and o["_ts"] - groups[-1][-1]["_ts"] <= 30 * 60:
                groups[-1].append(o)
            else:
                groups.append([o])
        for group in groups:
            used = set()
            for o in group:
                if self._settle(items, -o["amount_cents"], o["_ts"], o["transfer_account_id"],
                                bool(o["transfer_name"])):
                    used.add(o["id"])
            rest = [o for o in group if o["id"] not in used]
            if len(rest) > 1 and all(o["transfer_name"] for o in rest):
                self._settle(items, -sum(o["amount_cents"] for o in rest), rest[-1]["_ts"], None, True)

        pending = [i for i in items if not i.get("_done")]
        pending.sort(key=lambda i: (i["_ts"], i["id"]))
        totals = defaultdict(int)
        for i in pending:
            totals[i["action"]] += i["_cents"]
        net = sum(int(round(i["effect"] * 100)) for i in pending)
        out_items = [{k: v for k, v in i.items() if not k.startswith("_")} for i in pending]
        report = {
            "enabled": True,
            "account": acct["name"],
            "balance": _d(balance),
            "float_target": _d(self.float_cents) if self.float_cents else None,
            "gap": _d(self.float_cents - balance) if self.float_cents else None,
            "since": datetime.fromtimestamp(start, self.tz).isoformat(timespec="minutes"),
            "since_source": start_source,
            "count": len(pending),
            "totals": {k: _d(totals.get(k, 0)) for k in ("cover", "forward", "return", "transfer")},
            "net_effect": _d(net),
            "projected_balance": _d(balance + net),
            "off_by": _d(balance + net - self.float_cents) if self.float_cents else None,
            "items": out_items[:MAX_ITEMS_REPORTED],
            "items_truncated": len(out_items) > MAX_ITEMS_REPORTED,
        }
        return report

    @staticmethod
    def _settle(items: list[dict], amt: int, when: float, dest: Optional[str], to_saver: bool) -> bool:
        """Mark the items a transfer out of `amt` cents settles. True if it settled any."""
        window = when - SETTLE_WINDOW * 86400

        def pick(pred):
            return [(i["id"], i["_cents"]) for i in items
                    if not i.get("_done") and pred(i) and window <= i["_ts"] <= when + 60]

        hit = None
        if dest:
            hit = subset_sum(amt, pick(lambda i: i["action"] == "transfer" and i.get("_dest") == dest))
        if not hit and to_saver:
            hit = subset_sum(amt, pick(lambda i: i["action"] in ("forward", "return")))
        for i in items:
            if hit and i["id"] in hit:
                i["_done"] = True
        return bool(hit)

    def _missed_transfers(self, txns: list[dict], start: float) -> Iterable[dict]:
        """Automatic transfers out that ran in most recent cycles but not in this one."""
        auto = [t for t in txns if str(t["description"]).startswith("Auto Transfer")]
        if not auto:
            return []
        # Cycles: days with automatic transfers, merging days next to each other.
        days = sorted({date.fromisoformat(t["local_date"]) for t in auto})
        cycles: list[list[date]] = []
        for d0 in days:
            if cycles and (d0 - cycles[-1][-1]).days <= 1:
                cycles[-1].append(d0)
            else:
                cycles.append([d0])
        outs = [t for t in txns if t["amount_cents"] < 0 and t["transfer_account_id"]]

        def sent(cycle: list[date]) -> dict[str, list[dict]]:
            lo, hi = cycle[0] - timedelta(days=1), cycle[-1] + timedelta(days=1)
            got: dict[str, list[dict]] = defaultdict(list)
            for t in outs:
                if lo <= date.fromisoformat(t["local_date"]) <= hi:
                    got[t["transfer_account_id"]].append(t)
            return got

        per_cycle = [sent(c) for c in cycles]
        result = []
        for idx, cycle in enumerate(cycles):
            cycle_end = datetime.combine(cycle[-1], time.max, self.tz).timestamp()
            if cycle_end <= start or idx < CYCLE_HISTORY:
                continue
            history = per_cycle[idx - CYCLE_HISTORY:idx]
            counts: Counter = Counter(dest for h in history for dest in h)
            for dest, hits in counts.items():
                if hits < CYCLE_MIN_HITS:
                    continue
                autos = [t for h in history for t in h.get(dest, [])
                         if str(t["description"]).startswith("Auto Transfer")]
                if not autos:
                    continue  # only expect transfers that are normally automatic
                usual = Counter(-t["amount_cents"] for t in autos).most_common(1)[0][0]
                if any(-t["amount_cents"] == usual for t in per_cycle[idx].get(dest, [])):
                    continue
                last = autos[-1]
                name = last["transfer_name"] or str(last["description"]).replace("Auto Transfer to ", "", 1)
                anchor = min((t for t in auto if date.fromisoformat(t["local_date"]) in cycle),
                             key=lambda t: t["_ts"])
                result.append({"txn": anchor, "cents": usual, "name": name, "dest": dest,
                               "cycle": cycle[0].strftime("%-d %b"),
                               "key": f"missed-{dest}-{cycle[0].isoformat()}"})
        return result
