"""FastAPI app: serves the dashboard, the JSON API, the tile cache and exports.

The scheduler runs one pipeline cycle every settings.cycle_minutes (default 12)
plus one at startup, so the map is never empty on first load.
"""
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, db, emergency, logs, pipeline, regions, relief, tiles
from .config import NEPAL_BBOX, ROOT, settings
from .hazards import earth_rotation, outburst
from .scoring import BANDS, haversine_km

logs.setup()
log = logging.getLogger("app")

FRONTEND = ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    app.state.tile_client = httpx.AsyncClient(timeout=20, follow_redirects=True)
    sched = AsyncIOScheduler(timezone="Asia/Kathmandu")
    sched.add_job(pipeline.run_cycle, "interval", minutes=settings.cycle_minutes,
                  id="cycle", max_instances=1, coalesce=True)
    sched.start()
    asyncio.create_task(pipeline.run_cycle())      # warm start, non-blocking
    log.info("scheduler running every %d min", settings.cycle_minutes)
    yield
    sched.shutdown(wait=False)
    await app.state.tile_client.aclose()


app = FastAPI(title="Nepal Flood Watch", version="1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def _latest_scores() -> list[dict]:
    """Newest score per station, joined to its station row, reading and rainfall."""
    with db.conn() as c:
        rows = c.execute("""
            SELECT st.id, st.name, st.district, st.basin, st.lat, st.lon,
                   st.warning_level, st.danger_level,
                   sc.fsi, sc.band, sc.p_exceed_6h, sc.rise_rate, sc.components,
                   sc.ts AS scored_at, sc.impoundment_suspected, sc.impoundment_reason,
                   sc.hours_to_danger,
                   r.level, r.ts AS reading_ts, r.steady,
                   rf.past_24h, rf.next_12h
            FROM stations st
            JOIN scores sc ON sc.station_id = st.id
                AND sc.ts = (SELECT MAX(ts) FROM scores WHERE station_id = st.id)
            LEFT JOIN readings r ON r.station_id = st.id
                AND r.ts = (SELECT MAX(ts) FROM readings WHERE station_id = st.id)
            LEFT JOIN rainfall rf ON rf.station_id = st.id
            ORDER BY sc.fsi DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["components"] = json.loads(d["components"] or "{}")
        d["impoundment_suspected"] = bool(d["impoundment_suspected"])
        out.append(d)
    return out


def _level_history(station_id: int, limit: int = 96) -> list[tuple]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT ts, level FROM readings WHERE station_id=? ORDER BY ts DESC LIMIT ?",
            (station_id, limit)).fetchall()
    return [(r["ts"], r["level"]) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------
@app.get("/api/stations")
def stations(band: str | None = None, min_fsi: float = 0.0):
    """All gauges with their current score. Optional band / minimum-FSI filter."""
    rows = [r for r in _latest_scores() if r["fsi"] >= min_fsi]
    if band:
        rows = [r for r in rows if r["band"] == band.upper()]
    return rows


@app.get("/api/summary")
def summary():
    rows = _latest_scores()
    counts = {label: 0 for _, label in BANDS}
    for r in rows:
        counts[r["band"]] = counts.get(r["band"], 0) + 1
    return {
        "total_stations": len(rows),
        "reporting": sum(1 for r in rows if r["level"] is not None),
        "bands": counts,
        "at_risk": sum(1 for r in rows if r["fsi"] >= 50),
        "impoundment_alerts": sum(1 for r in rows if r["impoundment_suspected"]),
        "max_fsi": max((r["fsi"] for r in rows), default=0),
        "highest_p6h": max((r["p_exceed_6h"] or 0 for r in rows), default=0),
        "soonest_hours_to_danger": min(
            (r["hours_to_danger"] for r in rows if r["hours_to_danger"] is not None),
            default=None),
        "last_cycle": pipeline.LAST_RUN,
    }


@app.get("/api/station/{station_id}")
def station_detail(station_id: int):
    """Full analytics bundle for one gauge: descriptive -> prescriptive."""
    row = next((r for r in _latest_scores() if r["id"] == station_id), None)
    if not row:
        raise HTTPException(404, "unknown station")
    history = _level_history(station_id)
    score = {**row, "impoundment_suspected": row["impoundment_suspected"]}
    return {
        **analytics.analyse_station(row, score, history),
        "history": [{"ts": t, "level": lv} for t, lv in history],
        "warning_level": row["warning_level"],
        "danger_level": row["danger_level"],
        # Surfaced at the top level because the UI verdict quotes it directly.
        "rise_rate": row["rise_rate"],
        "impoundment_suspected": row["impoundment_suspected"],
        "impoundment_reason": row["impoundment_reason"],
    }


@app.get("/api/hazards")
def hazards(kind: str | None = None, limit: int = 500):
    """Fire and earthquake events, for the non-flood map layers."""
    sql = "SELECT * FROM hazard_events"
    params: list = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY occurred_on DESC LIMIT ?"
    params.append(limit)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    for r in rows:
        r["extra"] = json.loads(r["extra"] or "{}")
    return rows


@app.get("/api/incidents")
def incidents(limit: int = 200):
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM incidents ORDER BY occurred_on DESC LIMIT ?", (limit,))]


@app.get("/api/news")
def news(limit: int = 60):
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM news ORDER BY rowid DESC LIMIT ?", (limit,))]


@app.get("/api/regions")
def region_list():
    """Available regions. Nepal is live; the rest are declared but disabled."""
    return {"default": regions.DEFAULT_REGION, "regions": regions.listing()}


def _district_centroids() -> dict[str, list]:
    """Real centroids from gauge coordinates, with the curated table as fallback.

    Deriving these from live station data keeps them accurate for the districts
    that matter (the ones with rivers) without hardcoding 77 approximations.
    """
    region = regions.get()
    centroids = dict(region.centroids)
    with db.conn() as c:
        rows = c.execute("""
            SELECT district, AVG(lat) AS lat, AVG(lon) AS lon
            FROM stations WHERE district != '' GROUP BY district
        """).fetchall()
    for r in rows:
        centroids[r["district"]] = [round(r["lat"], 4), round(r["lon"], 4)]
    return centroids


@app.get("/api/events")
def events(limit: int = 150):
    """Placeable 'major event' markers: alerts, official incidents, and news.

    News items carry only a district name, so they are pinned to that district's
    centroid and explicitly marked `approximate` -- an operator must not read a
    headline pin as a surveyed location.
    """
    centroids = _district_centroids()
    out = []

    # 1. Model alerts: the gauges the system itself is calling out.
    for s in _latest_scores():
        if s["fsi"] < 75 and not s["impoundment_suspected"]:
            continue
        out.append({
            "kind": "impoundment" if s["impoundment_suspected"] else "alert",
            "title": s["name"],
            "detail": (s["impoundment_reason"] if s["impoundment_suspected"]
                       else f"FSI {s['fsi']} {s['band']}"),
            "lat": s["lat"], "lon": s["lon"], "approximate": False,
            "severity": s["fsi"], "when": s["scored_at"],
            "url": "", "source": "Nepal Flood Watch model",
            "station_id": s["id"],
        })

    # 2. Official incidents -- these have real coordinates.
    with db.conn() as c:
        for r in c.execute(
            "SELECT * FROM incidents ORDER BY occurred_on DESC LIMIT ?", (limit,)
        ):
            out.append({
                "kind": "incident", "title": r["title"], "detail": r["hazard"],
                "lat": r["lat"], "lon": r["lon"], "approximate": False,
                "severity": 60, "when": r["occurred_on"],
                "url": r["url"], "source": r["source"],
            })

        # 3. News, pinned to district centroid where we can resolve one.
        for r in c.execute(
            "SELECT * FROM news ORDER BY rowid DESC LIMIT ?", (limit,)
        ):
            for d in (r["districts"] or "").split(","):
                pos = centroids.get(d.strip())
                if not pos:
                    continue
                out.append({
                    "kind": "news", "title": r["title"], "detail": d.strip(),
                    "lat": pos[0], "lon": pos[1], "approximate": True,
                    "severity": 40, "when": r["published"],
                    "url": r["url"], "source": r["source"],
                })

    out.sort(key=lambda e: e["severity"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Outburst / explanation endpoints
# ---------------------------------------------------------------------------
@app.get("/api/outburst/alerts")
def outburst_alerts():
    """Gauges currently showing the impoundment signature (falling while raining)."""
    return [r for r in _latest_scores() if r["impoundment_suspected"]]


@app.get("/api/outburst/scenario")
def outburst_scenario(volume_m3: float | None = None, head_m: float | None = None):
    """Breach model. Defaults to the Rasuwa 2025 reference case.

    Pass volume_m3 and head_m to model a specific barrier instead.
    """
    if volume_m3 and head_m:
        return outburst.breach_hydrograph(volume_m3, head_m, outburst.RASUWA_2025["targets"])
    return outburst.reference_scenario()


@app.get("/api/outburst/stability")
def outburst_stability(dam_height_m: float, dam_volume_m3: float, catchment_area_km2: float):
    """Ermini & Casagli DBI for a barrier that has already formed."""
    return outburst.stability_index(dam_height_m, dam_volume_m3, catchment_area_km2)


@app.get("/api/explain/earth-rotation")
def explain_earth_rotation():
    """Worked answer on dams, length-of-day, and whether it affects flooding."""
    return earth_rotation.report()


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"cycle_minutes": settings.cycle_minutes, "bbox": NEPAL_BBOX, **pipeline.LAST_RUN}


@app.post("/api/refresh")
async def refresh():
    """Force a cycle now (the UI's refresh button)."""
    return await pipeline.run_cycle()


@app.post("/api/tiles/prefetch")
async def tiles_prefetch(style: str = "dark"):
    """Warm the offline tile cache for Nepal. Takes a few minutes on first run."""
    if style not in tiles.STYLES:
        raise HTTPException(400, f"style must be one of {list(tiles.STYLES)}")
    return await tiles.prefetch(style)


@app.get("/api/tiles/{style}/{z}/{x}/{y}.png")
async def tile(style: str, z: int, x: int, y: int):
    """Cached map tile. Misses are fetched upstream once, then served from disk."""
    if style not in tiles.STYLES:
        raise HTTPException(404, "unknown style")
    data = await tiles.fetch_tile(app.state.tile_client, style, z, x, y)
    if data is None:
        raise HTTPException(404, "tile outside Nepal or unavailable")
    # Sniff rather than assume: the canvas basemaps are JPEG despite the .png
    # route, and declaring the wrong type breaks strict image decoders.
    kind = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(data, media_type=kind,
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/nearby")
def nearby(lat: float, lon: float, radius_km: float = 30.0, limit: int = 40):
    """Ground truth around one point: BIPAD incidents, gauges, quakes, headlines.

    Satellite imagery says what is on the ground; BIPAD says what someone
    actually reported happening there. Neither is sufficient alone -- a
    250 m MODIS pixel cannot see a washed-out footbridge, and an incident
    report cannot show how far the water has spread. The Explore panel puts
    them side by side deliberately.

    Everything is returned with its distance so the operator can judge
    relevance rather than trusting a radius we picked.
    """
    out = {"incidents": [], "gauges": [], "hazards": [], "news": []}

    with db.conn() as c:
        for r in c.execute("SELECT * FROM incidents ORDER BY occurred_on DESC LIMIT 500"):
            d = haversine_km(lat, lon, r["lat"], r["lon"])
            if d <= radius_km:
                out["incidents"].append({**dict(r), "distance_km": round(d, 1)})

        for r in c.execute("SELECT * FROM hazard_events ORDER BY occurred_on DESC LIMIT 500"):
            d = haversine_km(lat, lon, r["lat"], r["lon"])
            if d <= radius_km:
                item = {**dict(r), "distance_km": round(d, 1)}
                item["extra"] = json.loads(item["extra"] or "{}")
                out["hazards"].append(item)

    for st in _latest_scores():
        if st["lat"] is None:
            continue
        d = haversine_km(lat, lon, st["lat"], st["lon"])
        if d <= radius_km:
            out["gauges"].append({
                "id": st["id"], "name": st["name"], "fsi": st["fsi"], "band": st["band"],
                "level": st["level"], "danger_level": st["danger_level"],
                "impoundment_suspected": st["impoundment_suspected"],
                "distance_km": round(d, 1),
            })

    # News carries only a district, so match on the districts of the gauges in
    # range rather than pretending a headline has coordinates.
    districts = {g_st["district"] for g_st in _latest_scores()
                 if g_st["lat"] is not None and g_st["district"]
                 and haversine_km(lat, lon, g_st["lat"], g_st["lon"]) <= radius_km}
    if districts:
        with db.conn() as c:
            for r in c.execute("SELECT * FROM news ORDER BY rowid DESC LIMIT 200"):
                hit = {d.strip() for d in (r["districts"] or "").split(",") if d.strip()}
                if hit & districts:
                    out["news"].append({**dict(r), "matched": sorted(hit & districts)})

    for key in out:
        out[key].sort(key=lambda i: i.get("distance_km", 999))
        out[key] = out[key][:limit]

    out["query"] = {"lat": lat, "lon": lon, "radius_km": radius_km}
    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    return out


@app.get("/api/relief")
def relief_channels():
    """Official donation channels. Links only -- see relief.py for why."""
    return relief.channels()


@app.get("/api/stream")
async def stream():
    """Server-sent events: one message per completed collection cycle.

    Replaces a 60-second poll. The console is watched for hours at a time, and
    polling meant a new DANGER reading could sit unseen for most of a minute
    while the browser re-fetched three endpoints it already had. This pushes
    once, when something has actually changed.

    A heartbeat every 20s keeps proxies from closing an idle connection, and
    gives the client something to notice if the server goes away.
    """
    async def events():
        last = pipeline.CYCLE_SEQ
        # Tell a reconnecting client where we are before it waits.
        yield f"event: hello\ndata: {json.dumps({'seq': last})}\n\n"
        idle = 0
        while True:
            await asyncio.sleep(2)
            if pipeline.CYCLE_SEQ != last:
                last = pipeline.CYCLE_SEQ
                idle = 0
                payload = {"seq": last, "finished": pipeline.LAST_RUN.get("finished"),
                           "stations": pipeline.LAST_RUN.get("stations")}
                yield f"event: cycle\ndata: {json.dumps(payload)}\n\n"
            else:
                idle += 2
                if idle >= 20:
                    idle = 0
                    yield ": keepalive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",      # nginx would otherwise buffer the stream
    })


@app.get("/api/emergency")
def emergency_contacts(district: str | None = None):
    """Verified emergency numbers, national plus district where we have them."""
    return emergency.contacts(district)


@app.get("/api/facilities/nearest")
def nearest_facilities(lat: float, lon: float, limit: int = 10,
                       radius_km: float = 25.0, kind: str = "health"):
    """Closest health facilities to a point, nearest first.

    Filters by bounding box in SQL before computing haversine in Python. Over
    16,299 facilities, doing the trigonometry on every row would be pointless
    work for a query that runs on every map click.
    """
    # 1 degree of latitude is ~111 km; longitude shrinks with latitude.
    import math
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))

    with db.conn() as c:
        rows = c.execute(
            """SELECT * FROM resources
               WHERE kind = ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
            (kind, lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()

    out = []
    for r in rows:
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        if d <= radius_km:
            out.append({**dict(r), "distance_km": round(d, 2)})
    out.sort(key=lambda f: f["distance_km"])
    return {"count": len(out), "returned": min(limit, len(out)),
            "radius_km": radius_km, "facilities": out[:limit]}


@app.get("/api/imagery/options")
def imagery_options():
    """Available imagery and, importantly, how current each option actually is."""
    return tiles.imagery_options()


@app.get("/api/satellite/{z}/{x}/{y}.jpg")
async def satellite_tile(z: int, x: int, y: int):
    """High-resolution satellite mosaic (Esri). Detailed but NOT current."""
    data = await tiles.fetch_satellite(app.state.tile_client, z, x, y)
    if data is None:
        raise HTTPException(404, "tile outside Nepal or unavailable")
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/gibs/{layer}/{date}/{z}/{x}/{y}.jpg")
async def gibs_tile(layer: str, date: str, z: int, x: int, y: int):
    """NASA GIBS daily imagery. Coarse, but genuinely from the last 24-48 h."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    data = await tiles.fetch_gibs(app.state.tile_client, layer, date, z, x, y)
    if data is None:
        raise HTTPException(404, "no imagery for that layer, date or location")
    # Dated imagery never changes, so it can be cached hard.
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=2592000"})


@app.get("/api/export.xlsx")
def export_xlsx():
    if not Path(settings.excel_path).exists():
        raise HTTPException(404, "workbook not generated yet")
    return FileResponse(
        settings.excel_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="nepal_flood_watch.xlsx")


@app.get("/api/export.json")
def export_json():
    if not Path(settings.json_path).exists():
        raise HTTPException(404, "snapshot not generated yet")
    return FileResponse(settings.json_path, media_type="application/json",
                        filename="nepal_flood_watch.json")


# Mounted last so every /api/* route above wins.
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
