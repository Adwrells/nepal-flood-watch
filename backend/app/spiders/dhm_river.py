"""DHM river watch -- the primary signal.

dhm.gov.np renders its Leaflet map from a `var coordinates = [...]` literal
embedded in the page. That array is richer than the public table endpoint: it
carries lat/lon, the live water level with its timestamp, and the warning and
danger marks for each gauge. We slice the literal out and json-parse it.
"""
import json
import re

import httpx

from .base import Spider

# Non-greedy up to the `];` that closes the literal.
_ARRAY = re.compile(r"var\s+coordinates\s*=\s*(\[.*?\])\s*;", re.S)


def _num(v) -> float | None:
    """DHM sends levels as strings, blanks, or ' '. Anything unparseable is None."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


class DhmRiverSpider(Spider):
    name = "dhm_river"
    start_urls = ["https://www.dhm.gov.np/hydrology/river-watch"]

    def parse(self, response: httpx.Response) -> list[dict]:
        m = _ARRAY.search(response.text)
        if not m:
            raise ValueError("dhm river-watch: `var coordinates` literal not found")

        out = []
        for s in json.loads(m.group(1)):
            wl = s.get("waterLevel") or {}
            if not isinstance(wl, dict):        # empty gauges send a bare " "
                wl = {}
            lat, lon = _num(s.get("latitude")), _num(s.get("longitude"))
            if lat is None or lon is None:
                continue                        # unmappable, drop it
            out.append(
                {
                    "id": s.get("id"),
                    "name": (s.get("name") or "").strip(),
                    "basin": s.get("basin") or "",
                    "district": s.get("district") or "",
                    "lat": lat,
                    "lon": lon,
                    "series_id": s.get("series_id"),
                    "warning_level": _num(s.get("warning_level")),
                    "danger_level": _num(s.get("danger_level")),
                    "level": _num(wl.get("value")),
                    "ts": wl.get("datetime"),
                    "status": (s.get("status") or "").strip(),
                    "steady": (s.get("steady") or "").strip(),
                }
            )
        return out
