"""Työkalujen logiikka: lukee ensisijaisesti DuckDB:stä, hakee tuoreimmat puuttuvat päivät APIsta."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import duckdb
import pandas as pd

from . import db
from .config import DATA_DIR, DB_PATH, TIMEZONE
from .tables import TABLES
from .sync import fetch_chunk, today

FRESH_WINDOW_DAYS = 14  # vain näin tuoreita puuttuvia päiviä haetaan APIsta; vanhempi aukko -> `oura-mcp sync`
MAX_ROWS = 500

DAILY_SUMMARY_SQL = """
CREATE OR REPLACE TEMP VIEW daily_summary AS
WITH main_sleep AS (
  SELECT * EXCLUDE (rn) FROM (
    SELECT *, row_number() OVER (PARTITION BY day ORDER BY total_sleep_duration DESC NULLS LAST) AS rn FROM sleep
  ) WHERE rn = 1),
days AS (
  SELECT day FROM daily_sleep UNION SELECT day FROM daily_readiness
  UNION SELECT day FROM daily_activity UNION SELECT day FROM main_sleep UNION SELECT day FROM daily_stress)
SELECT d.day,
  ds.score AS sleep_score, dr.score AS readiness_score, da.score AS activity_score,
  round(ms.total_sleep_duration / 3600.0, 2) AS sleep_hours,
  round(ms.deep_sleep_duration / 60.0, 1) AS deep_sleep_min,
  round(ms.rem_sleep_duration / 60.0, 1) AS rem_sleep_min,
  round(ms.light_sleep_duration / 60.0, 1) AS light_sleep_min,
  ms.efficiency AS sleep_efficiency,
  ms.average_heart_rate AS avg_hr, ms.average_hrv AS hrv, ms.lowest_heart_rate AS resting_hr,
  da.steps, da.active_calories, da.total_calories,
  dr.temperature_deviation, dr.temperature_trend_deviation,
  st.stress_high, st.recovery_high
FROM days d
LEFT JOIN daily_sleep ds USING (day) LEFT JOIN daily_readiness dr USING (day)
LEFT JOIN daily_activity da USING (day) LEFT JOIN main_sleep ms USING (day)
LEFT JOIN daily_stress st USING (day)
"""
METRICS = [
    "sleep_score", "readiness_score", "activity_score", "sleep_hours", "deep_sleep_min", "rem_sleep_min",
    "light_sleep_min", "sleep_efficiency", "avg_hr", "hrv", "resting_hr", "steps", "active_calories",
    "total_calories", "temperature_deviation", "temperature_trend_deviation", "stress_high", "recovery_high",
]
DAILY_ENDPOINTS = ["daily_sleep", "sleep", "daily_readiness", "daily_activity", "daily_stress"]


class ToolError(ValueError):
    pass


def _d(v: str | date) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ToolError(f"Virheellinen päivämäärä '{v}', käytä muotoa YYYY-MM-DD") from None


def records(df: pd.DataFrame) -> list[dict]:
    out = []
    for r in df.to_dict("records"):
        out.append({k: _clean(v) for k, v in r.items()})
    return out


def _clean(v: Any) -> Any:
    if v is None or (isinstance(v, float) and v != v) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp) and v.tzinfo is None and v == v.normalize():
        return v.date().isoformat()  # DATE-sarakkeet tulevat pandasista keskiyön aikaleimoina
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


def parse_period(period: str, anchor: date) -> tuple[date, date]:
    """'30d' | '12w' | '6m' | '1y' (päättyy ankkuriin) tai 'YYYY-MM-DD..YYYY-MM-DD'."""
    if ".." in period:
        a, b = period.split("..", 1)
        return _d(a.strip()), _d(b.strip())
    m = re.fullmatch(r"\s*(\d+)\s*([dwmy])\s*", period.lower())
    if not m:
        raise ToolError("period: käytä muotoa '30d', '12w', '6m', '1y' tai 'YYYY-MM-DD..YYYY-MM-DD'")
    n, unit = int(m[1]), m[2]
    days = n * {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
    return anchor - timedelta(days=days - 1), anchor


class Service:
    def __init__(self, db_path: Path | str | None = None, client_factory: Callable[[], Any] | None = None):
        self.db_path = DB_PATH if db_path is None else db_path
        self.client_factory = client_factory
        self._client = None

    # ----------------------------------------------------------- yhteydet
    @contextmanager
    def rw(self):
        con = db.connect(self.db_path)
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def ro(self):
        """Vain-luku-yhteys ilman tiedosto-/verkkoaccessia: SQL ei voi kirjoittaa eikä lukea muita tiedostoja."""
        with self.rw():  # varmistaa että tiedosto + skeema on olemassa
            pass
        con = duckdb.connect(str(self.db_path), read_only=True, config={"enable_external_access": "false"})
        try:
            con.execute(f"SET TimeZone='{TIMEZONE}'")
            con.execute(DAILY_SUMMARY_SQL)
            yield con
        finally:
            con.close()

    def client(self):
        if self._client is None:
            if self.client_factory is None:
                from .api import OuraClient
                from .config import Settings
                self.client_factory = lambda: OuraClient(Settings.load())
            self._client = self.client_factory()
        return self._client

    # ------------------------------------------------------- tuoreus (API-fallback)
    def freshen(self, endpoints: list[str], start: date, end: date, notes: list[str]) -> None:
        """Hae APIsta vain puuttuvat tuoreet päivät. Virhe (ei autentikointia, rate limit) -> huomautus, ei kaatumista."""
        lo = max(start, today() - timedelta(days=FRESH_WINDOW_DAYS))
        hi = min(end, today())
        if lo > hi:
            return
        by_ep = {t.endpoint: t for t in TABLES.values()}
        for ep in endpoints:
            t = by_ep[ep]
            with self.rw() as con:
                have = {r[0] for r in con.execute(
                    f"SELECT DISTINCT CAST({t.day_col} AT TIME ZONE '{TIMEZONE}' AS DATE) FROM {t.name} "
                    f"WHERE CAST({t.day_col} AT TIME ZONE '{TIMEZONE}' AS DATE) BETWEEN ? AND ?"
                    if t.day_col == "timestamp" else
                    f"SELECT DISTINCT {t.day_col} FROM {t.name} WHERE {t.day_col} BETWEEN ? AND ?", [lo, hi]).fetchall()}
                missing = [lo + timedelta(days=i) for i in range((hi - lo).days + 1) if lo + timedelta(days=i) not in have]
                if not missing:
                    continue
                a, b = min(missing), max(missing)
                if db.recently_fetched(con, ep, a, b):
                    continue
                try:
                    rows = fetch_chunk(self.client(), ep, a, b)
                except Exception as e:  # noqa: BLE001 — palautetaan huomautuksena, välimuistidata riittää usein
                    notes.append(f"APIsta haku ({ep} {a}..{b}) epäonnistui: {e}")
                    continue
                db.upsert(con, t, rows)
                db.log_fetch(con, ep, a, b)

    def _gap_notes(self, con, start: date, end: date, notes: list[str]) -> None:
        hi = min(end, today())
        recent = [d for d in (hi - timedelta(days=i) for i in range(3)) if d >= start and d >= today() - timedelta(days=2)]
        for d in recent:
            r = db.one(con, "SELECT sleep_score, readiness_score, activity_score FROM daily_summary WHERE day=?", [d])
            if r is None or (r[0] is None and r[1] is None):
                notes.append(f"{d}: uni/readiness puuttuu (avaa Oura-sovellus ja synkkaa sormus; aktiivisuus ja stressi päivittyvät taustalla).")

    # ----------------------------------------------------------------- työkalut
    def daily_summary(self, start_date: str, end_date: str) -> dict:
        s, e = _d(start_date), _d(end_date)
        if e < s:
            raise ToolError("end_date on ennen start_date")
        notes: list[str] = []
        self.freshen(DAILY_ENDPOINTS, s, e, notes)
        with self.ro() as con:
            df = con.execute("SELECT * FROM daily_summary WHERE day BETWEEN ? AND ? ORDER BY day", [s, e]).df()
            self._gap_notes(con, s, e, notes)
        if len(df) > 366:
            notes.append(f"Rajattu 366 riviin ({len(df)} päivää); käytä get_trends tai query_sql pidemmille jaksoille.")
            df = df.tail(366)
        return {"rows": records(df), "n_days": len(df), "notes": notes}

    def sleep_detail(self, date_: str) -> dict:
        d = _d(date_)
        notes: list[str] = []
        self.freshen(["daily_sleep", "sleep", "daily_readiness"], d, d, notes)
        with self.ro() as con:
            ds = db.one(con, "SELECT raw FROM daily_sleep WHERE day=?", [d])
            dr = db.one(con, "SELECT raw FROM daily_readiness WHERE day=?", [d])
            periods = con.execute("SELECT raw FROM sleep WHERE day=? ORDER BY bedtime_start", [d]).fetchall()
        if not (ds or periods):
            notes.append("Ei unidataa tälle päivälle. Uni ilmestyy vasta kun Oura-sovellus on avattu ja sormus synkattu.")
        return {
            "date": str(d),
            "daily_sleep": _daily_part(ds),
            "daily_readiness": _daily_part(dr),
            "sleep_periods": [_condense_sleep(json.loads(p[0])) for p in periods],
            "notes": notes,
        }

    def workouts(self, start_date: str, end_date: str) -> dict:
        s, e = _d(start_date), _d(end_date)
        notes: list[str] = []
        self.freshen(["workout"], s, e, notes)
        with self.ro() as con:
            df = con.execute(
                "SELECT day, activity, label, intensity, round(calories) AS calories, round(distance) AS distance_m, "
                "start_datetime, end_datetime, round(date_diff('second', start_datetime, end_datetime)/60.0, 1) AS duration_min, source "
                "FROM workout WHERE day BETWEEN ? AND ? ORDER BY start_datetime", [s, e]).df()
            summ = con.execute(
                "SELECT activity, count(*) AS n, round(sum(calories)) AS calories, "
                "round(sum(date_diff('second', start_datetime, end_datetime))/60.0, 1) AS minutes "
                "FROM workout WHERE day BETWEEN ? AND ? GROUP BY activity ORDER BY n DESC", [s, e]).df()
        return {"workouts": records(df.head(MAX_ROWS)), "by_activity": records(summ), "n": len(df), "notes": notes}

    def trends(self, metric: str, period: str, end_date: str | None = None) -> dict:
        _check_metric(metric)
        notes: list[str] = []
        with self.ro() as con:
            anchor = _d(end_date) if end_date else (db.one(
                con, f"SELECT max(day) FROM daily_summary WHERE {metric} IS NOT NULL")[0] or today())
        s, e = parse_period(period, anchor)
        self.freshen(DAILY_ENDPOINTS, s, e, notes)
        with self.ro() as con:
            df = con.execute(f"SELECT day, {metric} AS v FROM daily_summary WHERE day BETWEEN ? AND ? AND {metric} IS NOT NULL ORDER BY day", [s, e]).df()
            if df.empty:
                return {"metric": metric, "start": str(s), "end": str(e), "n_days": 0, "notes": notes + ["Ei dataa jaksolla."]}
            stats = db.one(
                con, f"SELECT count(*), avg(v), median(v), min(v), max(v), stddev_samp(v), "
                f"regr_slope(v, epoch(CAST(day AS TIMESTAMP))/86400.0) FROM (SELECT day, {metric} AS v FROM daily_summary "
                f"WHERE day BETWEEN ? AND ? AND {metric} IS NOT NULL)", [s, e])
            weekly = con.execute(
                f"SELECT CAST(date_trunc('week', day) AS DATE) AS week_start, round(avg({metric}),2) AS mean, count(*) AS n "
                f"FROM daily_summary WHERE day BETWEEN ? AND ? AND {metric} IS NOT NULL GROUP BY 1 ORDER BY 1", [s, e]).df()
            monthly = con.execute(
                f"SELECT strftime(day, '%Y-%m') AS month, round(avg({metric}),2) AS mean, count(*) AS n "
                f"FROM daily_summary WHERE day BETWEEN ? AND ? AND {metric} IS NOT NULL GROUP BY 1 ORDER BY 1", [s, e]).df()
            rolling = con.execute(
                f"SELECT day, round(r,2) AS rolling_7d FROM (SELECT day, avg({metric}) OVER (ORDER BY day RANGE BETWEEN INTERVAL 6 DAYS PRECEDING AND CURRENT ROW) AS r "
                f"FROM daily_summary WHERE day BETWEEN ? AND ? AND {metric} IS NOT NULL) ORDER BY day", [s, e]).df()
        n, mean, med, mn, mx, sd, slope = stats
        span = (df["day"].max() - df["day"].min()).days if len(df) > 1 else 0
        change = (slope or 0) * span
        direction = "vakaa" if not sd or abs(change) < 0.5 * sd else ("nouseva" if change > 0 else "laskeva")
        half = len(df) // 2
        # rolling-sarja harvennetaan: viikon välein + viimeinen piste
        step = rolling.iloc[::7]
        if rolling.index[-1] not in step.index:
            step = pd.concat([step, rolling.tail(1)])
        out = {
            "metric": metric, "start": str(s), "end": str(e), "n_days": int(n),
            "mean": _r(mean), "median": _r(med), "min": _r(mn), "max": _r(mx), "stddev": _r(sd),
            "trend": {"direction": direction, "slope_per_week": _r((slope or 0) * 7), "change_over_period": _r(change),
                      "first_half_mean": _r(df["v"].iloc[:half].mean()) if half else None,
                      "second_half_mean": _r(df["v"].iloc[half:].mean()),
                      "rule": "vakaa jos |muutos| < 0.5 × keskihajonta"},
            "rolling_7d_weekly_samples": records(step),
            "notes": notes,
        }
        long = (e - s).days > 120
        out["monthly" if long else "weekly"] = records(monthly if long else weekly)
        return out

    def compare_periods(self, metric: str, period_a: str, period_b: str) -> dict:
        _check_metric(metric)
        notes: list[str] = []
        with self.ro() as con:
            anchor = db.one(con, "SELECT max(day) FROM daily_summary")[0] or today()
        (a1, a2), (b1, b2) = parse_period(period_a, anchor), parse_period(period_b, anchor)
        self.freshen(DAILY_ENDPOINTS, min(a1, b1), max(a2, b2), notes)
        with self.ro() as con:
            def stat(x, y):
                r = db.one(
                    con, f"SELECT count({metric}), avg({metric}), median({metric}), min({metric}), max({metric}), stddev_samp({metric}) "
                    f"FROM daily_summary WHERE day BETWEEN ? AND ?", [x, y])
                return {"start": str(x), "end": str(y), "n": r[0], "mean": _r(r[1]), "median": _r(r[2]),
                        "min": _r(r[3]), "max": _r(r[4]), "stddev": _r(r[5])}
            a, b = stat(a1, a2), stat(b1, b2)
        delta = pct = None
        if a["mean"] is not None and b["mean"] is not None:
            delta = _r(b["mean"] - a["mean"])
            pct = _r(100 * (b["mean"] - a["mean"]) / a["mean"]) if a["mean"] else None
        return {"metric": metric, "period_a": a, "period_b": b, "delta_b_minus_a": delta, "pct_change": pct, "notes": notes}

    def heartrate_summary(self, date_: str) -> dict:
        d = _d(date_)
        notes: list[str] = []
        self.freshen(["heartrate"], d, d, notes)
        local = f"(timestamp AT TIME ZONE '{TIMEZONE}')"
        with self.ro() as con:
            hourly = con.execute(
                f"SELECT CAST(extract(hour FROM {local}) AS INTEGER) AS hour, count(*) AS n, min(bpm) AS min, "
                f"round(quantile_cont(bpm, 0.25),1) AS q1, round(median(bpm),1) AS median, round(quantile_cont(bpm, 0.75),1) AS q3, "
                f"max(bpm) AS max, round(avg(bpm),1) AS mean FROM heartrate WHERE CAST({local} AS DATE)=? GROUP BY 1 ORDER BY 1", [d]).df()
            src = con.execute(
                f"SELECT source, count(*) AS n, round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct, round(avg(bpm),1) AS mean_bpm "
                f"FROM heartrate WHERE CAST({local} AS DATE)=? GROUP BY 1 ORDER BY n DESC", [d]).df()
        if hourly.empty:
            notes.append("Ei syketietoja tälle päivälle.")
        return {"date": str(d), "hourly": records(hourly), "by_source": records(src), "notes": notes}

    def query_sql(self, sql: str) -> dict:
        try:
            stmts = duckdb.extract_statements(sql)
        except duckdb.Error as e:
            raise ToolError(f"SQL-jäsennysvirhe: {e}") from e
        if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
            raise ToolError("Vain yksi SELECT-lause sallitaan (myös WITH ... SELECT).")
        with self.ro() as con:
            try:
                cur = con.execute(sql)
                rows = cur.fetchmany(MAX_ROWS + 1)
                cols = [c[0] for c in cur.description]
            except duckdb.Error as e:
                raise ToolError(f"SQL-virhe: {e}") from e
        truncated = len(rows) > MAX_ROWS
        df = pd.DataFrame(rows[:MAX_ROWS], columns=cols)
        return {"columns": cols, "rows": records(df), "n": len(df), "truncated": truncated,
                "hint": f"Max {MAX_ROWS} riviä; käytä aggregointia (GROUP BY) isoille tauluille." if truncated else None}

    def export_csv(self, table: str, start_date: str | None = None, end_date: str | None = None) -> dict:
        names = list(TABLES) + ["daily_summary"]
        if table not in names:
            raise ToolError(f"Tuntematon taulu '{table}'. Valitse: {', '.join(names)}")
        s, e = _d(start_date) if start_date else None, _d(end_date) if end_date else None
        if table == "daily_summary":
            col, tz = "day", False
        else:
            col = TABLES[table].day_col
            tz = col == "timestamp"
        expr = f"CAST({col} AT TIME ZONE '{TIMEZONE}' AS DATE)" if tz else col
        where, params = [], []
        if s:
            where.append(f"{expr} >= ?"); params.append(s)
        if e:
            where.append(f"{expr} <= ?"); params.append(e)
        cols = "* EXCLUDE (raw)" if table != "daily_summary" else "*"
        with self.ro() as con:
            df = con.execute(f"SELECT {cols} FROM {table}" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {col}", params).df()
        out_dir = DATA_DIR / "exports" if self.db_path == DB_PATH else Path(self.db_path).parent / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{table}_{s or 'alku'}_{e or 'loppu'}.csv"
        df.to_csv(path, index=False)
        return {"path": str(path), "rows": len(df), "columns": list(df.columns)}


def _check_metric(m: str) -> None:
    if m not in METRICS:
        raise ToolError(f"Tuntematon metric '{m}'. Valitse: {', '.join(METRICS)}")


def _r(v: Any, n: int = 2):
    return None if v is None or v != v else round(float(v), n)


def _daily_part(row) -> dict | None:
    if not row:
        return None
    raw = json.loads(row[0])
    return {k: v for k, v in raw.items() if k not in ("id",)}


PHASES = {"1": "deep", "2": "light", "3": "rem", "4": "awake"}


def _condense_sleep(raw: dict) -> dict:
    """Pudota 5 min/30 s -aikasarjat pois; korvaa tiivistelmällä."""
    out = {k: v for k, v in raw.items() if k not in ("heart_rate", "hrv", "movement_30_sec", "sleep_phase_5_min", "sleep_phase_30_sec")}
    for key in ("heart_rate", "hrv"):
        items = [x for x in (raw.get(key) or {}).get("items", []) if x is not None]
        if items:
            out[f"{key}_summary"] = {"min": min(items), "max": max(items), "mean": round(sum(items) / len(items), 1), "interval_s": raw[key].get("interval")}
    ph = raw.get("sleep_phase_5_min")
    if ph:
        out["sleep_phase_minutes"] = {PHASES.get(c, c): ph.count(c) * 5 for c in sorted(set(ph))}
    return out
