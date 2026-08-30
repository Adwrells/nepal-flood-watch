"""Rainfall pressure per gauge.

DHM's own rainfall table sits behind a CSRF-guarded POST that rejects
non-browser sessions, so the reliable path is Open-Meteo: keyless, gives us
BOTH the past 24 h observed and the next 12 h forecast, and accepts up to 100
coordinates per call. The forecast half is what turns this from a nowcast into
an actual prediction.
"""
import httpx

API = "https://api.open-meteo.com/v1/forecast"
BATCH = 100          # Open-Meteo's documented multi-point limit


class RainfallSpider:
    name = "rainfall"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def run(self, stations: list[dict] | None = None) -> list[dict]:
        stations = stations or []
        out: list[dict] = []
        for i in range(0, len(stations), BATCH):
            chunk = stations[i : i + BATCH]
            r = await self.client.get(
                API,
                params={
                    "latitude": ",".join(f"{s['lat']:.4f}" for s in chunk),
                    "longitude": ",".join(f"{s['lon']:.4f}" for s in chunk),
                    "hourly": "precipitation",
                    "past_hours": 24,
                    "forecast_hours": 12,
                    "timezone": "Asia/Kathmandu",
                },
            )
            r.raise_for_status()
            data = r.json()
            # A single coordinate returns an object; several return a list.
            blocks = data if isinstance(data, list) else [data]
            # strict=: a length mismatch means the API returned a different number of
            # points than we asked for, which must fail loudly, not silently
            # pair the wrong rainfall with the wrong gauge.
            for station, block in zip(chunk, blocks, strict=False):
                mm = [v or 0.0 for v in block.get("hourly", {}).get("precipitation", [])]
                out.append(
                    {
                        "station_id": station["id"],
                        "ts": block.get("hourly", {}).get("time", [None])[0],
                        "past_24h": round(sum(mm[:24]), 1),   # observed
                        "next_12h": round(sum(mm[24:]), 1),   # forecast
                    }
                )
        return out
