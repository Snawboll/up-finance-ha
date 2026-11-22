"""Publish metrics to Home Assistant as sensor entities via the REST API.

Inside the add-on we use the Supervisor proxy (http://supervisor/core/api) with
SUPERVISOR_TOKEN. For local development set HA_URL (e.g. http://homeassistant.local:8123)
and HA_TOKEN (a long-lived access token).

States created this way disappear when Home Assistant restarts, so the add-on
checks a canary entity every minute and republishes if it has gone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Optional

from .metrics import Metrics
from .reconcile import Reconciler

log = logging.getLogger(__name__)

PREFIX = "sensor.up_"
CANARY = "sensor.up_last_sync"
CURRENCY = "AUD"
ATTRIBUTION = "Data from Up Bank via up-finance-ha"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unknown"


class HomeAssistant:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None):
        sup_token = os.environ.get("SUPERVISOR_TOKEN")
        if url is None and sup_token:
            url, token = "http://supervisor/core/api", sup_token
        elif url is None and os.environ.get("HA_URL"):
            url, token = os.environ["HA_URL"].rstrip("/") + "/api", os.environ.get("HA_TOKEN")
        self.url = url
        self.token = token
        self._last_hash: dict[str, str] = {}
        self.reconciler: Optional[Reconciler] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def canary_missing(self) -> bool:
        if not self.enabled:
            return False
        try:
            return self._request("GET", f"/states/{CANARY}") is None
        except Exception as e:  # HA restarting etc.
            log.debug("Canary check failed: %s", e)
            return False

    def get_state(self, entity_id: str) -> Optional[str]:
        """The state of an entity, or None if it doesn't exist or HA can't be reached."""
        if not self.enabled or not entity_id:
            return None
        try:
            r = self._request("GET", f"/states/{entity_id}")
        except Exception as e:
            log.debug("Reading %s failed: %s", entity_id, e)
            return None
        return r.get("state") if r else None

    def set_state(self, entity_id: str, state, attributes: dict, force: bool = False) -> None:
        payload = {"state": str(state) if state is not None else "unknown", "attributes": attributes}
        h = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if not force and self._last_hash.get(entity_id) == h:
            return
        self._request("POST", f"/states/{entity_id}", payload)
        self._last_hash[entity_id] = h

    def publish(self, m: Metrics, force: bool = False) -> int:
        if not self.enabled:
            log.info("Home Assistant API not configured; skipping sensor publish")
            return 0
        if force:
            self._last_hash.clear()
        sensors = build_sensors(m, self.reconciler)
        for entity_id, (state, attrs) in sensors.items():
            self.set_state(entity_id, state, attrs)
        log.info("Published %d sensors to Home Assistant", len(sensors))
        return len(sensors)


def _money(name: str, value: float, icon: str = "mdi:cash", **extra) -> tuple:
    attrs = {
        "friendly_name": name,
        "unit_of_measurement": CURRENCY,
        "device_class": "monetary",
        "state_class": "total",
        "icon": icon,
        "attribution": ATTRIBUTION,
    }
    attrs.update(extra)
    return round(value, 2), attrs


def _info(name: str, state, icon: str, **extra) -> tuple:
    attrs = {"friendly_name": name, "icon": icon, "attribution": ATTRIBUTION}
    attrs.update(extra)
    return state, attrs


def reconcile_sensors(rec: Reconciler) -> dict[str, tuple]:
    r = rec.report()
    if not r.get("enabled"):
        return {}
    s: dict[str, tuple] = {}
    items = [{k: i[k] for k in ("date", "payee", "amount", "action", "suggested", "note", "status")}
             for i in r["items"]]
    s[PREFIX + "reconcile"] = _info(
        "Up payments to reconcile", r["count"], "mdi:scale-balance",
        unit_of_measurement="items", state_class="measurement",
        account=r["account"], balance=r["balance"], float_target=r["float_target"], gap=r["gap"],
        since=r["since"], since_source=r["since_source"], totals=r["totals"], net_effect=r["net_effect"],
        projected_balance=r["projected_balance"], off_by=r["off_by"], items=items,
        items_truncated=r["items_truncated"])
    if r["float_target"]:
        s[PREFIX + "float_gap"] = _money(
            f"Up {r['account']} float gap", r["gap"], "mdi:wallet-plus",
            float_target=r["float_target"], balance=r["balance"], off_by_after_reconcile=r["off_by"])
    return s


def build_sensors(m: Metrics, rec: Optional[Reconciler] = None) -> dict[str, tuple]:
    ov = m.overview()
    monthly = m.monthly(13)
    cats = m.categories()
    budgets = m.budgets_status()
    recurring = m.recurring()
    merchants = m.merchants(90, 20)
    new_merchants = m.new_merchants(30)
    large = m.large_transactions(30, 15)

    s: dict[str, tuple] = {}
    s[CANARY] = _info("Up last sync", ov["last_sync_at"], "mdi:bank-transfer", device_class="timestamp")

    s[PREFIX + "spending_this_month"] = _money(
        "Up spending this month", ov["spent_this_month"], "mdi:cart",
        last_month_to_same_day=ov["spent_last_month_to_same_day"],
        projected=ov["projected_this_month"], month=ov["month"],
    )
    s[PREFIX + "spending_last_month"] = _money("Up spending last month", ov["spent_last_month"], "mdi:cart-outline")
    s[PREFIX + "projected_spending_this_month"] = _money(
        "Up projected spending this month", ov["projected_this_month"], "mdi:chart-line")
    s[PREFIX + "income_this_month"] = _money("Up income this month", ov["income_this_month"], "mdi:cash-plus")
    s[PREFIX + "income_last_month"] = _money("Up income last month", ov["income_last_month"], "mdi:cash-plus")
    s[PREFIX + "allowances_this_month"] = _money(
        "Up personal allowances this month", ov["allowances_this_month"], "mdi:account-cash",
        last_month=ov["allowances_last_month"])
    s[PREFIX + "net_last_month"] = _money("Up net last month", ov["net_last_month"], "mdi:scale-balance")
    s[PREFIX + "savings_rate_last_month"] = _info(
        "Up savings rate last month", ov["savings_rate_last_month_pct"], "mdi:piggy-bank-outline",
        unit_of_measurement="%", state_class="measurement")

    s[PREFIX + "total_balance"] = _money("Up total balance", ov["total_balance"], "mdi:bank")
    s[PREFIX + "total_savers"] = _money("Up total savers", ov["total_savers"], "mdi:piggy-bank")
    for a in ov["accounts"]:
        s[PREFIX + "balance_" + slug(a["name"])] = _money(
            f"Up {a['name']}", a["balance"], "mdi:piggy-bank" if a["type"] == "SAVER" else "mdi:credit-card",
            account_type=a["type"], ownership=a["ownership"])

    # Spending this month per parent category (Good Life, Home, Personal, Transport...)
    parents: dict[str, list] = {}
    for c in cats:
        parents.setdefault(c["parent"], [c["parent_name"], 0.0])[1] += c["spent"]
    for p in ("good-life", "home", "personal", "transport"):
        parents.setdefault(p, [p.replace("-", " ").title(), 0.0])
    for pid, (pname, total) in parents.items():
        s[PREFIX + "spending_" + slug(pid)] = _money(
            f"Up {pname} this month", total, "mdi:shape",
            categories={c["name"]: c["spent"] for c in cats if c["parent"] == pid and c["spent"]})

    for b in budgets:
        s[PREFIX + "budget_" + slug(b["category"])] = _money(
            f"Up budget {b['name']}", b["spent"], "mdi:target",
            budget=b["budget"], remaining=b["remaining"], projected=b["projected"],
            used_pct=b["used_pct"], pace_pct=b["pace_pct"])

    savers = m.savers()
    for sv in savers:
        s[PREFIX + "saver_" + slug(sv["saver"])] = _money(
            f"Up {sv['saver'].strip()} paid out this month", sv["covered_this_month"], "mdi:wallet",
            balance=sv["balance"], top_up=sv["top_up"], top_up_cadence=sv["top_up_cadence"],
            monthly_budget=sv["monthly_budget"], paid_out_avg_3m=sv["covered_avg_3m"],
            budget_used_avg_pct=sv["budget_used_avg_pct"],
            top_categories_90d={c["category"]: c["amount"] for c in sv["covered_categories_90d"][:5]})
    s[PREFIX + "report_savers"] = _info(
        "Up report savers", len(savers), "mdi:wallet-outline",
        savers=[{k: sv[k] for k in ("saver", "balance", "balance_90d_ago", "top_up", "top_up_cadence",
                                     "monthly_budget", "covered_this_month", "covered_avg_3m",
                                     "budget_used_avg_pct", "months", "covered_by_month", "funded_by_month",
                                     "covered_categories_90d")} for sv in savers])
    plan = m.plan()
    sm = plan["summary"]
    s[PREFIX + "outgoings_per_fortnight"] = _money(
        "Up outgoings per fortnight", sm["outgoings_per_fortnight"], "mdi:calendar-sync",
        plan=sm["plan_per_fortnight"], outside_plan_avg=sm["outside_plan_avg"], personal_avg=sm["personal_avg"],
        income_avg=sm["income_avg_per_fortnight"], income_covers_pct=sm["income_covers_pct"])
    s[PREFIX + "outside_plan_this_fortnight"] = _money(
        "Up outside the plan this fortnight", plan["outside_plan"][-1], "mdi:cart-arrow-right",
        fortnight_start=plan["current"]["start"], fortnight_end=plan["current"]["end"],
        average=sm["outside_plan_avg"],
        top_categories={c["name"]: c["this_fortnight"] for c in plan["outside_categories"] if c["this_fortnight"]})
    if plan["reserve"]:
        r = plan["reserve"]
        s[PREFIX + "reserve_balance"] = _money(
            "Up reserve balance", r["balance"], "mdi:safe", saver=r["saver"],
            start_of_fortnight=r["balance_at_start_of_fortnight"], runway_fortnights=r["runway_fortnights"])
        s[PREFIX + "reserve_runway"] = _info(
            "Up reserve runway", r["runway_fortnights"], "mdi:timer-sand",
            unit_of_measurement="fortnights", state_class="measurement",
            outgoings_per_fortnight=sm["outgoings_per_fortnight"])
    s[PREFIX + "report_plan"] = _info(
        "Up report plan", plan["current"]["start"], "mdi:calendar-check",
        summary=sm, fortnights=[f["start"] for f in plan["fortnights"]], reserve=plan["reserve"],
        income=plan["income"], into_savers=plan["into_savers"], outside_plan=plan["outside_plan"],
        personal=plan["personal"],
        savers=[{k: sv[k] for k in ("saver", "role", "per_fortnight", "paid_out_avg", "balance", "balance_at_start")}
                for sv in plan["savers"]],
        outside_categories=[{k: c[k] for k in ("name", "parent_name", "per_fortnight", "this_fortnight")}
                            for c in plan["outside_categories"][:12]])
    if ov.get("home_loan_balance") is not None:
        s[PREFIX + "home_loan_owing"] = _money("Up home loan owing", ov["home_loan_balance"], "mdi:home-percent")

    # Report sensors: compact data blocks for Claude / dashboards to read.
    s[PREFIX + "report_monthly"] = _info(
        "Up report monthly", monthly["months"][-1], "mdi:calendar-month",
        months=monthly["months"], total_spending=monthly["total_spending"], income=monthly["income"],
        allowances=monthly["allowances"],
        by_parent_category={x["name"]: x["values"] for x in monthly["spending"]})
    s[PREFIX + "report_categories"] = _info(
        "Up report categories", ov["month"], "mdi:format-list-bulleted",
        categories=[{k: c[k] for k in ("name", "parent_name", "spent", "count", "avg_6m", "budget")} for c in cats])
    active = [r for r in recurring if r["active"]]
    s[PREFIX + "report_recurring"] = _info(
        "Up recurring payments", len(active), "mdi:autorenew",
        annual_total=round(sum(r["annual_cost"] for r in active), 2),
        recurring=[{k: r[k] for k in ("merchant", "cadence", "typical_amount", "last_amount", "last_date",
                                       "next_expected", "annual_cost", "price_changed")} for r in active],
        lapsed=[r["merchant"] for r in recurring if not r["active"]])
    s[PREFIX + "report_merchants"] = _info(
        "Up top merchants 90d", len(merchants), "mdi:store", top_merchants_90d=merchants,
        new_merchants_30d=new_merchants)
    s[PREFIX + "report_large_transactions"] = _info(
        "Up large transactions 30d", len(large), "mdi:alert-circle-outline",
        threshold=m.large_threshold, transactions=large)
    if rec is not None:
        try:
            s.update(reconcile_sensors(rec))
        except Exception as e:  # never let reconciling stop the other sensors
            log.warning("Reconcile sensors failed: %s", e)
    return s
