"""Prediction verification: does an alert this system raises get echoed by an
official incident or a news headline afterward?

Every other accuracy claim in this codebase is checked against something
real: the forecast is backtested against a naive baseline before it is
trusted (models/__init__.py), and the outburst constants are calibrated
against Tangjiashan and Rasuwa (hazards/outburst.py). The one number nobody
was checking was the ordinary flood alert itself -- when a gauge crosses
WARNING or the impoundment detector fires, does that correspond to anything a
human later reported?

Two functions, called every cycle from pipeline.py:

    record_events()   Logs a new row the first time a station crosses into
                       WARNING+ or impoundment_suspected turns true, with a
                       cooldown so one sustained alert does not write a row
                       every 12 minutes.

    verify_pending()  Checks events old enough that an official response has
                       had a chance to appear: an incident within
                       CORROB_RADIUS_KM, or a news headline naming the
                       station's district, occurring AFTER the alert.

Forward-looking is the whole point. Matching same-cycle corroboration would
just restate scoring.corroboration_component(), which is already an INPUT to
the FSI -- that would be circular, not independent evidence. An event that
gets no match within VERIFY_WINDOW_HOURS is marked unconfirmed, not deleted:
a flood that never happened is itself information about the alert.

Small sample size and coarse radius/district matching mean this is a
transparency instrument, not a validated accuracy metric. Report it as
"N of M alerts saw a matching report," never as an accuracy percentage that
implies more precision than a 25 km radius and a free-text district match can
support.

Run standalone for a report without waiting for the next cycle:

    python -m app.prediction_log
"""
import logging
from datetime import datetime, timedelta

from dateutil.parser import parse as dtparse

from . import db
from .scoring import CORROB_RADIUS_KM, haversine_km

log = logging.getLogger("prediction_log")

# A sustained alert must not write a new row every 12 minutes.
COOLDOWN_HOURS = 24
# Give official channels time to catch up before checking at all.
VERIFY_DELAY_HOURS = 6
# After this long with no match, the alert is marked unconfirmed -- not
# retried forever.
VERIFY_WINDOW_HOURS = 72
# How long the log itself is kept; see pipeline.RETENTION_DAYS.
RETENTION_DAYS = 180

ALERT_BANDS = {"WARNING", "DANGER", "SEVERE"}


def record_events(stations: list[dict], scores: list[dict]) -> int:
    """Log a new prediction event for any station newly crossing an alert
    threshold. Returns the number of rows written."""
    by_id = {s["id"]: s for s in stations}
    now = datetime.now().astimezone()
    cooldown_cutoff = (now - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    written = 0

    with db.conn() as c:
        for sc in scores:
            st = by_id.get(sc["station_id"])
            if not st or st.get("lat") is None:
                continue

            kinds = []
            if sc["band"] in ALERT_BANDS:
                kinds.append(f"band_{sc['band'].lower()}")
            if sc.get("impoundment_suspected"):
                kinds.append("impoundment")

            for kind in kinds:
                already_open = c.execute(
                    """SELECT 1 FROM prediction_events
                       WHERE station_id = ? AND kind = ? AND ts > ? LIMIT 1""",
                    (st["id"], kind, cooldown_cutoff)).fetchone()
                if already_open:
                    continue

                c.execute(
                    """INSERT INTO prediction_events
                       (station_id, station_name, district, basin, lat, lon, ts, kind,
                        fsi, band, p_exceed_6h, hours_to_danger, reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (st["id"], st["name"], st.get("district"), st.get("basin"),
                     st["lat"], st["lon"], now.isoformat(timespec="seconds"), kind,
                     sc["fsi"], sc["band"], sc.get("p_exceed_6h"), sc.get("hours_to_danger"),
                     sc.get("impoundment_reason") if kind == "impoundment" else None))
                written += 1
                log.info("prediction logged: %s at %s (FSI %.1f, %s)",
                          kind, st["name"], sc["fsi"], sc["band"])
    return written


def _safe_parse(value):
    if not value:
        return None
    try:
        return dtparse(value)
    except (ValueError, TypeError):
        return None


def _is_after(candidate, reference) -> bool:
    """candidate > reference, tolerating a naive/aware mismatch.

    Every timestamp this system writes itself goes through clean.py and
    carries the Nepal +05:45 offset, but a third-party feed occasionally
    slipping through naive is exactly the kind of messy input this project
    does not crash on (see clean.py) -- an incomparable pair is treated as
    "not after", i.e. not a match, never assumed to be one.
    """
    try:
        return candidate > reference
    except TypeError:
        return False


def find_match(event: dict, incidents: list[dict], news: list[dict]) -> tuple[str, str] | None:
    """An incident within CORROB_RADIUS_KM, or a district-matching headline,
    occurring strictly AFTER the event. Pure function: no DB access, so it is
    trivial to pin with synthetic data in tests.

    Returns (source, detail) on the first match found, else None.
    """
    event_time = _safe_parse(event["ts"])
    if event_time is None or event.get("lat") is None:
        return None

    for inc in incidents:
        occurred = _safe_parse(inc.get("occurred_on"))
        if occurred is None or not _is_after(occurred, event_time):
            continue
        if inc.get("lat") is None or inc.get("lon") is None:
            continue
        if haversine_km(event["lat"], event["lon"], inc["lat"], inc["lon"]) <= CORROB_RADIUS_KM:
            return ("incident", f"{inc.get('title', 'untitled incident')} ({inc.get('source', '?')})")

    district = (event.get("district") or "").strip().lower()
    if district:
        for n in news:
            published = _safe_parse(n.get("published"))
            if published is None or not _is_after(published, event_time):
                continue
            if district in (n.get("districts") or "").lower():
                return ("news", f"{n.get('title', 'untitled headline')} ({n.get('source', '?')})")

    return None


def verify_pending() -> dict:
    """Check every unverified event old enough to be worth checking.

    Reads incidents/news from the database rather than requiring the caller
    to pass this cycle's freshly-scraped batch -- both tables hold their full
    retention window (pipeline.RETENTION_DAYS), which is what an event logged
    days ago needs to be checked against, not just what happened to be
    scraped in the same 12-minute cycle as the check.
    """
    now = datetime.now().astimezone()
    delay_cutoff = (now - timedelta(hours=VERIFY_DELAY_HOURS)).isoformat()

    with db.conn() as c:
        pending = [dict(r) for r in c.execute(
            """SELECT * FROM prediction_events
               WHERE corroborated IS NULL AND ts <= ? ORDER BY ts""",
            (delay_cutoff,)).fetchall()]
        if not pending:
            return {"checked": 0, "confirmed": 0, "unconfirmed": 0, "still_pending": 0}

        incidents = [dict(r) for r in c.execute("SELECT * FROM incidents").fetchall()]
        news = [dict(r) for r in c.execute("SELECT * FROM news").fetchall()]

        confirmed = unconfirmed = 0
        for ev in pending:
            match = find_match(ev, incidents, news)
            ev_time = _safe_parse(ev["ts"])
            # A row this system wrote itself with an unparseable ts is a bug,
            # not messy third-party input -- treat it as already past the
            # window so it resolves to unconfirmed rather than looping forever.
            age_hours = ((now - ev_time).total_seconds() / 3600.0
                         if ev_time and ev_time.tzinfo else VERIFY_WINDOW_HOURS)

            if match:
                source, detail = match
                c.execute(
                    """UPDATE prediction_events
                       SET verified_at=?, corroborated=1, corroboration_source=?,
                           corroboration_detail=? WHERE id=?""",
                    (now.isoformat(timespec="seconds"), source, detail, ev["id"]))
                confirmed += 1
                log.info("prediction CONFIRMED: %s at %s -- %s: %s",
                          ev["kind"], ev["station_name"], source, detail)
            elif age_hours >= VERIFY_WINDOW_HOURS:
                c.execute(
                    """UPDATE prediction_events SET verified_at=?, corroborated=0 WHERE id=?""",
                    (now.isoformat(timespec="seconds"), ev["id"]))
                unconfirmed += 1
                log.info("prediction UNCONFIRMED: %s at %s -- no official report within %dh",
                          ev["kind"], ev["station_name"], VERIFY_WINDOW_HOURS)
            # else: not old enough to give up on yet; left pending for next cycle.

    return {"checked": len(pending), "confirmed": confirmed, "unconfirmed": unconfirmed,
            "still_pending": len(pending) - confirmed - unconfirmed}


def summary(days: int = 30) -> dict:
    """Served by /api/predictions/verification. Deliberately reports counts,
    not a percentage -- see the module docstring for why."""
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM prediction_events WHERE ts >= ? ORDER BY ts DESC", (cutoff,)
        ).fetchall()]

    confirmed = [r for r in rows if r["corroborated"] == 1]
    unconfirmed = [r for r in rows if r["corroborated"] == 0]
    pending = [r for r in rows if r["corroborated"] is None]

    by_kind: dict[str, dict] = {}
    for r in rows:
        b = by_kind.setdefault(r["kind"], {"total": 0, "confirmed": 0, "unconfirmed": 0, "pending": 0})
        b["total"] += 1
        if r["corroborated"] == 1:
            b["confirmed"] += 1
        elif r["corroborated"] == 0:
            b["unconfirmed"] += 1
        else:
            b["pending"] += 1

    return {
        "window_days": days,
        "total_events": len(rows),
        "confirmed": len(confirmed),
        "unconfirmed": len(unconfirmed),
        "pending": len(pending),
        "by_kind": by_kind,
        "recent_confirmed": confirmed[:15],
        "recent_unconfirmed": unconfirmed[:15],
        "scope": ("Alerts this system raised, checked against official BIPAD "
                  "incidents and district-matching news published AFTER the "
                  "alert. A 25 km radius and free-text district matching are "
                  "coarse by design -- read this as evidence of whether alerts "
                  "correspond to anything reported, not as a calibrated "
                  "accuracy percentage."),
    }


if __name__ == "__main__":
    db.init()
    result = verify_pending()
    print(f"Verified this run: {result}")
    report = summary(days=30)
    print(f"\nLast {report['window_days']} days: {report['total_events']} alerts logged, "
          f"{report['confirmed']} confirmed, {report['unconfirmed']} unconfirmed, "
          f"{report['pending']} still pending.")
    for kind, b in report["by_kind"].items():
        print(f"  {kind:14s} total={b['total']:3d} confirmed={b['confirmed']:3d} "
              f"unconfirmed={b['unconfirmed']:3d} pending={b['pending']:3d}")
    print(f"\n{report['scope']}")
