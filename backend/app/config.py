"""Central settings. Override any field with an env var or a .env file."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

# Nepal only. Every spatial query is clipped to this box so no other country's
# data enters the system.
NEPAL_BBOX = {"west": 79.9, "east": 88.3, "south": 26.3, "north": 30.6}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # --- scheduling -------------------------------------------------------
    cycle_minutes: int = 12          # 10-15 min refresh window
    request_timeout: float = 45.0
    user_agent: str = "NepalFloodWatch/1.0 (+research; contact: ops@example.org)"

    # --- storage ----------------------------------------------------------
    db_path: Path = ROOT / "data" / "flood.db"
    excel_path: Path = ROOT / "data" / "nepal_flood_watch.xlsx"
    json_path: Path = ROOT / "data" / "snapshot.json"
    tile_cache: Path = ROOT / "data" / "tiles"

    # --- scoring weights (must sum to 1.0) --------------------------------
    w_level: float = 0.50            # how close the gauge is to danger mark
    w_rise: float = 0.25             # how fast it is climbing (lead indicator)
    w_rain: float = 0.18             # observed + forecast rainfall pressure
    w_corroboration: float = 0.07    # BIPAD incidents / news near the station

    # --- optional sources -------------------------------------------------
    # Free key: https://firms.modaps.eosdis.nasa.gov/api/area/
    firms_map_key: str = ""
    # Facebook is Graph-API-only. Scraping facebook.com HTML breaks their ToS,
    # so this stays a token-gated integration for Pages you have rights to.
    facebook_page_ids: str = ""
    facebook_token: str = ""

    # --- map tiles --------------------------------------------------------
    # Tiles are cached locally on first request. Nepal at z5-12 is a few
    # hundred MB; prefetch once and the console then works offline.
    tile_min_zoom: int = 5
    tile_max_zoom: int = 12
    tile_cache_days: int = 30


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.tile_cache.mkdir(parents=True, exist_ok=True)
