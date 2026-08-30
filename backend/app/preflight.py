"""Deployment preflight. Run before shipping or after changing a source.

    python -m app.preflight            # everything
    python -m app.preflight --offline  # skip live network checks

Exits 0 when every REQUIRED check passes. Optional checks (FIRMS without a key,
individual news feeds) report WARN and do not fail the run, because the system
is designed to keep working without them.
"""
import argparse
import asyncio
import json
import sys
import time

from . import analytics, clean, db, tiles
from .config import ROOT, settings
from .hazards import earth_rotation, outburst
from .hazards.quake import QuakeSpider
from .scoring import band_for, level_component, p_exceed_6h, score_station
from .spiders.base import new_client
from .spiders.bipad import BipadIncidentSpider
from .spiders.dhm_river import DhmRiverSpider
from .spiders.news import NewsSpider
from .spiders.rainfall import RainfallSpider

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name, required=True):
    """Decorator: run a check, capture its verdict, never let it crash the run."""
    def wrap(fn):
        def run(*a, **kw):
            t0 = time.perf_counter()
            try:
                detail = fn(*a, **kw)
                status = PASS
            except AssertionError as exc:
                status, detail = (FAIL if required else WARN), str(exc)
            except Exception as exc:                       # noqa: BLE001
                status, detail = (FAIL if required else WARN), f"{type(exc).__name__}: {exc}"
            ms = (time.perf_counter() - t0) * 1000
            results.append((name, status, f"{detail}  [{ms:.0f}ms]"))
            return status
        return run
    return wrap


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
@check("python version")
def c_python():
    assert sys.version_info >= (3, 11), f"need 3.11+, have {sys.version_info[:2]}"
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


@check("dependencies importable")
def c_deps():
    import apscheduler, fastapi, httpx, openpyxl, uvicorn        # noqa: F401
    return "fastapi, uvicorn, httpx, openpyxl, apscheduler"


@check("frontend assets present")
def c_frontend():
    missing = [f for f in ("index.html", "app.js", "styles.css")
               if not (ROOT / "frontend" / f).exists()]
    assert not missing, f"missing {missing}"
    return "index.html, app.js, styles.css"


@check("database writable")
def c_db():
    db.init()
    with db.conn() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert "stations" in tables and "scores" in tables, f"schema incomplete: {tables}"
    return f"{len(tables)} tables at {settings.db_path.name}"


@check("export paths writable")
def c_paths():
    for p in (settings.excel_path, settings.json_path):
        p.parent.mkdir(parents=True, exist_ok=True)
        probe = p.with_suffix(".probe")
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    return f"{settings.excel_path.parent}"


@check("scoring weights sum to 1.0")
def c_weights():
    total = (settings.w_level + settings.w_rise + settings.w_rain + settings.w_corroboration)
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}, not 1.0"
    return f"{total:.2f}"


# ---------------------------------------------------------------------------
# Model correctness -- these are the checks that catch a bad refactor
# ---------------------------------------------------------------------------
@check("severity bands monotonic")
def c_bands():
    seq = [band_for(f) for f in (0, 30, 60, 80, 95)]
    assert seq == ["NORMAL", "WATCH", "WARNING", "DANGER", "SEVERE"], seq
    # Level component must increase with stage.
    a = level_component(2.0, 4.0, 6.0)
    b = level_component(5.0, 4.0, 6.0)
    c = level_component(7.0, 4.0, 6.0)
    assert a < b < c, f"level component not monotonic: {a}, {b}, {c}"
    return "NORMAL<WATCH<WARNING<DANGER<SEVERE, level component monotonic"


@check("breach probability bounded and ordered")
def c_probability():
    low = p_exceed_6h(2.0, 6.0, 0.0, 0)
    high = p_exceed_6h(5.9, 6.0, 0.5, 100)
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0, "probability outside [0,1]"
    assert low < high, f"a fast-rising gauge near danger scored lower ({high}) than a calm one ({low})"
    return f"calm {low} < critical {high}"


@check("outburst physics matches published cases")
def c_outburst():
    # Tangjiashan (Wenchuan 2008) is the standard worked example: it was unstable
    # and required an emergency spillway.
    dbi = outburst.stability_index(82, 20.4e6, 3550)
    assert dbi["verdict"].startswith("unstable"), f"Tangjiashan should be unstable, got {dbi}"
    # Both peak-discharge relations must agree on order of magnitude.
    s = outburst.reference_scenario()
    lo, hi = s["peak_discharge_cumecs"]["envelope"]
    assert lo > 0 and hi / lo < 100, f"discharge envelope implausible: {lo}-{hi}"
    # Celerity must be a physically sane flood-wave speed for a mountain river.
    assert 3 < s["wave_celerity_ms"] < 20, f"celerity {s['wave_celerity_ms']} m/s out of range"
    # The transboundary list must speak DHM's basin vocabulary, not tributary
    # names -- an earlier version matched almost nothing.
    assert outburst.is_transboundary({"basin": "Narayani", "name": "Bhote Koshi at Rasuwagadi"}), \
        "Tibet-fed headwater not recognised as transboundary"
    assert not outburst.is_transboundary({"basin": "Bagmati", "name": "Nakkhu at Bungmati"}), \
        "valley stream wrongly treated as transboundary"

    # A shallow gauge must not fire on a large percentage of a tiny number.
    shallow = outburst.detect_impoundment(
        {"id": 1, "name": "Nakkhu at Bungmati", "basin": "Bagmati"},
        [("t1", 0.45), ("t2", 0.44), ("t3", 0.43), ("t4", 0.10)], 60.0, 0.0)
    assert not shallow.suspected, f"shallow gauge fired: {shallow.reason}"

    return (f"Tangjiashan DBI {dbi['dbi']} unstable; celerity {s['wave_celerity_ms']} m/s; "
            f"basin vocabulary and shallow-gauge guard hold")


@check("earth rotation calculation sane")
def c_rotation():
    r = earth_rotation.report()["calculations"][0]
    us = r["delta_length_of_day_us"]
    # Published estimates for Three Gorges are ~0.06 us; anything within an
    # order of magnitude means the moment-of-inertia terms are right.
    assert 0.01 < us < 1.0, f"Three Gorges dLOD {us} us is off by orders of magnitude"
    assert r["affects_river_discharge"] is False
    return f"Three Gorges {us} us/day, {r['times_smaller_than_seasonal_wobble']}x below seasonal"


@check("cleaning rejects bad data")
def c_clean():
    assert clean.norm_float(" ") is None, "blank should be None, not 0.0"
    assert clean.norm_float("N/A") is None
    assert clean.norm_float(" 2.34 m ") == 2.34
    assert clean.norm_district("sindhupalchowk") == "Sindhupalchok"
    assert clean.in_nepal(27.7, 85.3) and not clean.in_nepal(48.8, 2.3)
    # A 5 m jump in one hour is a sensor fault, not a flood.
    assert clean.reject_stage_outlier(7.0, "2026-08-29T12:00:00+05:45",
                                      2.0, "2026-08-29T11:00:00+05:45")
    return "blanks -> None, districts canonical, bbox enforced, outliers rejected"


@check("analytics produce a forecast")
def c_analytics():
    fc = analytics.holt_forecast([2.1, 2.2, 2.35, 2.5, 2.7, 2.95, 3.3], steps=6)
    assert len(fc.values) == 6, f"expected 6 horizons, got {len(fc.values)}"
    assert all(lo <= v <= hi for lo, v, hi in zip(fc.lower, fc.values, fc.upper)), \
        "forecast values fall outside their own prediction band"
    ttd = analytics.time_to_danger(3.3, 0.25, 4.5)
    assert ttd and 4.0 < ttd < 5.5, f"time to danger {ttd} h is wrong"
    # Prescriptive advice must drop actions there is no longer time for.
    p = analytics.prescribe("DANGER", 0.5)
    assert any(not a["feasible"] for a in p["actions"]), "no lead-time gating applied"
    return f"6h forecast, ttd {ttd} h, lead-time gating active"


@check("end-to-end scoring")
def c_scoring():
    station = {"id": 1, "name": "Test", "basin": "Koshi", "district": "Sunsari",
               "lat": 26.6, "lon": 87.2, "level": 5.5, "ts": "2026-08-29T12:00:00+05:45",
               "warning_level": 4.0, "danger_level": 6.0, "status": "", "steady": ""}
    s = score_station(station, 5.0, "2026-08-29T11:00:00+05:45",
                      {"past_24h": 80, "next_12h": 40}, [], [])
    assert 0 <= s["fsi"] <= 100, f"FSI {s['fsi']} out of range"
    assert s["band"] in {"NORMAL", "WATCH", "WARNING", "DANGER", "SEVERE"}
    assert s["rise_rate"] and abs(s["rise_rate"] - 0.5) < 1e-6, f"rise rate {s['rise_rate']}"
    json.loads(s["components"])
    return f"FSI {s['fsi']} {s['band']}, P(6h) {s['p_exceed_6h']}"


@check("tile maths clips to Nepal")
def c_tiles():
    assert tiles.in_nepal_tile(*tiles.deg2num(27.7, 85.3, 8), 8), "Kathmandu tile rejected"
    assert not tiles.in_nepal_tile(*tiles.deg2num(48.8, 2.3, 8), 8), "Paris tile accepted"
    # Just outside the border must still resolve, or the map renders as an island.
    assert tiles.in_nepal_tile(*tiles.deg2num(27.0, 89.0, 8), 8), "context buffer not applied"
    return f"Kathmandu in, Paris out, {tiles.CONTEXT_BUFFER_DEG} deg context buffer"


# ---------------------------------------------------------------------------
# Live sources
# ---------------------------------------------------------------------------
async def _live_checks():
    async with new_client() as client:

        @check("source: DHM river watch")
        def c_dhm(items):
            assert len(items) > 50, f"only {len(items)} gauges -- page format may have changed"
            with_level = [i for i in items if i["level"] is not None]
            assert with_level, "no gauge is reporting a water level"
            return f"{len(items)} gauges, {len(with_level)} reporting"

        @check("source: Open-Meteo rainfall")
        def c_rain(items):
            assert items, "no rainfall returned"
            return f"{len(items)} points"

        @check("source: BIPAD incidents")
        def c_bipad(items):
            hazards = {i["hazard"] for i in items}
            assert "fire" not in hazards, "hazard filter is not excluding non-water incidents"
            return f"{len(items)} water incidents: {sorted(hazards)}"

        @check("source: USGS earthquakes")
        def c_quake(items):
            return f"{len(items)} events in Nepal bbox, last 30 days"

        @check("source: news feeds", required=False)
        def c_news(spider, items):
            assert not spider.errors, f"feeds down: {spider.errors}"
            return f"{len(items)} flood headlines from {len(set(i['source'] for i in items))} feeds"

        @check("source: NASA FIRMS", required=False)
        def c_fire():
            assert settings.firms_map_key, \
                "FIRMS_MAP_KEY not set -- fire layer disabled (free key at firms.modaps.eosdis.nasa.gov)"
            return "key configured"

        stations = await DhmRiverSpider(client).run()
        cleaned = [s for s in (clean.clean_station(r) for r in stations) if s]
        c_dhm(cleaned)
        c_rain(await RainfallSpider(client).run(cleaned[:20]))
        c_bipad(await BipadIncidentSpider(client).run())
        c_quake(await QuakeSpider(client).run())
        news_spider = NewsSpider(client)
        c_news(news_spider, await news_spider.run())
        c_fire()


def main() -> int:
    ap = argparse.ArgumentParser(description="Nepal Flood Watch preflight")
    ap.add_argument("--offline", action="store_true", help="skip live source checks")
    args = ap.parse_args()

    print("\nNepal Flood Watch - deployment preflight\n" + "=" * 66)

    c_python(); c_deps(); c_frontend(); c_db(); c_paths(); c_weights()
    c_bands(); c_probability(); c_outburst(); c_rotation()
    c_clean(); c_analytics(); c_scoring(); c_tiles()

    if not args.offline:
        asyncio.run(_live_checks())
    else:
        results.append(("live sources", WARN, "skipped (--offline)"))

    icon = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
    for name, status, detail in results:
        print(f"  [{icon[status]}] {name:34s} {detail}")

    failed = sum(1 for _, s, _ in results if s == FAIL)
    warned = sum(1 for _, s, _ in results if s == WARN)
    print("=" * 66)
    print(f"  {len(results) - failed - warned} passed, {warned} warnings, {failed} failed")
    print("  READY TO DEPLOY\n" if not failed else "  NOT READY -- fix the failures above\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
