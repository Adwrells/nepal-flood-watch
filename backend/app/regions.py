"""Region registry. Nepal is the only active region; the shape allows more.

Everything country-specific lives here rather than being scattered through the
spiders, so adding Bhutan or Uttarakhand later means adding one entry and its
source adapters -- not rewriting the pipeline. Sources are named, not imported,
so a region can declare a source that does not exist yet without breaking.
"""
from dataclasses import asdict, dataclass, field


@dataclass
class Region:
    code: str
    name: str
    bbox: dict                      # west/east/south/north
    center: list                    # [lat, lon] for the initial map view
    zoom: int
    timezone: str
    sources: list                   # spider names active for this region
    enabled: bool = True
    note: str = ""
    # District/province centroids, used to place a headline on the map when the
    # only geography a news item carries is a district name.
    centroids: dict = field(default_factory=dict)


# Approximate district centroids for Nepal's most flood-exposed districts.
# Used ONLY as a fallback: pipeline.py derives real centroids from the DHM gauge
# coordinates wherever a district actually has gauges, which is self-maintaining
# and more accurate than any table checked in here.
NEPAL_DISTRICT_FALLBACK = {
    "Kailali": [28.68, 80.93], "Kanchanpur": [28.93, 80.30], "Bardiya": [28.30, 81.43],
    "Banke": [28.05, 81.62], "Dang": [28.00, 82.30], "Kapilvastu": [27.55, 83.05],
    "Rupandehi": [27.60, 83.45], "Nawalparasi": [27.60, 83.90], "Chitwan": [27.58, 84.45],
    "Parsa": [27.898, 84.88], "Bara": [27.03, 85.02], "Rautahat": [26.98, 85.28],
    "Sarlahi": [26.98, 85.55], "Mahottari": [26.90, 85.80], "Dhanusha": [26.82, 86.03],
    "Siraha": [26.65, 86.21], "Saptari": [26.60, 86.75], "Sunsari": [26.62, 87.16],
    "Morang": [26.66, 87.45], "Jhapa": [26.55, 87.90], "Udayapur": [26.84, 86.65],
    "Sindhupalchok": [27.95, 85.70], "Rasuwa": [28.12, 85.30], "Nuwakot": [27.92, 85.16],
    "Dhading": [27.87, 84.90], "Makwanpur": [27.42, 85.03], "Kavrepalanchok": [27.63, 85.54],
    "Kathmandu": [27.71, 85.32], "Lalitpur": [27.66, 85.32], "Bhaktapur": [27.67, 85.43],
    "Sindhuli": [27.20, 85.90], "Ramechhap": [27.42, 86.08], "Dolakha": [27.78, 86.17],
    "Solukhumbu": [27.70, 86.66], "Khotang": [27.20, 86.78], "Bhojpur": [27.17, 87.05],
    "Dhankuta": [26.98, 87.34], "Ilam": [26.91, 87.93], "Taplejung": [27.35, 87.67],
    "Panchthar": [27.12, 87.83], "Sankhuwasabha": [27.60, 87.28], "Terhathum": [27.13, 87.55],
    "Okhaldhunga": [27.32, 86.50], "Kaski": [28.21, 83.99], "Syangja": [28.09, 83.87],
    "Tanahun": [27.92, 84.25], "Gorkha": [28.00, 84.63], "Lamjung": [28.23, 84.38],
    "Manang": [28.67, 84.02], "Mustang": [28.85, 83.83], "Myagdi": [28.60, 83.57],
    "Baglung": [28.27, 83.59], "Parbat": [28.23, 83.71], "Palpa": [27.87, 83.55],
    "Gulmi": [28.07, 83.25], "Arghakhanchi": [27.95, 83.10], "Pyuthan": [28.10, 82.87],
    "Rolpa": [28.29, 82.63], "Salyan": [28.38, 82.17], "Surkhet": [28.60, 81.63],
    "Dailekh": [28.85, 81.72], "Jajarkot": [28.70, 82.19], "Rukum East": [28.63, 82.75],
    "Rukum West": [28.63, 82.35], "Humla": [29.97, 81.83], "Mugu": [29.53, 82.20],
    "Jumla": [29.28, 82.18], "Kalikot": [29.15, 81.62], "Dolpa": [29.03, 82.90],
    "Achham": [29.05, 81.30], "Bajura": [29.53, 81.52], "Bajhang": [29.54, 81.20],
    "Doti": [29.27, 80.93], "Dadeldhura": [29.30, 80.58], "Baitadi": [29.53, 80.48],
    "Darchula": [29.85, 80.55],
}

REGIONS = {
    "NP": Region(
        code="NP",
        name="Nepal",
        bbox={"west": 79.9, "east": 88.3, "south": 26.3, "north": 30.6},
        center=[28.2, 84.0],
        zoom=7,
        timezone="Asia/Kathmandu",
        sources=["dhm_river", "rainfall", "bipad", "news", "quake", "fire"],
        enabled=True,
        note="Live. DHM gauges, BIPAD incidents, Open-Meteo, USGS, NASA FIRMS.",
        centroids=NEPAL_DISTRICT_FALLBACK,
    ),
    # Placeholders. Each needs its own gauge adapter before it can be enabled;
    # rainfall (Open-Meteo), quakes (USGS) and fires (FIRMS) are already global.
    "BT": Region(
        code="BT", name="Bhutan",
        bbox={"west": 88.7, "east": 92.2, "south": 26.7, "north": 28.4},
        center=[27.5, 90.4], zoom=8, timezone="Asia/Thimphu",
        sources=["rainfall", "quake", "fire"], enabled=False,
        note="Needs a national hydrology gauge adapter before it can be enabled.",
    ),
    "IN-UT": Region(
        code="IN-UT", name="Uttarakhand, India",
        bbox={"west": 77.5, "east": 81.1, "south": 28.7, "north": 31.5},
        center=[30.1, 79.3], zoom=8, timezone="Asia/Kolkata",
        sources=["rainfall", "quake", "fire"], enabled=False,
        note="Needs a CWC gauge adapter before it can be enabled.",
    ),
}

DEFAULT_REGION = "NP"


def get(code: str | None = None) -> Region:
    """Resolve a region code, falling back to Nepal."""
    return REGIONS.get((code or DEFAULT_REGION).upper(), REGIONS[DEFAULT_REGION])


def listing() -> list[dict]:
    """Region metadata for the UI selector."""
    return [
        {**asdict(r), "centroids": None}      # centroids are large; omit from the list
        for r in REGIONS.values()
    ]
