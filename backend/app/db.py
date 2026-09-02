"""Thin sqlite layer. No ORM -- the schema is small and query patterns are fixed."""
import sqlite3
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY, name TEXT, basin TEXT, district TEXT,
    lat REAL, lon REAL, warning_level REAL, danger_level REAL, series_id INTEGER
);
CREATE TABLE IF NOT EXISTS readings (
    station_id INTEGER, ts TEXT, level REAL, status TEXT, steady TEXT,
    PRIMARY KEY (station_id, ts)
);
CREATE TABLE IF NOT EXISTS rainfall (
    station_id INTEGER PRIMARY KEY, ts TEXT, past_24h REAL, next_12h REAL
);
CREATE TABLE IF NOT EXISTS scores (
    station_id INTEGER, ts TEXT, fsi REAL, band TEXT, p_exceed_6h REAL,
    rise_rate REAL, components TEXT,
    impoundment_suspected INTEGER DEFAULT 0, impoundment_reason TEXT,
    hours_to_danger REAL, PRIMARY KEY (station_id, ts)
);
-- Non-flood hazards (fire, earthquake) share one table: same shape, one map layer.
CREATE TABLE IF NOT EXISTS hazard_events (
    id TEXT PRIMARY KEY, kind TEXT, title TEXT, lat REAL, lon REAL,
    magnitude REAL, occurred_on TEXT, source TEXT, url TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, title TEXT, hazard TEXT, lat REAL, lon REAL,
    occurred_on TEXT, source TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, title TEXT, url TEXT, published TEXT,
    source TEXT, districts TEXT
);
-- Health facilities and other BIPAD resources. A register, not a feed:
-- refreshed daily rather than every cycle.
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY, kind TEXT, title TEXT, title_ne TEXT,
    lat REAL, lon REAL, ward INTEGER, updated TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    started TEXT PRIMARY KEY, finished TEXT, ok INTEGER, notes TEXT
);
-- One row per alert this system raised (a band crossing into WARNING+, or an
-- impoundment signal). Append-only log, later updated in place by
-- prediction_log.verify_pending() once there has been time for an official
-- incident or a news report to appear -- or not. See prediction_log.py.
CREATE TABLE IF NOT EXISTS prediction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER, station_name TEXT, district TEXT, basin TEXT,
    lat REAL, lon REAL, ts TEXT, kind TEXT,
    fsi REAL, band TEXT, p_exceed_6h REAL, hours_to_danger REAL, reason TEXT,
    verified_at TEXT, corroborated INTEGER, corroboration_source TEXT,
    corroboration_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(station_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_scores_ts   ON scores(ts DESC);
CREATE INDEX IF NOT EXISTS idx_hazard_kind ON hazard_events(kind, occurred_on DESC);
-- Nearest-facility lookups scan by bounding box before computing haversine.
CREATE INDEX IF NOT EXISTS idx_resources_geo ON resources(kind, lat, lon);
CREATE INDEX IF NOT EXISTS idx_pred_station_kind ON prediction_events(station_id, kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pred_pending ON prediction_events(corroborated, ts);
"""


@contextmanager
def conn():
    """Yield a row-dict connection; commits on clean exit."""
    c = sqlite3.connect(settings.db_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")       # concurrent reads while a cycle writes
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
