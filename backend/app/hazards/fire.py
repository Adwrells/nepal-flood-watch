"""Active fire detections over Nepal from NASA FIRMS (VIIRS + MODIS).

FIRMS serves satellite thermal anomalies within ~3 hours of overpass. It needs
a free MAP_KEY (register at firms.modaps.eosdis.nasa.gov/api/area/ and put it in
.env as FIRMS_MAP_KEY). Without a key this spider reports "not configured"
rather than failing the cycle -- every other hazard keeps working.

Nepal's fire season is Feb-May, largely in the Chure and mid-hills. Fire matters
to a FLOOD system because a burned catchment loses its infiltration capacity:
post-fire debris flows are triggered by rainfall that the same slope would have
absorbed a year earlier. Detections are therefore retained for 180 days and
used to raise the rainfall sensitivity of gauges downstream of a burn scar.
"""
import csv
import io

import httpx

from ..config import NEPAL_BBOX, settings

API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
# VIIRS at 375 m resolves far smaller fires than MODIS at 1 km.
SENSOR = "VIIRS_SNPP_NRT"

# Confidence strings VIIRS uses; MODIS uses a 0-100 integer instead.
CONFIDENCE_RANK = {"l": "low", "n": "nominal", "h": "high"}


class FireSpider:
    name = "fire"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def run(self, days: int = 2) -> list[dict]:
        if not settings.firms_map_key:
            raise RuntimeError("FIRMS_MAP_KEY not set -- register free at "
                               "firms.modaps.eosdis.nasa.gov/api/area/")

        bbox = (f"{NEPAL_BBOX['west']},{NEPAL_BBOX['south']},"
                f"{NEPAL_BBOX['east']},{NEPAL_BBOX['north']}")
        url = f"{API}/{settings.firms_map_key}/{SENSOR}/{bbox}/{days}"
        r = await self.client.get(url)
        r.raise_for_status()
        if r.text.strip().startswith("Invalid"):
            raise RuntimeError("FIRMS rejected the MAP_KEY")

        out = []
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            except (KeyError, ValueError):
                continue
            conf = row.get("confidence", "")
            out.append({
                "id": f"firms-{row.get('acq_date')}-{row.get('acq_time')}-{lat:.4f}-{lon:.4f}",
                "kind": "fire",
                "lat": lat, "lon": lon,
                # Fire radiative power in MW -- the best available proxy for intensity.
                "frp_mw": _f(row.get("frp")),
                "brightness_k": _f(row.get("bright_ti4") or row.get("brightness")),
                "confidence": CONFIDENCE_RANK.get(conf.lower(), conf),
                "occurred_on": f"{row.get('acq_date')}T{_hhmm(row.get('acq_time'))}Z",
                "daynight": row.get("daynight", ""),
                "source": f"NASA FIRMS {SENSOR}",
                "url": "https://firms.modaps.eosdis.nasa.gov/map/",
            })
        return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hhmm(t):
    """FIRMS gives acquisition time as HHMM without a separator (e.g. '742')."""
    t = (t or "").zfill(4)
    return f"{t[:2]}:{t[2:]}"
