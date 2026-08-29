"""Earthquakes over Nepal, from the USGS FDSN event service.

Keyless and public. Relevant here for two reasons beyond shaking damage:

  * a M5+ event in a steep monsoon-saturated catchment is the single most
    common trigger for the landslides that dam rivers, so a quake raises the
    outburst watch level for gauges in its radius;
  * co-seismic landslide risk stays elevated for weeks afterwards.
"""
import httpx

from ..config import NEPAL_BBOX

API = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# A shallow M5 in the high Himalaya mobilises slopes; a deep M5 largely does not.
LANDSLIDE_TRIGGER_MAG = 4.5
LANDSLIDE_TRIGGER_DEPTH_KM = 40.0


class QuakeSpider:
    name = "quake"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def run(self, days: int = 30, min_mag: float = 3.0) -> list[dict]:
        r = await self.client.get(API, params={
            "format": "geojson",
            "minlatitude": NEPAL_BBOX["south"], "maxlatitude": NEPAL_BBOX["north"],
            "minlongitude": NEPAL_BBOX["west"], "maxlongitude": NEPAL_BBOX["east"],
            "minmagnitude": min_mag,
            "starttime": _days_ago(days),
            "orderby": "time", "limit": 500,
        })
        r.raise_for_status()

        out = []
        for f in r.json().get("features", []):
            p, c = f["properties"], f["geometry"]["coordinates"]
            lon, lat, depth = c[0], c[1], c[2]
            mag = p.get("mag") or 0
            out.append({
                "id": f"usgs-{f['id']}",
                "kind": "earthquake",
                "title": p.get("place") or "",
                "magnitude": mag,
                "depth_km": depth,
                "lat": lat, "lon": lon,
                "occurred_on": _iso(p.get("time")),
                "url": p.get("url") or "",
                "source": "USGS",
                # Flag events capable of seeding a landslide dam upstream.
                "landslide_trigger": bool(
                    mag >= LANDSLIDE_TRIGGER_MAG and depth <= LANDSLIDE_TRIGGER_DEPTH_KM
                ),
            })
        return out


def _days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _iso(ms) -> str:
    from datetime import datetime, timezone
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
