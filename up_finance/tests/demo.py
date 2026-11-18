"""Serve the dashboard locally with fake data:  python3 tests/demo.py  -> http://127.0.0.1:8099"""
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests")]

from app.db import Database  # noqa: E402
from app.metrics import Metrics  # noqa: E402
from app.sync import Syncer  # noqa: E402
from app.web import App, serve  # noqa: E402
from fake_up import FakeUpClient  # noqa: E402

tz = ZoneInfo("Australia/Sydney")
path = root / ".dev-data" / "demo.db"
path.unlink(missing_ok=True)
db = Database(path)
client = FakeUpClient(datetime.now(tz).date())
syncer = Syncer(db, client, tz)
syncer.sync()
# fake a few weeks of balance snapshots so the line chart has shape
conn = db.connect()
from datetime import timedelta  # noqa: E402
import random  # noqa: E402
rnd = random.Random(1)
today = datetime.now(tz).date()
for acc, base in [("acc-spending", 3400), ("acc-saver", 11500), ("acc-holiday", 1200)]:
    bal = base * 100
    for i in range(90, 0, -1):
        bal += rnd.randint(-15000, 18000) if acc == "acc-spending" else rnd.randint(0, 2500)
        conn.execute("INSERT OR REPLACE INTO balance_snapshots VALUES (?,?,?)",
                     ((today - timedelta(days=i)).isoformat(), acc, max(bal, 0)))
conn.commit()
conn.close()
metrics = Metrics(db, tz, {"groceries": 600, "restaurants-and-cafes": 150, "takeaway": 120}, 200,
                  reserve_saver="Emergency Fund")
serve(App(db, metrics, "", lambda: syncer.sync()), 8099, "dev", host="127.0.0.1")
print("Dashboard on http://127.0.0.1:8099")
while True:
    time.sleep(3600)
