import json
from datetime import date, timedelta

import pytest

from oura_mcp import db, service as svc_mod
from oura_mcp.service import Service, ToolError
from oura_mcp.tables import TABLES

START = date(2024, 1, 1)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.rows = {}

    def get_all(self, ep, start=None, end=None, fields=None):
        self.calls.append((ep, str(start)[:10], str(end)[:10]))
        return self.rows.get(ep, [])


@pytest.fixture
def svc(tmp_path, monkeypatch):
    s = Service(tmp_path / "t.duckdb", client_factory=FakeClient)
    # täytä 60 päivää: pisteet nousevat 1/pv
    with s.rw() as con:
        n = 60
        db.upsert(con, TABLES["daily_sleep"], [{"day": str(START + timedelta(i)), "score": 60 + i, "contributors": {"deep_sleep": 50}} for i in range(n)])
        db.upsert(con, TABLES["daily_readiness"], [{"day": str(START + timedelta(i)), "score": 70, "temperature_deviation": 0.1} for i in range(n)])
        db.upsert(con, TABLES["daily_activity"], [{"day": str(START + timedelta(i)), "score": 80, "steps": 8000 + i} for i in range(n)])
        db.upsert(con, TABLES["sleep"], [
            {"id": f"s{i}", "day": str(START + timedelta(i)), "type": "long_sleep", "total_sleep_duration": 27000,
             "deep_sleep_duration": 5400, "rem_sleep_duration": 6000, "average_hrv": 40 + i, "lowest_heart_rate": 50,
             "sleep_phase_5_min": "1122334", "heart_rate": {"interval": 300, "items": [50, 55, None, 60]},
             "readiness": {"score": 70, "temperature_deviation": 0.1}} for i in range(n)] +
            [{"id": "nap", "day": str(START), "type": "late_nap", "total_sleep_duration": 1800}])
        db.upsert(con, TABLES["heartrate"], [
            {"timestamp": f"2024-01-10T{h:02d}:{m:02d}:00+02:00", "bpm": 50 + h, "source": "sleep" if h < 7 else "awake"}
            for h in range(24) for m in range(0, 60, 5)])
    monkeypatch.setattr(svc_mod, "today", lambda: date(2020, 1, 1))  # ei tuoreita päiviä -> ei API-fallbackia
    return s


def test_daily_summary_one_row_per_day_main_sleep_only(svc):
    r = svc.daily_summary("2024-01-01", "2024-01-03")
    assert r["n_days"] == 3
    row = r["rows"][0]
    assert row["sleep_hours"] == 7.5 and row["deep_sleep_min"] == 90 and row["rem_sleep_min"] == 100  # nap ei sotke
    assert row["resting_hr"] == 50 and row["hrv"] == 40 and row["steps"] == 8000 and row["temperature_deviation"] == 0.1
    assert row["day"] == "2024-01-01"


def test_trends_direction_and_weekly(svc):
    r = svc.trends("sleep_score", "30d", "2024-02-15")
    assert r["trend"]["direction"] == "nouseva"
    assert r["trend"]["slope_per_week"] == pytest.approx(7, abs=0.01)
    assert "weekly" in r and r["n_days"] == 30
    r2 = svc.trends("readiness_score", "30d", "2024-02-15")
    assert r2["trend"]["direction"] == "vakaa"
    assert len(svc.trends("sleep_score", "2023-03-01..2024-02-29")["monthly"]) == 2


def test_compare_periods(svc):
    r = svc.compare_periods("sleep_score", "2024-01-01..2024-01-10", "2024-01-11..2024-01-20")
    assert r["delta_b_minus_a"] == 10.0 and r["period_a"]["n"] == 10


def test_unknown_metric_and_bad_period(svc):
    with pytest.raises(ToolError):
        svc.trends("nope", "30d")
    with pytest.raises(ToolError):
        svc.trends("steps", "kuukausi")


def test_sleep_detail_condenses_series(svc):
    r = svc.sleep_detail("2024-01-02")
    p = r["sleep_periods"][0]
    assert "heart_rate" not in p and p["heart_rate_summary"] == {"min": 50, "max": 60, "mean": 55.0, "interval_s": 300}
    assert p["sleep_phase_minutes"] == {"deep": 10, "light": 10, "rem": 10, "awake": 5}
    assert r["daily_sleep"]["contributors"]["deep_sleep"] == 50


def test_heartrate_summary_hourly_and_sources(svc):
    r = svc.heartrate_summary("2024-01-10")
    assert len(r["hourly"]) == 24 and r["hourly"][3] == {"hour": 3, "n": 12, "min": 53, "q1": 53.0, "median": 53.0, "q3": 53.0, "max": 53, "mean": 53.0}
    src = {s["source"]: s for s in r["by_source"]}
    assert src["sleep"]["n"] == 7 * 12 and src["sleep"]["pct"] == pytest.approx(29.2, abs=0.1)


def test_query_sql_readonly_guards(svc):
    assert svc.query_sql("SELECT count(*) AS n FROM daily_summary")["rows"] == [{"n": 60}]
    for bad in ["DROP TABLE sleep", "INSERT INTO daily_sleep (day) VALUES ('2025-01-01')", "SELECT 1; SELECT 2",
                "COPY (SELECT 1) TO '/tmp/x.csv'", "SELECT * FROM read_csv('/etc/passwd')", "ATTACH 'x.db'"]:
        with pytest.raises(ToolError):
            svc.query_sql(bad)
    assert svc.query_sql("SELECT * FROM heartrate, range(2)")["truncated"] is True


def test_export_csv(svc):
    r = svc.export_csv("daily_summary", "2024-01-01", "2024-01-05")
    assert r["rows"] == 5 and open(r["path"]).readline().startswith("day,")
    r = svc.export_csv("heartrate", "2024-01-10", "2024-01-10")
    assert r["rows"] == 288 and "raw" not in r["columns"]
    with pytest.raises(ToolError):
        svc.export_csv("passwd")


def test_api_fallback_only_for_missing_recent_days_and_not_repeated(tmp_path, monkeypatch):
    fc = FakeClient()
    s = Service(tmp_path / "f.duckdb", client_factory=lambda: fc)
    monkeypatch.setattr(svc_mod, "today", lambda: date(2024, 3, 10))
    fc.rows["daily_activity"] = [{"day": "2024-03-09", "score": 77, "steps": 1234}]
    r = s.daily_summary("2024-03-08", "2024-03-10")
    eps = {c[0]: c for c in fc.calls}
    assert set(eps) == set(svc_mod.DAILY_ENDPOINTS) and eps["daily_activity"][1:] == ("2024-03-08", "2024-03-10")
    assert r["rows"][0]["steps"] == 1234
    assert any("uni/readiness puuttuu" in n for n in r["notes"])
    n_calls = len(fc.calls)
    s.daily_summary("2024-03-08", "2024-03-10")  # puuttuva uni ei laukaise uutta hakua heti
    assert len(fc.calls) == n_calls


def test_api_failure_degrades_to_note(tmp_path, monkeypatch):
    class Boom:
        def get_all(self, *a, **k):
            raise RuntimeError("Aja `oura-mcp auth` uudelleen.")
    s = Service(tmp_path / "g.duckdb", client_factory=Boom)
    monkeypatch.setattr(svc_mod, "today", lambda: date(2024, 3, 10))
    r = s.daily_summary("2024-03-09", "2024-03-10")
    assert r["rows"] == [] and any("oura-mcp auth" in n for n in r["notes"])


def test_no_gap_note_for_old_ranges(svc, monkeypatch):
    monkeypatch.setattr(svc_mod, "today", lambda: date(2025, 1, 1))
    assert svc.daily_summary("2024-06-01", "2024-06-03")["notes"] == []
