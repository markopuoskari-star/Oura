"""DuckDB-välimuisti: yhteys, skeema, upsert ja synkkatila."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from .config import DB_PATH, TIMEZONE
from .tables import TABLES, Table


def connect(path: Path | str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = DB_PATH if path is None else path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute(f"SET TimeZone='{TIMEZONE}'")
    if not read_only:
        init_schema(con)
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    for t in TABLES.values():
        cols = ", ".join(f'"{c}" {typ}' for c, typ, _ in t.cols)
        pk = ", ".join(f'"{c}"' for c in t.pk)
        con.execute(f'CREATE TABLE IF NOT EXISTS {t.name} ({cols}, raw JSON, PRIMARY KEY ({pk}))')
    con.execute("CREATE TABLE IF NOT EXISTS sync_state (endpoint VARCHAR PRIMARY KEY, last_day DATE, synced_at TIMESTAMPTZ)")
    con.execute("CREATE TABLE IF NOT EXISTS fetch_log (endpoint VARCHAR, attempted_at TIMESTAMPTZ, start_day DATE, end_day DATE)")


def _dig(row: dict, path: tuple[str, ...]) -> Any:
    cur: Any = row
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def upsert(con: duckdb.DuckDBPyConnection, table: Table, rows: Iterable[dict]) -> int:
    """Idempotentti upsert (INSERT OR REPLACE primary keyn mukaan). Palauttaa käsiteltyjen rivien määrän."""
    rows = list(rows)
    if not rows:
        return 0
    data = {c: [_dig(r, p) for r in rows] for c, _, p in table.cols}
    data["raw"] = [json.dumps(r, ensure_ascii=False) for r in rows]
    df = pd.DataFrame(data, dtype=object).drop_duplicates(subset=list(table.pk), keep="last")
    select = ", ".join(f'CAST("{c}" AS {typ})' for c, typ, _ in table.cols) + ", CAST(raw AS JSON)"
    names = ", ".join(f'"{c}"' for c, _, _ in table.cols) + ", raw"
    con.register("_upsert_df", df)
    try:
        con.execute(f"INSERT OR REPLACE INTO {table.name} ({names}) SELECT {select} FROM _upsert_df")
    finally:
        con.unregister("_upsert_df")
    return len(df)


# ------------------------------------------------------------------ synkkatila
def get_last_synced(con, endpoint: str) -> date | None:
    r = con.execute("SELECT last_day FROM sync_state WHERE endpoint=?", [endpoint]).fetchone()
    return r[0] if r else None


def set_last_synced(con, endpoint: str, day: date) -> None:
    cur = get_last_synced(con, endpoint)
    if cur and cur >= day:
        day = cur
    con.execute(
        "INSERT OR REPLACE INTO sync_state VALUES (?, ?, now())", [endpoint, day]
    )


def log_fetch(con, endpoint: str, start: date, end: date) -> None:
    con.execute("INSERT INTO fetch_log VALUES (?, now(), ?, ?)", [endpoint, start, end])


def recently_fetched(con, endpoint: str, start: date, end: date, within: timedelta = timedelta(minutes=30)) -> bool:
    """Onko tätä (tai laajempaa) väliä yritetty hakea hiljattain? Estää API-kutsut joka työkalukutsulla
    silloin kun data puuttuu pysyvästi (esim. uni ennen kuin sormus on synkattu)."""
    r = con.execute(
        "SELECT count(*) FROM fetch_log WHERE endpoint=? AND start_day<=? AND end_day>=? "
        "AND attempted_at > now() - ?::INTERVAL",
        [endpoint, start, end, f"{int(within.total_seconds())} seconds"],
    ).fetchone()
    return r[0] > 0
