"""Periodic reachability tracking for the official sources this console links to.

The Updates tab has always linked out to NDRRMA, DHM, Nepal Police, and a
handful of international and humanitarian sources rather than scraping them --
see the frontend's own note on why (Facebook Graph-API-only, DHM's rainfall
table CSRF-guarded). NDRRMA's own "Daily Bulletin" page and DHM's notice pages
are client-rendered, not a feed, so there is nothing structured to scrape there
either.

What IS honest and useful without pretending otherwise: confirming, on a slow
background cadence, that each linked source is actually up right now, and
recording when it was last checked. A dead link in a disaster console is worse
than a slow one, and this is the cheapest way to know before a user does.

This deliberately is NOT another spider isolated per-cycle like the 12-minute
pipeline -- checking six URLs is not worth doing every 12 minutes, so it runs
on its own slower interval (see main.py's second scheduler job).
"""
import logging
from dataclasses import asdict, dataclass
from datetime import UTC

import httpx

log = logging.getLogger("official_sources")

CHECK_INTERVAL_MINUTES = 45


@dataclass
class Source:
    group: str
    name: str
    detail: str
    url: str


# Mirrors the frontend's OFFICIAL_SOURCES list (frontend/app.js) so the two
# stay in sync; if you add a source here, add it there too, and vice versa.
SOURCES = [
    Source("Government", "NDRRMA", "National Disaster Risk Reduction & Management Authority",
           "https://bipadportal.gov.np/"),
    Source("Government", "DHM", "Hydrology & Meteorology — flood bulletins",
           "https://www.dhm.gov.np/"),
    Source("Government", "Nepal Police", "Official updates",
           "https://www.nepalpolice.gov.np/"),
    Source("Government", "National Statistics Office", "Census and national statistics",
           "https://nsonepal.gov.np/"),
    Source("Government", "DNPWC", "National Parks & Wildlife Conservation",
           "https://dnpwc.gov.np/en/"),
    Source("International", "WHO Nepal", "World Health Organization — country office",
           "https://www.who.int/nepal"),
    Source("International", "UN OCHA Nepal", "Humanitarian coordination and situation reports",
           "https://www.unocha.org/nepal"),
    Source("International", "IFRC / Nepal Red Cross", "Relief operations and appeals",
           "https://www.ifrc.org/emergencies"),
    Source("International", "UNICEF Nepal", "Child and family emergency response",
           "https://www.unicef.org/nepal/"),
    Source("International", "ReliefWeb — Nepal", "Aggregated situation reports and assessments",
           "https://reliefweb.int/country/npl"),
]

# {url: {"reachable": bool, "status_code": int|None, "checked_at": iso str, "error": str|None}}
STATUS: dict[str, dict] = {}
LAST_CHECK_STARTED: str | None = None


async def check_all() -> None:
    """HEAD each source once; GET as a fallback for sites that reject HEAD.

    Runs in the background on CHECK_INTERVAL_MINUTES, never inside the flood
    pipeline's own cycle -- a slow or dead government website must not delay
    a gauge score reaching the map.
    """
    global LAST_CHECK_STARTED
    from datetime import datetime

    LAST_CHECK_STARTED = datetime.now(UTC).isoformat()
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for src in SOURCES:
            checked_at = datetime.now(UTC).isoformat()
            try:
                r = await client.head(src.url)
                if r.status_code >= 400:
                    r = await client.get(src.url)     # some sites reject HEAD outright
                STATUS[src.url] = {
                    "reachable": r.status_code < 400,
                    "status_code": r.status_code,
                    "checked_at": checked_at,
                    "error": None,
                }
            except Exception as exc:                  # noqa: BLE001 - isolate per source
                STATUS[src.url] = {
                    "reachable": False,
                    "status_code": None,
                    "checked_at": checked_at,
                    "error": str(exc)[:200],
                }
                log.warning("official source unreachable: %s (%s)", src.name, exc)


def snapshot() -> dict:
    """Current source list plus whatever the last background check found.

    Sources not yet checked (first ~45 min after startup) report
    reachable=None rather than a guessed True/False.
    """
    out = []
    for src in SOURCES:
        row = {**asdict(src)}
        status = STATUS.get(src.url)
        row["status"] = status or {"reachable": None, "status_code": None,
                                    "checked_at": None, "error": None}
        out.append(row)
    return {
        "sources": out,
        "check_interval_minutes": CHECK_INTERVAL_MINUTES,
        "last_check_started": LAST_CHECK_STARTED,
    }
