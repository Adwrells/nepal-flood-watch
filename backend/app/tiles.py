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
import re
import time
from pathlib import Path

import httpx

from .config import NEPAL_BBOX, settings

log = logging.getLogger("tiles")

# GIBS dates reach a cache path and a log line, so the shape is enforced.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _style_label(style: str) -> str:
    """Our own constant for a validated style, not the caller's string.

    The membership guard above already rejects anything unknown, but the guard
    narrows the VALUE while the variable still holds the caller's object. Taint
    analysis is right to keep flagging it, and looking the label up in our own
    table means the log line is built from our literals either way.
    """
    return _STYLE_LABELS.get(style, "unknown")


def _fault(exc: Exception) -> str:
    """Describe a failed request without echoing anything the remote controls.

    Exception text embeds the request URL and often the server's response, so
    logging it lets a hostile upstream inject newlines and forge entries that
    look like ours. The exception CLASS plus the status code carries everything
    the log is actually for -- "was it a timeout, a 404, or a 500" -- and is
    derived from our own code rather than from their bytes.

    Sanitising the string instead would work, but a hand-rolled sanitiser is
    something every future reader (and every static analyser) has to take on
    trust. Not passing the value at all needs no trust.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" HTTP {int(status)}" if status else "")


def _content_kind(content_type) -> str:
    """Coarse, closed-vocabulary description of a response's declared type."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("text/html"):
        return "html"          # the shape of a "blocked" or error page
    if ct.startswith("application/json"):
        return "json"
    return "other" if ct else "absent"

# Basemap provider, chosen after two false starts worth recording:
#
#   CARTO Positron/Dark Matter  now burns an "API KEY REQUIRED" watermark into
#                               keyless requests. It still returns HTTP 200, so
#                               nothing in the code can detect the failure -- it
#                               only shows up on screen.
#   tile.openstreetmap.org      technically reachable, but proxying and bulk
#                               prefetching it is precisely what OSM's tile
#                               usage policy forbids. Their volunteer servers
#                               are not a CDN, and they will serve a "blocked"
#                               notice tile instead. Not a rate limit to route
#                               around; a rule not to break.
#
# Esri's canvas basemaps are keyless, permit caching with attribution, and give
# a genuine dark cartography rather than a CSS-inverted light one. They are also
# the same provider already serving the satellite layer, so this is one
# relationship to honour rather than three.
#
# Note the path order: Esri REST is /tile/{z}/{y}/{x}, NOT the {z}/{x}/{y} of a
# standard XYZ layer.
_ESRI = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
         "{service}/MapServer/tile/{z}/{y}/{x}")

STYLES = {
    "dark": _ESRI.replace("{service}", "Canvas/World_Dark_Gray_Base"),
    "light": _ESRI.replace("{service}", "Canvas/World_Light_Gray_Base"),
    "street": _ESRI.replace("{service}", "World_Street_Map"),
    "topo": _ESRI.replace("{service}", "World_Topo_Map"),
}

# Literal labels for logging, so a log line is never built from a caller string.
_STYLE_LABELS = {"dark": "dark", "light": "light", "street": "street", "topo": "topo"}

ATTRIBUTION = ('Basemap &copy; <a href="https://www.esri.com">Esri</a>, '
               'HERE, Garmin, &copy; OpenStreetMap contributors')

# --- satellite imagery -----------------------------------------------------
# Two very different things, and conflating them would be dangerous:
#
#   Esri World Imagery  sub-metre detail, but a MOSAIC assembled over months to
#                       years. Excellent for "what is on the ground here";
#                       useless for "is it flooded right now".
#   NASA GIBS           250 m MODIS / 375 m VIIRS, but a genuinely NEW image
#                       every day, roughly 3 h behind the satellite pass. Coarse,
#                       but it is the only free imagery that answers "today".
#
# Every response carries its freshness so the UI can label it honestly.
SATELLITE = {
    "esri": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "max_zoom": 19,
        "dated": False,
        "label": "High resolution (Esri)",
        "freshness": "Mosaic, typically months to years old. Detail, not currency.",
        "attribution": "Imagery &copy; Esri, Maxar, Earthstar Geographics",
    },
}

# GIBS WMTS REST is /{TileMatrix}/{TileRow}/{TileCol} -- that is z/y/x, NOT the
# z/x/y of a standard XYZ layer. Getting this backwards silently returns the
# wrong hemisphere.
GIBS_URL = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/{layer}/default/"
            "{date}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg")

GIBS_LAYERS = {
    "truecolor": {
        "id": "MODIS_Terra_CorrectedReflectance_TrueColor",
        "label": "Daily true colour (MODIS Terra)",
        "freshness": "One image per day, about 3 h behind the satellite pass. 250 m.",
        "note": "What the eye would see. Cloud often hides the ground in monsoon.",
    },
    "flood": {
        "id": "MODIS_Terra_CorrectedReflectance_Bands721",
        "label": "Flood-enhanced (MODIS bands 7-2-1)",
        "freshness": "One image per day, about 3 h behind the satellite pass. 250 m.",
        "note": ("Shortwave-infrared composite: standing water reads near-black, "
                 "vegetation bright green, cloud white. This is the layer to use "
                 "for spotting inundation, because water and cloud stop looking "
                 "alike."),
    },
    "viirs": {
        "id": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
        "label": "Daily true colour (VIIRS)",
        "freshness": "One image per day, about 3 h behind the satellite pass. 375 m.",
        "note": "Second daily look; useful when the MODIS pass was clouded out.",
    },
}
GIBS_MAX_ZOOM = 9          # GoogleMapsCompatible_Level9 has no tiles beyond this
GIBS_ATTRIBUTION = ("Imagery courtesy NASA EOSDIS GIBS / Worldview")


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
    """Return a tile from disk, fetching and caching it on a miss.

    `style` is validated here, not only in the route. It reaches a filesystem
    path and a log line, and this module should be safe to call from anywhere
    rather than safe only because its current caller happens to check first.
    """
    if style not in STYLES:
        return None
    z, x, y = int(z), int(x), int(y)
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
        log.debug("tile miss %s/%s/%s/%s: %s", _style_label(style), z, x, y, _fault(exc))
        return path.read_bytes() if path.exists() else None    # stale beats blank

    # A provider that answers 200 with an HTML error page (or a "blocked"
    # notice) would otherwise be cached and served forever as a valid tile.
    if not r.headers.get("content-type", "").startswith("image/"):
        log.warning("tile provider returned %s, not an image, for %s/%s/%s/%s",
                    _content_kind(r.headers.get("content-type")),
                    _style_label(style), z, x, y)
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


async def prefetch(style: str = "dark", min_z: int | None = None, max_z: int | None = None) -> dict:
    """Warm the cache for all of Nepal. Run once; the map then works offline.

    Concurrency is capped at 4 and each request is spaced, which keeps this
    inside a courteous request rate. Do NOT repoint this at OSM's own tile
    servers: bulk prefetching them is explicitly against their usage policy.
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


# ---------------------------------------------------------------------------
# Satellite imagery
# ---------------------------------------------------------------------------
def satellite_path(source: str, z: int, x: int, y: int, date: str = "static") -> Path:
    return settings.tile_cache / "sat" / source / date / str(z) / str(x) / f"{y}.jpg"


async def fetch_satellite(client: httpx.AsyncClient, z: int, x: int, y: int) -> bytes | None:
    """Esri World Imagery tile, cached. Note the z/y/x order in their REST path."""
    z, x, y = int(z), int(x), int(y)
    if not in_nepal_tile(x, y, z):
        return None
    path = satellite_path("esri", z, x, y)
    if is_fresh(path):
        return path.read_bytes()

    url = SATELLITE["esri"]["url"].format(z=z, y=y, x=x)
    try:
        r = await client.get(url, headers={"User-Agent": settings.user_agent})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("esri tile miss %s/%s/%s: %s", z, x, y, _fault(exc))
        return path.read_bytes() if path.exists() else None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


async def fetch_gibs(client: httpx.AsyncClient, layer: str, date: str,
                     z: int, x: int, y: int) -> bytes | None:
    """NASA GIBS daily imagery tile, cached per date.

    Dated tiles never expire: the image for 8 July 2025 will not change, so a
    cached copy stays valid forever. That makes before/after comparison free
    after the first look.
    """
    z, x, y = int(z), int(x), int(y)
    if layer not in GIBS_LAYERS or z > GIBS_MAX_ZOOM:
        return None
    # Dates land in a cache path and a log line; accept only the exact shape.
    if not _DATE_RE.fullmatch(date):
        return None
    if not in_nepal_tile(x, y, z):
        return None

    path = satellite_path(layer, z, x, y, date)
    if path.exists():                       # dated imagery is immutable
        return path.read_bytes()

    url = GIBS_URL.format(layer=GIBS_LAYERS[layer]["id"], date=date, z=z, y=y, x=x)
    try:
        r = await client.get(url, headers={"User-Agent": settings.user_agent})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        # GIBS_LAYERS is ours, so its label is a literal; the date is already
        # shape-checked and is not echoed. Neither caller string reaches the log.
        log.debug("gibs miss %s %s/%s/%s: %s",
                  GIBS_LAYERS[layer]["label"], z, x, y, _fault(exc))
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def imagery_options() -> dict:
    """What the Explore tab can show, and how current each option actually is."""
    from datetime import date, timedelta
    today = date.today()
    return {
        "basemaps": [
            {"id": "dark", "label": "Dark map", "kind": "map"},
            {"id": "light", "label": "Light map", "kind": "map"},
            {"id": "esri", "label": SATELLITE["esri"]["label"], "kind": "satellite",
             "freshness": SATELLITE["esri"]["freshness"],
             "max_zoom": SATELLITE["esri"]["max_zoom"],
             "attribution": SATELLITE["esri"]["attribution"]},
        ],
        "daily": [
            {"id": k, **{kk: vv for kk, vv in v.items() if kk != "id"},
             "max_zoom": GIBS_MAX_ZOOM, "attribution": GIBS_ATTRIBUTION}
            for k, v in GIBS_LAYERS.items()
        ],
        # GIBS publishes with a lag, so "today" is often not there yet.
        "dates": [(today - timedelta(days=d)).isoformat() for d in range(1, 15)],
        "worldview": "https://worldview.earthdata.nasa.gov/",
    }
