"""BIPAD Portal -- the Government of Nepal's official disaster incident registry.

Django REST Framework, public, no key. We pull recent water-related incidents
and use them to corroborate gauge readings: a rising river with a reported
incident nearby is a materially stronger signal than the gauge alone.

Note on filtering: the incident endpoint returns `hazard` as a numeric ID, not a
title, so the hazard vocabulary is fetched once from /hazard/ and cached. An
earlier version filtered on a `hazardTitle` field that does not exist, which
silently let every fire and snakebite in the country through as flood evidence.
"""
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://bipadportal.gov.np/api/v1"

# Hazard titles that mean "water is where it should not be". Matched against the
# live vocabulary so a renumbering upstream cannot silently break the filter.
WET_HAZARD_TITLES = {
    "flood", "landslide", "heavy rainfall", "inundation", "soil erosion",
    "glacial lake outburst", "avalanche", "hailstorm",
}
# Glacial lake outburst is called out separately: it is the direct observational
# counterpart to hazards/outburst.py, so it gets flagged rather than merged.
OUTBURST_TITLES = {"glacial lake outburst", "avalanche", "landslide"}


class BipadIncidentSpider:
    name = "bipad"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.errors: list[str] = []

    async def _hazard_vocabulary(self) -> dict[int, str]:
        """id -> lowercase title. Fetched per cycle; the list is ~40 rows."""
        r = await self.client.get(f"{BASE}/hazard/", params={"limit": 200})
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        return {h["id"]: (h.get("title") or "").strip().lower() for h in rows if h.get("id")}

    async def run(self, days: int = 14) -> list[dict]:
        vocab = await self._hazard_vocabulary()
        wet_ids = {i for i, t in vocab.items() if t in WET_HAZARD_TITLES}
        outburst_ids = {i for i, t in vocab.items() if t in OUTBURST_TITLES}

        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        r = await self.client.get(f"{BASE}/incident/", params={
            "limit": 500, "ordering": "-incident_on", "incident_on__gt": since,
        })
        r.raise_for_status()

        out = []
        for it in r.json().get("results", []):
            hazard_id = it.get("hazard")
            if hazard_id not in wet_ids:
                continue
            pt = (it.get("point") or {}).get("coordinates") or [None, None]
            if pt[0] is None:
                continue
            out.append({
                "id": f"bipad-{it['id']}",
                "title": it.get("title") or "",
                "hazard": vocab.get(hazard_id, "unknown"),
                "lon": pt[0],
                "lat": pt[1],
                "occurred_on": it.get("incidentOn") or it.get("createdOn"),
                "source": "BIPAD Portal (MoHA)",
                "url": f"https://bipadportal.gov.np/incidents/{it['id']}/response",
                # Mass-movement hazards that can dam a river upstream.
                "outburst_relevant": hazard_id in outburst_ids,
            })
        return out
