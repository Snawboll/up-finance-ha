"""Payee rules: re-classify transactions Up leaves uncategorised or gets wrong.

A rule matches on the payee (description) and, optionally, the transaction message:

    - payee: Old Bank Account        # case-insensitive substring of the description
      message: car (re)?payment      # optional case-insensitive regex on the message/note
      category: car-loan             # Up category id, or your own (e.g. childcare)
      parent: transport              # parent group for your own categories (optional)
      treat_as: spending             # spending | income | transfer (default: spending if
                                     # a category is given, otherwise unchanged)

The first matching rule wins. Rules never touch transfers between synced accounts.
`ignore_payees: [X]` is shorthand for `{payee: X, treat_as: transfer}`.
Results are stored in `txn_overrides`, rebuilt after every sync and at start-up.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

KINDS = ("spending", "income", "transfer")


@dataclass
class Rule:
    payee: str
    message: Optional[re.Pattern] = None
    category: Optional[str] = None
    parent: Optional[str] = None
    treat_as: Optional[str] = None

    def matches(self, description: str, *texts: Optional[str]) -> bool:
        """texts: the transaction's message and note; the pattern may match either."""
        if self.payee.lower() not in (description or "").lower():
            return False
        return self.message is None or any(self.message.search(t.strip()) for t in texts if t)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def parse_rules(raw_rules, ignore_payees=()) -> list[Rule]:
    rules: list[Rule] = []
    for r in raw_rules or []:
        payee = str(r.get("payee") or "").strip()
        if not payee:
            continue
        msg = r.get("message")
        try:
            pattern = re.compile(str(msg), re.IGNORECASE) if msg else None
        except re.error as e:
            log.error("Ignoring payee rule for %r: bad message pattern (%s)", payee, e)
            continue
        treat_as = r.get("treat_as") or None
        if treat_as and treat_as not in KINDS:
            log.error("Ignoring payee rule for %r: treat_as must be one of %s", payee, KINDS)
            continue
        category = slugify(str(r["category"])) if r.get("category") else None
        rules.append(Rule(payee, pattern, category,
                          slugify(str(r["parent"])) if r.get("parent") else None,
                          treat_as or ("spending" if category else None)))
    for p in ignore_payees or []:
        if str(p).strip():
            rules.append(Rule(str(p).strip(), treat_as="transfer"))
    return rules


def match_rules(conn: sqlite3.Connection, rules: list[Rule]) -> list[tuple[str, int, Rule, Optional[str]]]:
    """(transaction_id, rule_index, rule, parent_category) for every transaction a rule matches."""
    if not rules:
        return []
    known_parents = {r[0]: r[1] for r in conn.execute("SELECT id, parent_id FROM categories")}
    rows = conn.execute(
        "SELECT id, description, message, note FROM transactions WHERE transfer_account_id IS NULL").fetchall()
    out = []
    for row in rows:
        idx = next((i for i, r in enumerate(rules) if r.matches(row["description"], row["message"], row["note"])),
                   None)
        if idx is None:
            continue
        rule = rules[idx]
        parent = None
        if rule.category:
            parent = rule.parent or known_parents.get(rule.category) or (
                rule.category if rule.category in known_parents else "uncategorised")
        out.append((row["id"], idx, rule, parent))
    return out


def apply_rules(conn: sqlite3.Connection, rules: list[Rule]) -> int:
    conn.execute("DELETE FROM txn_overrides")
    matches = match_rules(conn, rules)
    conn.executemany(
        "INSERT INTO txn_overrides(transaction_id, kind, category, parent_category) VALUES (?,?,?,?)",
        [(tid, rule.treat_as, rule.category, parent) for tid, _, rule, parent in matches])
    return len(matches)


def preview_rules(conn: sqlite3.Connection, rules: list[Rule], samples: int = 5) -> list[dict]:
    """What each rule would match, without changing anything."""
    matches = match_rules(conn, rules)
    by_rule: dict[int, list[str]] = {}
    for tid, idx, _, _ in matches:
        by_rule.setdefault(idx, []).append(tid)
    out = []
    for i, rule in enumerate(rules):
        ids = by_rule.get(i, [])
        info = {"rule": rule_to_dict(rule), "matched": len(ids), "total": 0.0, "first": None, "last": None,
                "examples": []}
        if ids:
            marks = ",".join("?" * len(ids))
            agg = conn.execute(f"SELECT SUM(amount_cents), MIN(local_date), MAX(local_date) FROM transactions "
                               f"WHERE id IN ({marks})", ids).fetchone()
            info.update(total=round((agg[0] or 0) / 100, 2), first=agg[1], last=agg[2])
            info["examples"] = [dict(r) for r in conn.execute(
                f"SELECT local_date AS date, description, message, amount_cents/100.0 AS amount FROM transactions "
                f"WHERE id IN ({marks}) ORDER BY ABS(amount_cents) DESC LIMIT ?", ids + [samples])]
        out.append(info)
    return out


def rule_to_dict(rule: Rule) -> dict:
    d = {"payee": rule.payee}
    if rule.message:
        d["message"] = rule.message.pattern
    if rule.category:
        d["category"] = rule.category
    if rule.parent:
        d["parent"] = rule.parent
    if rule.treat_as and not (rule.category and rule.treat_as == "spending"):
        d["treat_as"] = rule.treat_as
    return d
