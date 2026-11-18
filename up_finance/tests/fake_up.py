"""A fake Up API producing realistic-looking data, for tests and the local demo."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional

SPENDING = "acc-spending"
SAVER = "acc-saver"
HOLIDAY = "acc-holiday"
DAILY = "acc-daily"        # saver topped up fortnightly; covers groceries and cafes
HOMELOAN = "acc-homeloan"
PERSONAL = "acc-personal"  # individual account: must never be synced in joint scope

CATEGORIES = [
    ("good-life", "Good Life", None), ("restaurants-and-cafes", "Restaurants & Cafes", "good-life"),
    ("takeaway", "Takeaway", "good-life"), ("tv-and-music", "TV, Music & Streaming", "good-life"),
    ("home", "Home", None), ("groceries", "Groceries", "home"), ("utilities", "Utilities", "home"),
    ("personal", "Personal", None), ("clothing-and-accessories", "Clothing & Accessories", "personal"),
    ("mobile-phone", "Mobile Phone", "personal"), ("technology", "Technology", "personal"),
    ("transport", "Transport", None), ("fuel", "Fuel", "transport"), ("public-transport", "Public Transport", "transport"),
]
PARENT = {c: p for c, _, p in CATEGORIES}


def _money(cents: int) -> dict:
    return {"currencyCode": "AUD", "value": f"{cents / 100:.2f}", "valueInBaseUnits": cents}


def make_txn(tid: str, when: datetime, desc: str, cents: int, account: str = SPENDING,
             category: Optional[str] = None, transfer: Optional[str] = None, status: str = "SETTLED",
             message: Optional[str] = None) -> dict:
    rel = {
        "account": {"data": {"type": "accounts", "id": account}},
        "transferAccount": {"data": {"type": "accounts", "id": transfer} if transfer else None},
        "category": {"data": {"type": "categories", "id": category} if category else None},
        "parentCategory": {"data": {"type": "categories", "id": PARENT[category]} if category else None},
        "tags": {"data": []},
    }
    return {
        "type": "transactions", "id": tid,
        "attributes": {
            "status": status, "rawText": desc.upper(), "description": desc, "message": message,
            "isCategorizable": True, "holdInfo": None, "roundUp": None, "cashback": None,
            "amount": _money(cents), "foreignAmount": None, "cardPurchaseMethod": None,
            "settledAt": when.isoformat() if status == "SETTLED" else None,
            "createdAt": when.isoformat(), "transactionType": None, "note": None,
        },
        "relationships": rel,
    }


def generate(today: date, months: int = 14, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    tz = timezone(timedelta(hours=11))
    start = today - timedelta(days=months * 30)
    out: list[dict] = []
    n = 0

    def add(day: date, *args, hour: int = 12, minute: Optional[int] = None, **kw):
        nonlocal n
        n += 1
        minute = rnd.randint(0, 59) if minute is None else minute
        when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        out.append(make_txn(f"txn-{n:05d}", when, *args, **kw))

    d = start
    while d <= today:
        if d.weekday() == 5:
            amt = rnd.randint(9000, 16000)
            mins = rnd.randint(0, 58)
            add(d, "Woolworths", -amt, category="groceries", hour=10, minute=mins)
            add(d, "Cover from Daily Expenses", amt, transfer=DAILY, hour=10, minute=mins + 1)
            add(d, "Cover to 2Up Spending", -amt, account=DAILY, transfer=SPENDING, hour=10, minute=mins + 1)
        if d.weekday() == 0 and ((d - start).days // 7) % 2 == 0:
            add(d, "Auto Transfer to Daily Expenses", -60000, transfer=DAILY, hour=6)
            add(d, "Auto Transfer from 2Up Spending", 60000, account=DAILY, transfer=SPENDING, hour=6)
        if d.weekday() in (0, 2, 4) and rnd.random() < 0.8:
            add(d, rnd.choice(["Seven Seeds", "Proud Mary", "Market Lane"]), -rnd.randint(450, 1400),
                category="restaurants-and-cafes", hour=8)
        if d.weekday() == 4 and rnd.random() < 0.6:
            add(d, "Uber Eats", -rnd.randint(2500, 6000), category="takeaway", hour=19)
        if rnd.random() < 0.12:
            add(d, "Ampol", -rnd.randint(5000, 9000), category="fuel")
        if d.weekday() < 5 and rnd.random() < 0.5:
            add(d, "Transport for NSW", -rnd.randint(300, 900), category="public-transport", hour=8)
        if d.day == 3:
            add(d, "Netflix", -2299, category="tv-and-music")
        if d.day == 9:
            add(d, "Spotify", -1399 if d < today - timedelta(days=120) else -1499, category="tv-and-music")
        if d.day == 15:
            add(d, "Telstra", -6500, category="mobile-phone")
        if d.day == 28:
            add(d, "Interest", -150000, account=HOMELOAN)
        if d.day == 1:
            add(d, "Rent - J Smith", -220000, message="Rent")
            add(d, "Transfer to Saver", -50000, transfer=SAVER)
            add(d, "Transfer from Spending", 50000, account=SAVER, transfer=SPENDING)
            add(d, "Transfer to Personal", -30000, transfer=PERSONAL, message="Allowance")
            add(d, "Transfer from 2Up", 30000, account=PERSONAL, transfer=SPENDING)
            add(d, "Secret Personal Shop", -4200, account=PERSONAL, category="clothing-and-accessories")
        if d.day == 20 and d.month % 3 == 0:
            add(d, "Origin Energy", -rnd.randint(28000, 34000), category="utilities")
        # fortnightly salary on Thursdays
        if d.weekday() == 3 and ((d - start).days // 7) % 2 == 0:
            add(d, "ACME PTY LTD", 310000, message="Salary")
        if rnd.random() < 0.03:
            add(d, rnd.choice(["Uniqlo", "JB Hi-Fi", "Kmart"]), -rnd.randint(3000, 45000),
                category=rnd.choice(["clothing-and-accessories", "technology"]))
        d += timedelta(days=1)

    add(start, "Drawdown", -50000000, account=HOMELOAN)
    add(today - timedelta(days=6), "Apple Store", -189900, category="technology")
    add(today - timedelta(days=2), "Bunnings", -8650, category=None, status="HELD")
    add(today - timedelta(days=4), "New Cafe Co", -850, category="restaurants-and-cafes")
    out.sort(key=lambda t: t["attributes"]["createdAt"], reverse=True)
    return out


class FakeUpClient:
    """Implements the UpClient methods the syncer uses."""

    def __init__(self, today: date, months: int = 14):
        self.today = today
        self.txns = generate(today, months)
        self.balances = {SPENDING: 342150, SAVER: 1250000, HOLIDAY: 210000, PERSONAL: 55500,
                         DAILY: 12000, HOMELOAN: -49000000}
        self.calls: list[Optional[str]] = []
        self.account_calls: list[Optional[str]] = []

    def accounts(self) -> list[dict]:
        names = {SPENDING: ("Spending", "TRANSACTIONAL", "JOINT"), SAVER: ("Emergency Fund", "SAVER", "JOINT"),
                 HOLIDAY: ("Holiday", "SAVER", "JOINT"), PERSONAL: ("Personal", "TRANSACTIONAL", "INDIVIDUAL"),
                 DAILY: ("🛒 Daily Expenses", "SAVER", "JOINT"), HOMELOAN: ("🏠 Home loan", "HOME_LOAN", "JOINT")}
        return [{"type": "accounts", "id": aid, "attributes": {
            "displayName": n, "accountType": t, "ownershipType": o,
            "balance": _money(self.balances[aid]), "createdAt": "2023-01-01T10:00:00+11:00"}}
            for aid, (n, t, o) in names.items()]

    def categories(self) -> list[dict]:
        return [{"type": "categories", "id": cid, "attributes": {"name": name},
                 "relationships": {"parent": {"data": {"type": "categories", "id": p} if p else None}}}
                for cid, name, p in CATEGORIES]

    def transactions(self, since: Optional[str] = None, account_id: Optional[str] = None):
        self.calls.append(since)
        self.account_calls.append(account_id)
        txns = [t for t in self.txns
                if account_id is None or t["relationships"]["account"]["data"]["id"] == account_id]
        if not since:
            return iter(txns)
        s = datetime.fromisoformat(since)
        return iter([t for t in txns if datetime.fromisoformat(t["attributes"]["createdAt"]) >= s])
