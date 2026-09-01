"""One collection cycle: scrape -> clean -> score -> analyse -> persist -> export.

Each source is isolated. A dead news feed, a missing FIRMS key, or a BIPAD
timeout degrades the score (that component simply contributes nothing) instead
of losing the river data, which is the part that actually matters.

Order is deliberate: rainfall and the hazard feeds all key off the CLEANED
station list, so cleaning happens before anything spatial or numeric.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from dateutil.parser import parse as dtparse

from . import analytics, clean, db, excel
from .config import settings
from .hazards import outburst
from .hazards.fire import FireSpider
from .hazards.quake import QuakeSpider
from .scoring import score_station
from .spiders.base import new_client
from .spiders.bipad import BipadIncidentSpider
from .spiders.dhm_river import DhmRiverSpider
from .spiders.news import NewsSpider
from .spiders.rainfall import RainfallSpider
from .spiders.resources import ResourceSpider

log = logging.getLogger("pipeline")

# How many past readings feed the forecast and the impoundment baseline.
HISTORY_WINDOW = 24

LAST_RUN: dict = {"started": None, "finished": None, "sources": {}, "stations": 0}

# Bumped once per completed cycle. The SSE endpoint watches this rather than
# polling the database: it is the one authoritative "something changed" signal,
# and an integer comparison is cheaper than a query.
CYCLE_SEQ = 0

# The resource register barely changes (BIPAD's own records date from 2022), so
# it is refreshed daily. Re-pulling 16k rows every 12 minutes would be waste.
RESOURCE_REFRESH_HOURS = 24


async def _safe(name: str, coro):
    """Run a spider, record its outcome, never raise into the cycle."""
    try:
        items = await coro
        LAST_RUN["sources"][name] = {"ok": True, "items": len(items)}
        return items
    except Exception as exc:                      # noqa: BLE001 - deliberate catch-all
        log.warning("spider %s failed: %s", name, exc)
        LAST_RUN["sources"][name] = {"ok": False, "error": str(exc)[:200]}
        return []


def _history(station_ids: list[int]) -> dict[int, list]:
    """Recent (ts, level) per station, oldest-first: feeds forecast + baseline."""
    if not station_ids:
        return {}
    out: dict[int, list] = {sid: [] for sid in station_ids}
    with db.conn() as c:
        # noqa: S608 -- only the PLACEHOLDERS are interpolated ("?,?,?"); the
        # ids themselves are passed as bound parameters below and never reach
        # the SQL text. sqlite3 has no way to bind a variable-length IN list.
        placeholders = ",".join("?" * len(station_ids))
        query = (  # noqa: S608 - see comment above; only placeholders interpolate
            "SELECT station_id, ts, level FROM readings "
            f"WHERE station_id IN ({placeholders}) "
            "ORDER BY station_id, ts DESC"
        )
        rows = c.execute(query, station_ids).fetchall()
    for r in rows:
        bucket = out.setdefault(r["station_id"], [])
        if len(bucket) < HISTORY_WINDOW:
            bucket.append((r["ts"], r["level"]))
    return {k: list(reversed(v)) for k, v in out.items()}


# Retention. Without this the database grows forever, and -- worse -- rows
# written by an older, buggier version of a spider linger and get served as if
# they were current. Anything the model actually uses has a much shorter
# horizon than these limits.
RETENTION_DAYS = {
    # Readings are IRREPLACEABLE and cheap. DHM publishes only the current
    # value -- there is no historical API -- so a reading not captured is gone
    # for good, and every predictive signal (rise rate, forecast, impoundment
    # baseline) is computed from this table. 90 days costs ~27 MB. Keep it.
    "readings": 90,
    # Scores are DERIVABLE from readings + rainfall and were 83% of the
    # database at 30 days (~132 MB) for data the UI only ever reads back a few
    # days of. Seven days covers the history charts and the trend display;
    # anything older can be recomputed if it is ever wanted.
    "scores": 7,
    "incidents": 60,
    "news": 30,
    "hazard_events": 90,
    "cycles": 30,
}
_TS_COLUMN = {
    "readings": "ts", "scores": "ts", "incidents": "occurred_on",
    "news": "published", "hazard_events": "occurred_on", "cycles": "started",
}


def prune() -> dict:
    """Drop rows past their retention horizon. Returns what was removed."""
    removed = {}
    with db.conn() as c:
        for table, days in RETENTION_DAYS.items():
            cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
            col = _TS_COLUMN[table]
            # Only delete rows with a parseable timestamp; a blank one is a data
            # bug, and silently deleting it would hide that.
            cur = c.execute(
                f"DELETE FROM {table} WHERE {col} IS NOT NULL AND {col} != '' AND {col} < ?",
                (cutoff,))
            if cur.rowcount > 0:
                removed[table] = cur.rowcount
    return removed


def _resources_are_stale() -> bool:
    """True when the resource register has never been loaded or has aged out."""
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*), MAX(updated) FROM resources").fetchone()
    if not row or not row[0]:
        return True
    with db.conn() as c:
        last = c.execute(
            "SELECT finished FROM cycles WHERE notes LIKE '%resources%' ORDER BY finished DESC LIMIT 1"
        ).fetchone()
    if not last or not last[0]:
        return True
    try:
        age = datetime.now().astimezone() - dtparse(last[0])
    except (ValueError, TypeError):
        return True
    return age > timedelta(hours=RESOURCE_REFRESH_HOURS)


def _persist_resources(rows) -> int:
    if not rows:
        return 0
    with db.conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO resources
               (id,kind,title,title_ne,lat,lon,ward,updated,source)
               VALUES (:id,:kind,:title,:title_ne,:lat,:lon,:ward,:updated,:source)""",
            rows)
    return len(rows)


def _persist(stations, rainfall, incidents, news, scores, hazards) -> None:
    with db.conn() as c:
        c.executemany(
            """INSERT INTO stations (id,name,basin,district,lat,lon,warning_level,danger_level,series_id)
               VALUES (:id,:name,:basin,:district,:lat,:lon,:warning_level,:danger_level,:series_id)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, basin=excluded.basin,
                 district=excluded.district, lat=excluded.lat, lon=excluded.lon,
                 warning_level=excluded.warning_level, danger_level=excluded.danger_level""",
            stations)
        c.executemany(
            "INSERT OR IGNORE INTO readings (station_id,ts,level,status,steady) VALUES (?,?,?,?,?)",
            [(s["id"], s["ts"], s["level"], s["status"], s["steady"])
             for s in stations if s["ts"] and s["level"] is not None])
        c.executemany(
            """INSERT INTO rainfall (station_id,ts,past_24h,next_12h)
               VALUES (:station_id,:ts,:past_24h,:next_12h)
               ON CONFLICT(station_id) DO UPDATE SET ts=excluded.ts,
                 past_24h=excluded.past_24h, next_12h=excluded.next_12h""",
            rainfall)
        c.executemany(
            """INSERT OR REPLACE INTO incidents (id,title,hazard,lat,lon,occurred_on,source,url)
               VALUES (:id,:title,:hazard,:lat,:lon,:occurred_on,:source,:url)""",
            incidents)
        c.executemany(
            """INSERT OR REPLACE INTO news (id,title,url,published,source,districts)
               VALUES (:id,:title,:url,:published,:source,:districts)""",
            news)
        c.executemany(
            """INSERT OR REPLACE INTO scores
               (station_id,ts,fsi,band,p_exceed_6h,rise_rate,components,
                impoundment_suspected,impoundment_reason,hours_to_danger)
               VALUES (:station_id,:ts,:fsi,:band,:p_exceed_6h,:rise_rate,:components,
                       :impoundment_suspected,:impoundment_reason,:hours_to_danger)""",
            scores)
        c.executemany(
            """INSERT OR REPLACE INTO hazard_events
               (id,kind,title,lat,lon,magnitude,occurred_on,source,url,extra)
               VALUES (:id,:kind,:title,:lat,:lon,:magnitude,:occurred_on,:source,:url,:extra)""",
            hazards)


def _normalise_hazards(quakes, fires) -> list[dict]:
    """Fold two very different feeds into one table shape for the map layer."""
    out = []
    for q in quakes:
        if not clean.in_nepal(q["lat"], q["lon"]):
            continue
        out.append({
            "id": q["id"], "kind": "earthquake", "title": clean.norm_text(q["title"]),
            "lat": q["lat"], "lon": q["lon"], "magnitude": q["magnitude"],
            "occurred_on": clean.norm_timestamp(q["occurred_on"]) or "",
            "source": q["source"], "url": q["url"],
            "extra": json.dumps({"depth_km": q["depth_km"],
                                 "landslide_trigger": q["landslide_trigger"]}),
        })
    for f in fires:
        if not clean.in_nepal(f["lat"], f["lon"]):
            continue
        out.append({
            "id": f["id"], "kind": "fire",
            "title": f"Active fire ({f.get('confidence', 'n/a')} confidence)",
            "lat": f["lat"], "lon": f["lon"], "magnitude": f.get("frp_mw"),
            "occurred_on": clean.norm_timestamp(f["occurred_on"]) or "",
            "source": f["source"], "url": f["url"],
            "extra": json.dumps({"frp_mw": f.get("frp_mw"),
                                 "brightness_k": f.get("brightness_k"),
                                 "daynight": f.get("daynight")}),
        })
    return out


def _write_snapshot(stations, scores, hazards, quality) -> None:
    """Plain-JSON mirror of current state, for anything that is not this UI."""
    by_id = {s["id"]: s for s in stations}
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "quality": quality,
        "stations": [
            {**by_id.get(sc["station_id"], {}),
             "fsi": sc["fsi"], "band": sc["band"], "p_exceed_6h": sc["p_exceed_6h"],
             "rise_rate": sc["rise_rate"], "hours_to_danger": sc["hours_to_danger"],
             "impoundment_suspected": bool(sc["impoundment_suspected"]),
             "components": json.loads(sc["components"])}
            for sc in sorted(scores, key=lambda s: s["fsi"], reverse=True)
        ],
        "hazards": hazards,
    }
    tmp = settings.json_path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(settings.json_path)


async def run_cycle() -> dict:
    """Scrape every source once, rescore every gauge, refresh both exports."""
    db.init()
    started = datetime.now().astimezone()
    LAST_RUN.update(started=started.isoformat(timespec="seconds"), finished=None, sources={})

    async with new_client() as client:
        raw_stations = await _safe("dhm_river", DhmRiverSpider(client).run())
        raw_incidents, raw_news, quakes, fires = await asyncio.gather(
            _safe("bipad", BipadIncidentSpider(client).run()),
            _safe("news", NewsSpider(client).run()),
            _safe("quake", QuakeSpider(client).run()),
            _safe("fire", FireSpider(client).run()),
        )

        # Clean before anything spatial or numeric touches the data.
        cleaned = clean.clean_batch(raw_stations, raw_incidents, raw_news)
        stations, incidents, news = cleaned["stations"], cleaned["incidents"], cleaned["news"]

        rainfall = await _safe("rainfall", RainfallSpider(client).run(stations))

        # Health facilities: only when the register is stale.
        if _resources_are_stale():
            rows = await _safe("resources", ResourceSpider(client).run("health"))
            n = _persist_resources([r for r in rows if clean.in_nepal(r["lat"], r["lon"])])
            log.info("resource register refreshed: %d health facilities", n)

    history = _history([s["id"] for s in stations])
    rain_by_id = {r["station_id"]: r for r in rainfall}
    hazards = _normalise_hazards(quakes, fires)

    scores = []
    for s in stations:
        past = history.get(s["id"], [])
        prev_ts, prev_level = past[-1] if past else (None, None)

        # Reject physically impossible jumps rather than scoring a sensor fault.
        if clean.reject_stage_outlier(s["level"], s["ts"], prev_level, prev_ts):
            log.warning("rejected outlier at %s: %s -> %s", s["name"], prev_level, s["level"])
            s["level"] = None
        # No new packet this cycle -> no delta, so the rise component stays 0.
        if prev_ts == s["ts"]:
            prev_level, prev_ts = None, None

        # `past` goes in so the exceedance probability can widen itself on a
        # noisy gauge instead of reporting 100% off one jittery reading pair.
        score = score_station(s, prev_level, prev_ts, rain_by_id.get(s["id"]),
                              incidents, news, history=past)

        rain = rain_by_id.get(s["id"], {})
        signal = outburst.detect_impoundment(
            s, past, rain.get("past_24h", 0.0), rain.get("next_12h", 0.0))
        score["impoundment_suspected"] = int(signal.suspected)
        score["impoundment_reason"] = signal.reason
        score["hours_to_danger"] = analytics.time_to_danger(
            s["level"], score["rise_rate"], s["danger_level"])

        # An impoundment signal escalates a gauge the plain score would call calm,
        # because its diagnostic symptom is a FALLING river. Floor the band at
        # WARNING so the outburst protocol surfaces instead of "no action".
        if signal.suspected and score["fsi"] < 50:
            score["fsi"], score["band"] = 50.0, "WARNING"

        scores.append(score)

    _persist(stations, rainfall, incidents, news, scores, hazards)
    pruned = prune()
    if pruned:
        log.info("pruned expired rows: %s", pruned)
    excel.export(stations, scores, rain_by_id, incidents, news)
    _write_snapshot(stations, scores, hazards, cleaned["quality"])

    LAST_RUN.update(
        finished=datetime.now().astimezone().isoformat(timespec="seconds"),
        stations=len(stations),
        cycle_minutes=settings.cycle_minutes,
        quality=cleaned["quality"],
        impoundment_alerts=sum(s["impoundment_suspected"] for s in scores),
        pruned=pruned,
    )
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO cycles (started,finished,ok,notes) VALUES (?,?,?,?)",
                  (LAST_RUN["started"], LAST_RUN["finished"], int(bool(stations)),
                   json.dumps(LAST_RUN["sources"])))
    global CYCLE_SEQ
    CYCLE_SEQ += 1
    LAST_RUN["seq"] = CYCLE_SEQ
    log.info("cycle done: %d stations scored, %d hazard events", len(scores), len(hazards))
    return LAST_RUN
