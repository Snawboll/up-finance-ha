"""Add-on entry point.

In the add-on, options come from /data/options.json (written by the Supervisor).
For local development:  python -m app.main --dev   with UP_TOKEN set in the
environment (and optionally DATA_DIR, HA_URL, HA_TOKEN, API_KEY).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database
from .ha import HomeAssistant
from .metrics import Metrics
from .sync import Syncer
from .up_client import UpAuthError, UpClient
from .web import App, serve

log = logging.getLogger("up_finance")

OPTIONS_FILE = Path("/data/options.json")
INGRESS_PORT = 8099
LAN_PORT = 8098


def load_options() -> dict:
    opts = {}
    if OPTIONS_FILE.exists():
        opts = json.loads(OPTIONS_FILE.read_text())
    env_map = {"UP_TOKEN": "up_token", "API_KEY": "api_key", "SYNC_INTERVAL_MINUTES": "sync_interval_minutes",
               "LARGE_TRANSACTION_THRESHOLD": "large_transaction_threshold", "LOG_LEVEL": "log_level",
               "ACCOUNT_SCOPE": "account_scope"}
    for env, key in env_map.items():
        if os.environ.get(env):
            opts[key] = os.environ[env]
    opts.setdefault("sync_interval_minutes", 30)
    opts.setdefault("large_transaction_threshold", 200)
    opts.setdefault("budgets", [])
    opts.setdefault("log_level", "info")
    opts.setdefault("account_scope", "joint")
    opts.setdefault("ignore_payees", [])
    return opts


def parse_budgets(raw) -> dict[str, float]:
    out = {}
    for b in raw or []:
        cat = str(b.get("category", "")).strip().lower()
        if cat and b.get("monthly"):
            out[cat] = float(b["monthly"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true", help="local mode: dashboard on 127.0.0.1 without auth")
    parser.add_argument("--once", action="store_true", help="sync once, print a summary and exit")
    args = parser.parse_args()

    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, str(opts["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tz = ZoneInfo(os.environ.get("TZ") or "Australia/Sydney")
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    db = Database(data_dir / "up_finance.db")
    db.set_ignored_payees([str(p) for p in opts["ignore_payees"]])
    metrics = Metrics(db, tz, parse_budgets(opts["budgets"]), float(opts["large_transaction_threshold"]))
    ha = HomeAssistant()

    token = str(opts.get("up_token") or "")
    syncer = Syncer(db, UpClient(token), tz, str(opts["account_scope"])) if token else None
    sync_lock = threading.Lock()

    def do_sync() -> dict:
        if syncer is None:
            msg = "No Up token configured - set up_token in the add-on Configuration tab"
            with db.writer() as conn:
                db.set_state(conn, "last_error", msg)
            raise UpAuthError(msg)
        with sync_lock:
            try:
                result = syncer.sync()
                with db.writer() as conn:
                    db.set_state(conn, "last_error", "")
            except Exception as e:
                with db.writer() as conn:
                    db.set_state(conn, "last_error",
                                 f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {e}")
                raise
            try:
                ha.publish(metrics)
            except Exception as e:
                log.warning("Publishing sensors failed: %s", e)
            return result

    if args.once:
        print(json.dumps(do_sync(), indent=2))
        print(json.dumps(metrics.overview(), indent=2))
        return

    app = App(db, metrics, str(opts.get("api_key") or ""), do_sync)
    if args.dev:
        serve(app, int(os.environ.get("PORT", 8099)), "dev", host="127.0.0.1")
    else:
        serve(app, INGRESS_PORT, "ingress")
        if len(app.api_key) >= 16:
            serve(app, LAN_PORT, "lan")
        else:
            log.info("api_key not set (min 16 chars) - LAN data API disabled")

    interval = max(5, int(opts["sync_interval_minutes"])) * 60
    next_sync = 0.0
    retry_wait = 0
    while True:
        now = time.monotonic()
        if now >= next_sync:
            try:
                do_sync()
                next_sync = now + interval
                retry_wait = 0
            except UpAuthError as e:
                log.error("%s. Retrying in 1 hour.", e)
                next_sync = now + 3600
            except Exception as e:
                retry_wait = min(retry_wait * 2 if retry_wait else 300, 6 * 3600)
                log.error("Sync failed: %s - retrying in %d min", e, retry_wait // 60)
                next_sync = now + retry_wait
        elif ha.canary_missing():
            log.info("Home Assistant sensors missing (restart?) - republishing")
            try:
                ha.publish(metrics, force=True)
            except Exception as e:
                log.warning("Republish failed: %s", e)
        time.sleep(60)


if __name__ == "__main__":
    main()
