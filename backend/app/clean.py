"""Cleaning and standardisation. Every scraped record passes through here.

The upstream feeds are inconsistent in ways that quietly corrupt a score if you
let them through:

  * DHM sends water levels as "", " ", "N/A" or a float-as-string
  * station names arrive lower-case and unpunctuated ("kokhajor khola at ...")
  * district spelling varies across sources (Kavre / Kavrepalanchok /
    Kabhrepalanchok all refer to one district; joining on the raw string
    silently drops the corroboration signal)
  * timestamps come as naive local, UTC-with-offset, or RSS RFC-822
  * telemetry glitches produce impossible stage jumps

Rule followed throughout: standardise aggressively, but NEVER invent. A value
that cannot be parsed becomes None and is excluded from scoring, rather than
being defaulted to zero -- a zero would read as "river is empty", which scores
as safe and is the most dangerous possible failure mode.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from dateutil import parser as dtparse

NPT = timezone(timedelta(hours=5, minutes=45))     # Nepal Standard Time, UTC+05:45

# Physically impossible stage change; anything beyond this is a telemetry fault.
MAX_STAGE_JUMP_M_PER_H = 3.0
# Nepal's real bounds. Anything outside is a bad coordinate, not a station.
NEPAL_BOUNDS = {"west": 79.9, "east": 88.3, "south": 26.3, "north": 30.6}

# Canonical district spellings, keyed by the variants actually seen in the wild.
DISTRICT_ALIASES = {
    "kavre": "Kavrepalanchok", "kabhrepalanchok": "Kavrepalanchok",
    "kavrepalanchowk": "Kavrepalanchok", "kabhre": "Kavrepalanchok",
    "sindhupalchowk": "Sindhupalchok", "sindhupalchuk": "Sindhupalchok",
    "makwanpur": "Makwanpur", "makawanpur": "Makwanpur",
    "chitawan": "Chitwan", "kathmandu metropolitan": "Kathmandu",
    "nawalparasi east": "Nawalparasi", "nawalparasi west": "Nawalparasi",
    "rukum east": "Rukum East", "rukum west": "Rukum West",
    "dhanusa": "Dhanusha", "mahottari": "Mahottari",
    "terhathum": "Terhathum", "tehrathum": "Terhathum",
    "solukhumbhu": "Solukhumbu", "okhaldunga": "Okhaldhunga",
}

# Words that stay lower-case inside a station name.
_MINOR = {"at", "of", "the", "near", "and"}


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------
def norm_float(value) -> float | None:
    """Parse a number out of DHM's mixed string encoding. Unparseable -> None.

    Explicitly NOT defaulting to 0.0: a zero stage reads as a safe, empty river
    and would score as NORMAL. None excludes the gauge from scoring instead,
    which is the correct behaviour for a gauge that is not reporting.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math_isfinite(value) else None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "-", "--", "null", "none"}:
        return None
    # Tolerate a trailing unit ("2.34 m") or a thousands separator.
    text = text.replace(",", "")
    m = re.match(r"^[-+]?\d*\.?\d+", text)
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    return v if math_isfinite(v) else None


def math_isfinite(v) -> bool:
    import math
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def norm_text(value) -> str:
    """Collapse whitespace and strip control/zero-width characters."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", text).strip()


def title_case_station(value) -> str:
    """'kokhajor khola at hariharpurgadi' -> 'Kokhajor Khola at Hariharpurgadi'.

    Preserves existing capitals so acronyms and correctly-cased names survive.
    """
    text = norm_text(value)
    if not text:
        return ""
    words = []
    for i, w in enumerate(text.split(" ")):
        if w.lower() in _MINOR and i > 0:
            words.append(w.lower())
        elif w.isupper() and len(w) > 1:
            words.append(w)                      # keep acronyms
        else:
            words.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(words)


def norm_district(value) -> str:
    """Map a district string onto its canonical spelling."""
    text = norm_text(value)
    if not text:
        return ""
    key = text.lower().replace("district", "").strip()
    if key in DISTRICT_ALIASES:
        return DISTRICT_ALIASES[key]
    return title_case_station(key)


def norm_timestamp(value) -> str | None:
    """Any input timestamp -> ISO 8601 in Nepal time. Unparseable -> None.

    Naive timestamps are assumed to be Nepal local, which is what DHM and BIPAD
    both publish. Getting this wrong shifts every rise-rate by 5h45m.
    """
    if not value:
        return None
    try:
        dt = dtparse.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NPT)
    return dt.astimezone(NPT).isoformat(timespec="seconds")


def in_nepal(lat, lon) -> bool:
    return (lat is not None and lon is not None
            and NEPAL_BOUNDS["south"] <= lat <= NEPAL_BOUNDS["north"]
            and NEPAL_BOUNDS["west"] <= lon <= NEPAL_BOUNDS["east"])


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def clean_station(raw: dict) -> dict | None:
    """Standardise one DHM gauge record. Returns None if it is unusable.

    Also drops the warning/danger marks when they are non-monotonic
    (danger <= warning), which happens in the source data and would otherwise
    make the level component divide by a negative interval.
    """
    lat, lon = norm_float(raw.get("lat")), norm_float(raw.get("lon"))
    if not in_nepal(lat, lon):
        return None
    if raw.get("id") is None:
        return None

    warning = norm_float(raw.get("warning_level"))
    danger = norm_float(raw.get("danger_level"))
    if warning is not None and danger is not None and danger <= warning:
        danger = None                            # inconsistent marks, trust neither order

    return {
        "id": int(raw["id"]),
        "name": title_case_station(raw.get("name")) or f"Station {raw['id']}",
        "basin": title_case_station(raw.get("basin")),
        "district": norm_district(raw.get("district")),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "series_id": raw.get("series_id"),
        "warning_level": warning,
        "danger_level": danger,
        "level": norm_float(raw.get("level")),
        "ts": norm_timestamp(raw.get("ts")),
        "status": norm_text(raw.get("status")).upper(),
        "steady": norm_text(raw.get("steady")).upper(),
    }


def reject_stage_outlier(level, ts, prev_level, prev_ts) -> bool:
    """True when a reading implies a physically impossible rate of change.

    A gauge cannot rise 3 m in an hour; that is a stuck sensor, a unit switch,
    or a datum reset. We reject the READING, not the station, so the next good
    packet recovers it automatically.
    """
    if None in (level, prev_level) or not ts or not prev_ts:
        return False
    try:
        hours = (dtparse.parse(ts) - dtparse.parse(prev_ts)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return False
    if hours <= 0:
        return False
    return abs(level - prev_level) / hours > MAX_STAGE_JUMP_M_PER_H


def clean_incident(raw: dict) -> dict | None:
    lat, lon = norm_float(raw.get("lat")), norm_float(raw.get("lon"))
    if not in_nepal(lat, lon):
        return None
    return {**raw,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "title": norm_text(raw.get("title")),
            "hazard": norm_text(raw.get("hazard")).lower(),
            "occurred_on": norm_timestamp(raw.get("occurred_on")) or ""}


def clean_news(raw: dict) -> dict | None:
    title = norm_text(raw.get("title"))
    if not title or not raw.get("url"):
        return None
    districts = [norm_district(d) for d in (raw.get("districts") or "").split(",") if d.strip()]
    return {**raw,
            "title": title,
            "districts": ",".join(sorted(set(districts))),
            "published": norm_timestamp(raw.get("published")) or ""}


def dedupe(records: list[dict], key: str = "id") -> list[dict]:
    """Keep the first occurrence of each key, preserving order."""
    seen, out = set(), []
    for r in records:
        k = r.get(key)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def clean_batch(raw_stations, raw_incidents, raw_news) -> dict:
    """Run the whole cleaning pass and report what was dropped and why.

    The report is surfaced at /api/health so data quality is visible rather than
    silent -- a source that starts returning garbage should be obvious.
    """
    stations = [s for s in (clean_station(r) for r in raw_stations) if s]
    incidents = [i for i in (clean_incident(r) for r in raw_incidents) if i]
    news = [n for n in (clean_news(r) for r in raw_news) if n]

    stations = dedupe(stations, "id")
    incidents = dedupe(incidents, "id")
    news = dedupe(news, "id")

    reporting = [s for s in stations if s["level"] is not None]
    return {
        "stations": stations,
        "incidents": incidents,
        "news": news,
        "quality": {
            "stations_in": len(raw_stations), "stations_kept": len(stations),
            "stations_reporting_level": len(reporting),
            "stations_with_danger_mark": sum(1 for s in stations if s["danger_level"]),
            "incidents_in": len(raw_incidents), "incidents_kept": len(incidents),
            "news_in": len(raw_news), "news_kept": len(news),
            "cleaned_at": datetime.now(NPT).isoformat(timespec="seconds"),
        },
    }
