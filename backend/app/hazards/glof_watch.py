"""Nepal's known potentially dangerous glacial lakes (PDGLs), ranked.

What this module IS: a citation-backed reference list of the glacial lakes
Nepal's own monitoring agencies already treat as highest priority, cross-
referenced against whatever the live gauge network is seeing right now in the
same basin/district.

What this module IS NOT: a breach-probability or time-to-failure predictor.
Unlike the river gauges (backtested against a naive baseline before any
forecast is trusted, see models/__init__.py) there is no labelled GLOF outcome
history to validate a predictive model against. Pretending otherwise would be
exactly the kind of invented precision this codebase avoids everywhere else --
see outburst.py's own rule: "every constant is sourced ... none of it is
invented." So `live_corroboration` here answers a narrower, honest question:
"is the downstream gauge network currently showing the same falling-river-
while-raining signature that flagged Bhote Koshi in July 2025, for a station
plausibly fed by this lake's basin?" Yes/no plus its normal confidence label
-- nothing more.

Sources
-------
Rank and the six named priority lakes: ICIMOD/UNDP's regional glacial lake
inventory (2026), which grouped 47 potentially dangerous glacial lakes across
Nepal, the Tibet Autonomous Region and India into Rank I (highest breach risk,
31 lakes), Rank II and Rank III (growing, needs monitoring, 12 + 4 lakes).
21 of the 47 sit in Nepal; 42 of the 47 drain into the Koshi basin, making it
the most exposed corridor. DHM and UNDP separately prioritise four of these
(Thulagi, Lower Barun, Lumding Tsho, Hongu 2) for active risk-reduction work,
alongside the two already famous for past lowering operations (Tsho Rolpa,
Imja Tsho).

    https://www.icimod.org/new-glacial-lake-inventory-report-released-47-potentially-dangerous-glacial-lakes-ranked/
    https://www.undp.org/nepal/press-releases/report-icimod-and-undp-identifies-potentially-dangerous-glacial-lakes-koshi-gandaki-and-karnali-river-basins

Coordinates are the lakes' own published locations where a source gives one
(Tsho Rolpa, Imja Tsho, Thulagi, Lower Barun); the remaining two are pinned at
their reported district/valley and flagged `approximate` in exactly the sense
the frontend already uses for a news headline pinned to a district centroid --
useful for "which part of Nepal", not a surveyed lake outline.
"""
from dataclasses import asdict, dataclass, field


@dataclass
class PriorityLake:
    name: str
    rank: str                # "I", "II", "III" -- ICIMOD/UNDP 2026 inventory
    basin: str                # this app's basin vocabulary (matches stations.basin)
    headwater: str            # matches outburst.HEADWATER_NAMES substrings
    district: str
    lat: float
    lon: float
    approximate: bool         # True where no published lake-level coordinate was found
    area_note: str
    source: str
    source_url: str


# Six lakes named specifically by ICIMOD/UNDP/DHM reporting as the highest-
# priority watch list, all Rank I. The other 15 Nepal PDGLs from the same
# inventory are not individually named in public reporting as of this
# writing, so they are represented in aggregate (see BASIN_TOTALS) rather
# than invented.
PRIORITY_LAKES = [
    PriorityLake(
        name="Tsho Rolpa", rank="I", basin="koshi", headwater="tama koshi",
        district="Dolakha", lat=27.867, lon=86.467, approximate=False,
        area_note="Nepal's largest glacial lake; partially lowered by 3 m in 2000 "
                   "after a 1990s hazard assessment, but continues to expand.",
        source="ICIMOD/UNDP 2026 PDGL inventory; DHM early-warning system installed 1998",
        source_url="https://www.icimod.org/new-glacial-lake-inventory-report-released-47-potentially-dangerous-glacial-lakes-ranked/",
    ),
    PriorityLake(
        name="Imja Tsho", rank="I", basin="koshi", headwater="dudh koshi",
        district="Solukhumbu", lat=27.898, lon=86.928, approximate=False,
        area_note="One of the Himalaya's fastest-growing lakes; lowered by ~3.4 m "
                   "in 2016 (UNDP/GEF), with sirens and gauges installed downstream.",
        source="ICIMOD/UNDP 2026 PDGL inventory",
        source_url="https://www.icimod.org/new-glacial-lake-inventory-report-released-47-potentially-dangerous-glacial-lakes-ranked/",
    ),
    PriorityLake(
        name="Thulagi (Dona)", rank="I", basin="narayani", headwater="marsyangdi",
        district="Manang / Lamjung", lat=28.5208, lon=84.5403, approximate=False,
        area_note="Formed ~1970s; area grew 0.76 -> 0.94 sq km between 1995 and 2009.",
        source="DHM/UNDP priority list; ICIMOD/UNDP 2026 PDGL inventory",
        source_url="https://www.undp.org/nepal/press-releases/report-icimod-and-undp-identifies-potentially-dangerous-glacial-lakes-koshi-gandaki-and-karnali-river-basins",
    ),
    PriorityLake(
        name="Lower Barun (Tallopokhari)", rank="I", basin="koshi", headwater="arun",
        district="Sankhuwasabha", lat=27.7975, lon=87.0906, approximate=False,
        area_note="Nearly tripled in area since 1989 (0.64 -> 2.2 sq km by 2019); "
                   "growth accelerated sharply after 2000.",
        source="DHM/UNDP priority list; ICIMOD/UNDP 2026 PDGL inventory",
        source_url="https://www.undp.org/nepal/press-releases/report-icimod-and-undp-identifies-potentially-dangerous-glacial-lakes-koshi-gandaki-and-karnali-river-basins",
    ),
    PriorityLake(
        name="Lumding Tsho", rank="I", basin="koshi", headwater="dudh koshi",
        district="Solukhumbu", lat=27.85, lon=86.85, approximate=True,
        area_note="DHM/UNDP priority lake for risk-reduction work; no published "
                   "lake-level coordinate found -- pinned to its reported valley.",
        source="DHM/UNDP priority list; ICIMOD/UNDP 2026 PDGL inventory",
        source_url="https://www.undp.org/nepal/press-releases/report-icimod-and-undp-identifies-potentially-dangerous-glacial-lakes-koshi-gandaki-and-karnali-river-basins",
    ),
    PriorityLake(
        name="Hongu 2 (Chamlang South)", rank="I", basin="koshi", headwater="dudh koshi",
        district="Solukhumbu", lat=27.77, lon=86.95, approximate=True,
        area_note="DHM/UNDP priority lake for risk-reduction work; no published "
                   "lake-level coordinate found -- pinned to its reported valley.",
        source="DHM/UNDP priority list; ICIMOD/UNDP 2026 PDGL inventory",
        source_url="https://www.undp.org/nepal/press-releases/report-icimod-and-undp-identifies-potentially-dangerous-glacial-lakes-koshi-gandaki-and-karnali-river-basins",
    ),
]

# Aggregate counts from the same 2026 inventory, for context around the six
# named lakes above -- not a claim that these are the only other lakes.
BASIN_TOTALS = {
    "note": "47 potentially dangerous glacial lakes across Nepal, the Tibet "
            "Autonomous Region of China, and India: 31 Rank I, 12 Rank II, "
            "4 Rank III. 21 of the 47 are in Nepal.",
    "by_basin": {"koshi": 42, "gandaki": 3, "karnali": 2},
    "source": "ICIMOD/UNDP 2026 PDGL inventory",
    "source_url": "https://www.icimod.org/new-glacial-lake-inventory-report-released-47-potentially-dangerous-glacial-lakes-ranked/",
}


@dataclass
class LakeStatus:
    lake: dict
    live_corroboration: bool
    confidence: str                    # matches ImpoundmentSignal.confidence
    nearby_stations: list = field(default_factory=list)
    note: str = ""


def _matches_lake(station: dict, lake: PriorityLake) -> bool:
    """A live gauge plausibly drains the same headwater as this lake.

    Same two-part test outburst.is_transboundary() uses (basin + headwater
    name in the station name), plus a district fallback for the two lakes
    without a distinct headwater station name of their own.
    """
    basin = (station.get("basin") or "").strip().lower()
    name = (station.get("name") or "").strip().lower()
    district = (station.get("district") or "").strip().lower()

    if basin == lake.basin and lake.headwater in name:
        return True
    return bool(district) and district in lake.district.lower()


def rank_glof_watch(latest_scores: list[dict]) -> dict:
    """The six priority lakes, each cross-checked against live gauge signals.

    `latest_scores` is main._latest_scores()'s output -- one row per station
    with its newest score, joined to impoundment_suspected/reason. Sorting is
    by (rank, live_corroboration) so a lake with a currently-active precursor
    signal always surfaces above a quiet one of the same official rank.
    """
    statuses = []
    for lake in PRIORITY_LAKES:
        nearby = [s for s in latest_scores if _matches_lake(s, lake)]
        active = [s for s in nearby if s.get("impoundment_suspected")]

        if active:
            confidence = "moderate"
            corroborated = True
            note = ("Live precursor signal: " +
                    "; ".join(f"{s['name']} ({s.get('impoundment_reason', 'flagged')})"
                              for s in active))
        elif nearby:
            corroborated = False
            confidence = "low"
            note = (f"{len(nearby)} gauge(s) monitored in this headwater; "
                     "none currently show the falling-while-raining signature.")
        else:
            corroborated = False
            confidence = "low"
            note = "No DHM gauge currently mapped to this specific headwater reach."

        statuses.append(LakeStatus(
            lake=asdict(lake),
            live_corroboration=corroborated,
            confidence=confidence,
            nearby_stations=[{"id": s["id"], "name": s["name"],
                              "impoundment_suspected": bool(s.get("impoundment_suspected"))}
                              for s in nearby],
            note=note,
        ))

    # Rank I above II above III; within a rank, a live signal sorts first.
    rank_order = {"I": 0, "II": 1, "III": 2}
    statuses.sort(key=lambda s: (rank_order.get(s.lake["rank"], 9),
                                  not s.live_corroboration))

    return {
        "lakes": [asdict(s) for s in statuses],
        "context": BASIN_TOTALS,
        "scope": ("Known-lake ranking plus a live cross-check against the same "
                  "gauge-based precursor the river model already uses. This is "
                  "not a breach-time or breach-probability prediction -- there is "
                  "no validated outcome history to backtest one against."),
    }
