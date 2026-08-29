"""Flood Severity Index (FSI) and 6-hour breach probability.

FSI is 0-100 and blends four components, weighted in config.py:

  level  (50%)  how close the gauge sits to its official danger mark
  rise   (25%)  metres/hour of climb -- the lead indicator, a slow river at
                90% of danger is calmer than a fast one at 60%
  rain   (18%)  past 24 h observed + next 12 h forecast rainfall over the gauge
  corrob ( 7%)  BIPAD incidents / news headlines near the gauge in the last 48 h

Every component is normalised to 0-100 before weighting so the bands below stay
comparable across basins with very different absolute river heights.
"""
import json
import math
from datetime import datetime

from dateutil import parser as dtparse

from .config import settings

# Band thresholds. Kept as (floor, label) so the UI and Excel share one source.
BANDS = [(90, "SEVERE"), (75, "DANGER"), (50, "WARNING"), (25, "WATCH"), (0, "NORMAL")]

RISE_FULL_SCALE = 0.50      # m/h that saturates the rise component
RAIN_FULL_SCALE = 200.0     # mm (24 h past + 12 h forecast) that saturates rain
CORROB_RADIUS_KM = 25.0


def band_for(fsi: float) -> str:
    return next(label for floor, label in BANDS if fsi >= floor)


def haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371.0
    dlat, dlon = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def level_component(level, warning, danger) -> float:
    """Piecewise so the scale is anchored on DHM's own published marks."""
    if level is None:
        return 0.0
    if warning and level < warning:
        return _clamp(45.0 * (level / warning))               # 0-45 approaching warning
    if warning and danger and danger > warning:
        if level < danger:
            return 45.0 + 40.0 * (level - warning) / (danger - warning)   # 45-85
        return _clamp(85.0 + 15.0 * (level - danger) / max(danger * 0.1, 0.2))
    if warning and level >= warning:
        return 70.0                    # over warning, no danger mark published
    return 0.0


def rise_component(rise_rate: float | None) -> float:
    if not rise_rate or rise_rate <= 0:
        return 0.0
    return _clamp(100.0 * rise_rate / RISE_FULL_SCALE)


def rain_component(past_24h: float, next_12h: float) -> float:
    # Forecast rain is weighted 1.2x: it has not yet reached the channel, so it
    # is the part of the signal that is still ahead of the gauge.
    total = (past_24h or 0) + 1.2 * (next_12h or 0)
    return _clamp(100.0 * total / RAIN_FULL_SCALE)


def corroboration_component(station, incidents, news) -> float:
    """Nearby official incidents count double what a matching headline counts."""
    score = 0.0
    for inc in incidents:
        if haversine_km(station["lat"], station["lon"], inc["lat"], inc["lon"]) <= CORROB_RADIUS_KM:
            score += 40.0
    district = (station.get("district") or "").lower()
    if district:
        for n in news:
            if district in (n.get("districts") or "").lower():
                score += 20.0
    return _clamp(score)


def rise_rate_mph(level, ts, prev_level, prev_ts) -> float | None:
    """Metres per hour between the two most recent distinct readings."""
    if None in (level, prev_level) or not ts or not prev_ts:
        return None
    try:
        hours = (dtparse.parse(ts) - dtparse.parse(prev_ts)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None
    if hours <= 0 or hours > 12:          # stale gauge; rate is meaningless
        return None
    return (level - prev_level) / hours


def p_exceed_6h(level, danger, rise_rate, next_12h_rain) -> float:
    """Probability the gauge passes its danger mark within 6 hours.

    Linear extrapolation of the observed rise, nudged by forecast rain, then
    squashed through a logistic so the output degrades gracefully rather than
    snapping between 0 and 1. Scale of 0.35 m means "roughly a third of a metre
    of headroom is where confidence crosses 50%".
    """
    if level is None or not danger:
        return 0.0
    rain_push = 0.004 * (next_12h_rain or 0)      # ~4 cm of rise per 100 mm forecast
    projected = level + 6.0 * (rise_rate or 0.0) + rain_push
    return round(1.0 / (1.0 + math.exp(-(projected - danger) / 0.35)), 3)


def score_station(station, prev_level, prev_ts, rain, incidents, news) -> dict:
    """Return the full scored record for one gauge."""
    past_24h = (rain or {}).get("past_24h", 0.0)
    next_12h = (rain or {}).get("next_12h", 0.0)
    rate = rise_rate_mph(station["level"], station["ts"], prev_level, prev_ts)

    parts = {
        "level": round(level_component(station["level"], station["warning_level"], station["danger_level"]), 1),
        "rise": round(rise_component(rate), 1),
        "rain": round(rain_component(past_24h, next_12h), 1),
        "corroboration": round(corroboration_component(station, incidents, news), 1),
    }
    fsi = (
        settings.w_level * parts["level"]
        + settings.w_rise * parts["rise"]
        + settings.w_rain * parts["rain"]
        + settings.w_corroboration * parts["corroboration"]
    )
    fsi = round(_clamp(fsi), 1)

    return {
        "station_id": station["id"],
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fsi": fsi,
        "band": band_for(fsi),
        "p_exceed_6h": p_exceed_6h(station["level"], station["danger_level"], rate, next_12h),
        "rise_rate": round(rate, 4) if rate is not None else None,
        "components": json.dumps(parts),
    }
