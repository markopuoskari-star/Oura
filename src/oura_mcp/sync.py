"""Historian synkkaus APIsta DuckDB:hen paloissa (idempotentti)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import TIMEZONE
from .db import get_last_synced, set_last_synced, upsert
from .tables import TABLES

TZ = ZoneInfo(TIMEZONE)
OVERLAP_DAYS = 3  # jatkettaessa palataan taaksepäin: uni/readiness ja kuluvan päivän aktiivisuus päivittyvät jälkikäteen

# palan koko päivinä
CHUNK_DAYS = {
    "daily_sleep": 90, "daily_activity": 90, "daily_readiness": 90, "daily_stress": 90,
    "daily_spo2": 90, "daily_resilience": 90, "daily_cardiovascular_age": 90, "vO2_max": 90,
    "sleep": 30, "workout": 30, "session": 30, "enhanced_tag": 90,
    "heartrate": 7,
}


def today() -> date:
    return datetime.now(TZ).date()


def chunks(start: date, end: date, days: int):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def _local_dt(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=TZ).isoformat()


def fetch_chunk(client, endpoint: str, a: date, b: date) -> list[dict]:
    if endpoint == "heartrate":
        return client.get_all(endpoint, _local_dt(a), _local_dt(b + timedelta(days=1)))
    return client.get_all(endpoint, a, b)


def run_sync(
    client,
    con,
    start: date | None,
    end: date | None = None,
    endpoints: list[str] | None = None,
    progress: Callable[[str], None] = lambda s: None,
) -> dict[str, int]:
    end = end or today()
    endpoints = endpoints or [t.endpoint for t in TABLES.values()]
    by_ep = {t.endpoint: t for t in TABLES.values()}
    totals: dict[str, int] = {}
    for ep in endpoints:
        table = by_ep[ep]
        if start is not None:
            ep_start = start
        else:
            last = get_last_synced(con, ep)
            if last is None:
                raise ValueError(f"{ep}: ei aiempaa synkkausta — anna --from YYYY-MM-DD")
            ep_start = last - timedelta(days=OVERLAP_DAYS)
        if ep_start > end:
            continue
        n = 0
        for a, b in chunks(ep_start, end, CHUNK_DAYS.get(ep, 30)):
            rows = fetch_chunk(client, ep, a, b)
            n += upsert(con, table, rows)
            set_last_synced(con, ep, b)
            progress(f"{ep} {a}..{b}: {len(rows)} riviä")
        totals[ep] = n
    return totals
