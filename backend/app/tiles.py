"""Local OpenStreetMap tile cache, clipped to Nepal.

Two reasons this exists rather than pointing Leaflet straight at tile.osm.org:

  * an ops console that stops working when the internet does is not an ops
    console. Once the cache is warm, the map renders offline.
  * hammering OSM's volunteer tile servers from an app that redraws every 12
    minutes is exactly what their tile usage policy asks you not to do.

Tiles outside the Nepal bounding box are refused, so the cache cannot grow into
a world mirror.
"""
import asyncio
import logging
import math
import time
from pathlib import Path

import httpx

from .config import NEPAL_BBOX, settings

log = logging.getLogger("tiles")

# Carto's Positron/Dark Matter basemaps render OSM data and permit this use with
# attribution. Both are kept so the UI can switch with the theme.
STYLES = {
    "dark": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    "light": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
ATTRIBUTION = ('&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
               'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>')


def deg2num(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """WGS84 -> slippy-map tile indices (standard Web Mercator formula)."""
    lat_r = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def num2deg(x: int, y: int, zoom: int) -> tuple[float, float]:
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


# Degrees of surrounding context to keep. Without it Nepal renders as an island
# floating in blank space, which reads as a broken map rather than a deliberate
# clip. 1.5 degrees is roughly one tile of border at z7.
CONTEXT_BUFFER_DEG = 1.5


def in_nepal_tile(x: int, y: int, z: int, buffer_deg: float = CONTEXT_BUFFER_DEG) -> bool:
    """True when the tile's extent intersects the Nepal bbox plus its buffer."""
    lat_n, lon_w = num2deg(x, y, z)
    lat_s, lon_e = num2deg(x + 1, y + 1, z)
    return not (lon_e < NEPAL_BBOX["west"] - buffer_deg
                or lon_w > NEPAL_BBOX["east"] + buffer_deg
                or lat_n < NEPAL_BBOX["south"] - buffer_deg
                or lat_s > NEPAL_BBOX["north"] + buffer_deg)


def cache_path(style: str, z: int, x: int, y: int) -> Path:
    return settings.tile_cache / style / str(z) / str(x) / f"{y}.png"


def is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < settings.tile_cache_days


async def fetch_tile(client: httpx.AsyncClient, style: str, z: int, x: int, y: int) -> bytes | None:
    """Return a tile from disk, fetching and caching it on a miss."""
    path = cache_path(style, z, x, y)
    if is_fresh(path):
        return path.read_bytes()
    if not in_nepal_tile(x, y, z):
        return None

    url = STYLES[style].format(z=z, x=x, y=y)
    try:
        r = await client.get(url, headers={"User-Agent": settings.user_agent})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("tile miss %s/%s/%s/%s: %s", style, z, x, y, exc)
        return path.read_bytes() if path.exists() else None    # stale beats blank

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


async def prefetch(style: str = "dark", min_z: int | None = None, max_z: int | None = None) -> dict:
    """Warm the cache for all of Nepal. Run once; the map then works offline.

    Concurrency is capped at 4 and each request is spaced, which keeps this
    inside a courteous request rate for a free tile service.
    """
    min_z = min_z or settings.tile_min_zoom
    max_z = max_z or settings.tile_max_zoom

    # Include the context buffer, otherwise the border tiles the map actually
    # shows would be the only ones still needing the network.
    b = CONTEXT_BUFFER_DEG
    jobs = []
    for z in range(min_z, max_z + 1):
        x0, y0 = deg2num(NEPAL_BBOX["north"] + b, NEPAL_BBOX["west"] - b, z)
        x1, y1 = deg2num(NEPAL_BBOX["south"] - b, NEPAL_BBOX["east"] + b, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                if in_nepal_tile(x, y, z):
                    jobs.append((z, x, y))

    sem = asyncio.Semaphore(4)
    done = {"fetched": 0, "cached": 0, "failed": 0}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        async def one(z, x, y):
            async with sem:
                if is_fresh(cache_path(style, z, x, y)):
                    done["cached"] += 1
                    return
                data = await fetch_tile(client, style, z, x, y)
                done["fetched" if data else "failed"] += 1
                await asyncio.sleep(0.05)        # be a good citizen

        await asyncio.gather(*(one(z, x, y) for z, x, y in jobs))

    done["total_tiles"] = len(jobs)
    done["style"] = style
    done["zoom_range"] = [min_z, max_z]
    return done
