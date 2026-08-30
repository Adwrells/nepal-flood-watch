"""BIPAD resource registry: health facilities, and the other resource classes.

16,299 health facilities nationally, every one geocoded. This is the layer that
turns "the river is rising here" into "and the nearest hospital is 4 km that
way", which is the question an operator actually has next.

Refreshed daily, not every cycle. The records carry `lastModifiedDate` values
from 2022 -- this is a register, not a feed, and re-pulling 16k rows every 12
minutes would be pure waste and a discourtesy to a government server.
"""
import httpx

BASE = "https://bipadportal.gov.np/api/v1/resource/"
PAGE = 1000
# Safety cap: the API reports count as maxint, so paging must be self-limiting.
MAX_PAGES = 25

# BIPAD's own vocabulary. `health` is the one that matters here; the others are
# kept so the table can answer "what else is near this gauge" later.
TYPES = ("health", "education", "governance", "finance", "communication")


class ResourceSpider:
    name = "resources"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.errors: list[str] = []

    async def run(self, resource_type: str = "health") -> list[dict]:
        out: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            r = await self.client.get(BASE, params={
                "resource_type": resource_type, "limit": PAGE, "offset": offset,
            })
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("results", [])
            if not rows:
                break

            for it in rows:
                pt = (it.get("point") or {}).get("coordinates") or [None, None]
                if pt[0] is None:
                    continue                      # unmappable, and this table is spatial
                out.append({
                    "id": f"bipad-res-{it['id']}",
                    "kind": it.get("resourceType") or resource_type,
                    "title": (it.get("title") or "").strip(),
                    "title_ne": (it.get("titleNe") or "") or "",
                    "lon": pt[0],
                    "lat": pt[1],
                    "ward": it.get("ward"),
                    "updated": it.get("lastModifiedDate") or "",
                    "source": "BIPAD Portal (MoHA)",
                })

            if not payload.get("next"):
                break
            offset += PAGE
        return out
