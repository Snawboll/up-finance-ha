"""Link Up "Cover from <saver>" transfers to the purchases they paid for.

Up records a cover as a transfer from the saver into the spending account, but
doesn't say which purchase it was for. Two passes:

1. auto   - a purchase on the same account, for exactly the cover amount, within
            10 minutes either side (Up's automatic covers land within seconds).
2. manual - a purchase for exactly the cover amount in the 14 days before the
            cover (covers you add later by hand).

Each purchase is linked to at most one cover. Covers with no match are kept with
spend_id NULL so their money is still counted against the saver.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime

AUTO_WINDOW_S = 10 * 60
MANUAL_WINDOW_S = 14 * 24 * 3600


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def link_covers(conn: sqlite3.Connection) -> dict:
    covers = conn.execute(
        "SELECT id, account_id, amount_cents, created_at, transfer_account_id FROM transactions "
        "WHERE transfer_account_id IS NOT NULL AND amount_cents > 0 AND description LIKE 'Cover%from%' "
        "ORDER BY created_at").fetchall()
    spends = conn.execute(
        "SELECT id, account_id, amount_cents, created_at FROM transactions "
        "WHERE transfer_account_id IS NULL AND amount_cents < 0").fetchall()

    by_key: dict[tuple, list[tuple[float, str]]] = defaultdict(list)
    for s in spends:
        by_key[(s["account_id"], -s["amount_cents"])].append((_ts(s["created_at"]), s["id"]))

    used: set[str] = set()
    links: dict[str, tuple[str | None, str | None]] = {}

    def best(cover, lo: float, hi: float):
        t = _ts(cover["created_at"])
        cands = [(abs(st - t), sid) for st, sid in by_key.get((cover["account_id"], cover["amount_cents"]), [])
                 if sid not in used and lo <= st - t <= hi]
        return min(cands)[1] if cands else None

    for c in covers:  # pass 1: automatic covers
        sid = best(c, -AUTO_WINDOW_S, AUTO_WINDOW_S)
        if sid:
            used.add(sid)
            links[c["id"]] = (sid, "auto")
    for c in covers:  # pass 2: manual covers of earlier purchases
        if c["id"] in links:
            continue
        sid = best(c, -MANUAL_WINDOW_S, 0)
        if sid:
            used.add(sid)
        links[c["id"]] = (sid, "manual" if sid else None)

    conn.execute("DELETE FROM cover_links")
    conn.executemany(
        "INSERT INTO cover_links(cover_id, spend_id, saver_id, amount_cents, match_type) VALUES (?,?,?,?,?)",
        [(c["id"], links[c["id"]][0], c["transfer_account_id"], c["amount_cents"], links[c["id"]][1])
         for c in covers],
    )
    matched = sum(1 for v in links.values() if v[0])
    return {"covers": len(covers), "matched": matched}
