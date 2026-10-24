from datetime import date, timedelta

import pytest

from oura_mcp import db, sync
from oura_mcp.tables import TABLES


class FakeClient:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get_all(self, ep, start=None, end=None, fields=None):
        self.calls.append((ep, str(start), str(end)))
        return [r for r in self.data.get(ep, []) if str(start)[:10] <= r.get("day", r.get("timestamp", ""))[:10] <= str(end)[:10]]


@pytest.fixture
def con():
    c = db.connect(":memory:")
    yield c
    c.close()


def sleep_row(day, score):
    return {"day": day, "score": score, "timestamp": f"{day}T00:00:00+00:00", "contributors": {"deep_sleep": 80}}


def test_upsert_idempotent_and_updates(con):
    t = TABLES["daily_sleep"]
    db.upsert(con, t, [sleep_row("2024-01-01", 70), sleep_row("2024-01-02", 75)])
    db.upsert(con, t, [sleep_row("2024-01-02", 90)])
    assert con.execute("SELECT count(*) FROM daily_sleep").fetchone()[0] == 2
    assert con.execute("SELECT score, c_deep_sleep FROM daily_sleep WHERE day='2024-01-02'").fetchone() == (90, 80)


def test_missing_fields_become_null_and_raw_kept(con):
    db.upsert(con, TABLES["daily_activity"], [{"day": "2024-01-01", "steps": 5000, "extra_new_field": 1}])
    assert con.execute("SELECT steps, score, raw->>'extra_new_field' FROM daily_activity").fetchone() == (5000, None, "1")


def test_heartrate_timestamp_pk_with_offset(con):
    db.upsert(con, TABLES["heartrate"], [{"timestamp": "2024-01-01T10:00:00+02:00", "bpm": 60, "source": "rest"}] * 2)
    assert con.execute("SELECT count(*), min(bpm) FROM heartrate").fetchone() == (1, 60)


def test_sync_chunks_and_resume(con):
    data = {"daily_sleep": [sleep_row("2024-01-05", 70), sleep_row("2024-06-01", 80)]}
    fc = FakeClient(data)
    sync.run_sync(fc, con, date(2024, 1, 1), date(2024, 6, 30), ["daily_sleep"])
    assert len(fc.calls) == 3  # 90 pv paloissa: 1.1–29.3, 30.3–27.6, 28.6–30.6
    assert con.execute("SELECT count(*) FROM daily_sleep").fetchone()[0] == 2
    assert db.get_last_synced(con, "daily_sleep") == date(2024, 6, 30)

    fc2 = FakeClient(data)
    sync.run_sync(fc2, con, None, date(2024, 7, 10), ["daily_sleep"])  # jatkaa viimeisestä miinus overlap
    assert fc2.calls[0][1] == str(date(2024, 6, 30) - timedelta(days=sync.OVERLAP_DAYS))
    assert con.execute("SELECT count(*) FROM daily_sleep").fetchone()[0] == 2


def test_resume_without_state_requires_from(con):
    with pytest.raises(ValueError, match="--from"):
        sync.run_sync(FakeClient({}), con, None, date(2024, 1, 1), ["sleep"])


def test_heartrate_uses_local_tz_datetimes(con):
    fc = FakeClient({})
    sync.run_sync(fc, con, date(2024, 1, 1), date(2024, 1, 3), ["heartrate"])
    ep, a, b = fc.calls[0]
    assert a == "2024-01-01T00:00:00+02:00" and b == "2024-01-04T00:00:00+02:00"
