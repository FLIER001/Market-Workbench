"""Small clock scheduler for market score snapshots."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def start(sector_refresh, level2_refresh, plate_refresh) -> None:
    tasks = (
        ("sector", 15 * 60, sector_refresh),
        ("level2", 60 * 60, level2_refresh),
        ("plate", 15 * 60, plate_refresh),
    )

    def loop():
        last: dict[str, float] = {}
        closed: dict[str, str] = {}
        while True:
            time.sleep(60)
            now = datetime.now(BEIJING)
            if now.weekday() >= 5:
                continue
            minutes = now.hour * 60 + now.minute
            trading = (9 * 60 + 15 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)
            postclose = 15 * 60 + 10 <= minutes <= 15 * 60 + 20
            for key, interval, refresh in tasks:
                due = trading and time.time() - last.get(key, 0) >= interval
                due = due or (postclose and closed.get(key) != now.date().isoformat())
                if not due:
                    continue
                try:
                    refresh()
                    last[key] = time.time()
                    if postclose:
                        closed[key] = now.date().isoformat()
                except Exception:
                    pass

    threading.Thread(target=loop, daemon=True, name="score-scheduler").start()
