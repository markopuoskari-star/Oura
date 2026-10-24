"""Taulumäärittelyt: mitkä kentät puretaan sarakkeiksi. Koko alkuperäinen vastaus säilyy `raw`-sarakkeessa (JSON),
joten uusia sarakkeita voi johtaa jälkikäteen ilman uutta hakua."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    name: str
    endpoint: str
    pk: tuple[str, ...]
    day_col: str  # sarake jolla päiväsuodatus tehdään (DATE tai TIMESTAMPTZ)
    cols: tuple[tuple[str, str, tuple[str, ...]], ...]  # (sarake, duckdb-tyyppi, polku vastauksessa)


def C(name: str, typ: str, *path: str):
    return (name, typ, path or (name,))


def contrib(*names: str):
    return tuple(C(f"c_{n}", "INTEGER", "contributors", n) for n in names)


TABLES: dict[str, Table] = {t.name: t for t in [
    Table("daily_sleep", "daily_sleep", ("day",), "day", (
        C("day", "DATE"), C("score", "INTEGER"), C("timestamp", "TIMESTAMPTZ"),
        *contrib("deep_sleep", "efficiency", "latency", "rem_sleep", "restfulness", "timing", "total_sleep"))),
    Table("sleep", "sleep", ("id",), "day", (
        C("id", "VARCHAR"), C("day", "DATE"), C("type", "VARCHAR"),
        C("bedtime_start", "TIMESTAMPTZ"), C("bedtime_end", "TIMESTAMPTZ"),
        C("total_sleep_duration", "INTEGER"), C("time_in_bed", "INTEGER"), C("awake_time", "INTEGER"),
        C("light_sleep_duration", "INTEGER"), C("deep_sleep_duration", "INTEGER"), C("rem_sleep_duration", "INTEGER"),
        C("latency", "INTEGER"), C("efficiency", "INTEGER"), C("restless_periods", "INTEGER"),
        C("average_heart_rate", "DOUBLE"), C("lowest_heart_rate", "INTEGER"), C("average_hrv", "DOUBLE"),
        C("average_breath", "DOUBLE"),
        C("readiness_score", "INTEGER", "readiness", "score"),
        C("temperature_deviation", "DOUBLE", "readiness", "temperature_deviation"),
        C("temperature_trend_deviation", "DOUBLE", "readiness", "temperature_trend_deviation"))),
    Table("daily_activity", "daily_activity", ("day",), "day", (
        C("day", "DATE"), C("score", "INTEGER"), C("steps", "INTEGER"), C("active_calories", "INTEGER"),
        C("total_calories", "INTEGER"), C("target_calories", "INTEGER"),
        C("equivalent_walking_distance", "INTEGER"), C("high_activity_time", "INTEGER"),
        C("medium_activity_time", "INTEGER"), C("low_activity_time", "INTEGER"),
        C("sedentary_time", "INTEGER"), C("resting_time", "INTEGER"), C("average_met_minutes", "DOUBLE"),
        C("inactivity_alerts", "INTEGER"),
        *contrib("meet_daily_targets", "move_every_hour", "recovery_time", "stay_active",
                 "training_frequency", "training_volume"))),
    Table("daily_readiness", "daily_readiness", ("day",), "day", (
        C("day", "DATE"), C("score", "INTEGER"), C("temperature_deviation", "DOUBLE"),
        C("temperature_trend_deviation", "DOUBLE"),
        *contrib("activity_balance", "body_temperature", "hrv_balance", "previous_day_activity",
                 "previous_night", "recovery_index", "resting_heart_rate", "sleep_balance"))),
    Table("daily_stress", "daily_stress", ("day",), "day", (
        C("day", "DATE"), C("stress_high", "INTEGER"), C("recovery_high", "INTEGER"), C("day_summary", "VARCHAR"))),
    Table("daily_spo2", "daily_spo2", ("day",), "day", (
        C("day", "DATE"), C("spo2_average", "DOUBLE", "spo2_percentage", "average"),
        C("breathing_disturbance_index", "INTEGER"))),
    Table("daily_resilience", "daily_resilience", ("day",), "day", (
        C("day", "DATE"), C("level", "VARCHAR"),
        C("c_sleep_recovery", "DOUBLE", "contributors", "sleep_recovery"),
        C("c_daytime_recovery", "DOUBLE", "contributors", "daytime_recovery"),
        C("c_stress", "DOUBLE", "contributors", "stress"))),
    Table("daily_cardiovascular_age", "daily_cardiovascular_age", ("day",), "day", (
        C("day", "DATE"), C("vascular_age", "INTEGER"))),
    Table("vo2_max", "vO2_max", ("day",), "day", (
        C("day", "DATE"), C("vo2_max", "DOUBLE"), C("timestamp", "TIMESTAMPTZ"))),
    Table("workout", "workout", ("id",), "day", (
        C("id", "VARCHAR"), C("day", "DATE"), C("activity", "VARCHAR"), C("calories", "DOUBLE"),
        C("distance", "DOUBLE"), C("intensity", "VARCHAR"), C("label", "VARCHAR"), C("source", "VARCHAR"),
        C("start_datetime", "TIMESTAMPTZ"), C("end_datetime", "TIMESTAMPTZ"))),
    Table("session", "session", ("id",), "day", (
        C("id", "VARCHAR"), C("day", "DATE"), C("type", "VARCHAR"), C("mood", "VARCHAR"),
        C("start_datetime", "TIMESTAMPTZ"), C("end_datetime", "TIMESTAMPTZ"))),
    Table("enhanced_tag", "enhanced_tag", ("id",), "start_day", (
        C("id", "VARCHAR"), C("tag_type_code", "VARCHAR"), C("start_time", "TIMESTAMPTZ"),
        C("end_time", "TIMESTAMPTZ"), C("start_day", "DATE"), C("end_day", "DATE"), C("comment", "VARCHAR"))),
    Table("heartrate", "heartrate", ("timestamp",), "timestamp", (
        C("timestamp", "TIMESTAMPTZ"), C("bpm", "INTEGER"), C("source", "VARCHAR"))),
]}

BY_ENDPOINT = {t.endpoint: t for t in TABLES.values()}
