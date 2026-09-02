"""Nepal country-profile reference data: census demographics and protected areas.

Static, like emergency.py and relief.py -- this is not scraped or refreshed on
a cycle, because a national census runs once a decade and a protected-area
boundary does not move week to week. It is refreshed by editing this file when
a newer official figure is published, with the source named at the point of
use rather than claimed in aggregate.

Every number below traces to a specific publication, not an inference. Where
two official-adjacent sources disagreed on a figure (Blackbuck Conservation
Area is reported as both 15.95 and 16.95 sq km depending on outlet), the
number is annotated rather than silently picked.
"""
from dataclasses import asdict, dataclass

# ---------------------------------------------------------------------------
# Demographics -- 2021 National Population and Housing Census
# ---------------------------------------------------------------------------
CENSUS_SOURCE = {
    "publisher": "National Statistics Office (NSO), Government of Nepal",
    "publication": "12th National Population and Housing Census 2021",
    "url": "https://censusnepal.cbs.gov.np/Home/Index/EN",
    "results_explorer_url": "https://censusresults.nsonepal.gov.np/",
    "portal_note": "NSO's own site (nsonepal.gov.np) links out to this dedicated "
                   "census portal under Statistics Portal -> National Population "
                   "Census 2021 Portal; microdata.nsonepal.gov.np carries the "
                   "underlying dataset for the same census. The figures below are "
                   "national totals -- censusresults.nsonepal.gov.np is NSO's own "
                   "interactive explorer and goes down to province/district/"
                   "municipality level (e.g. ?province=1&district=12&municipality=14), "
                   "with caste/ethnicity, language, household and social-indicator "
                   "charts per location. Linked rather than mirrored here, the same "
                   "way the Updates tab links to DHM and NDRRMA rather than "
                   "reproducing their content: 753 municipalities of drill-down "
                   "data belongs in NSO's own tool, not duplicated and going stale "
                   "in this one.",
    "note": ("142 distinct caste/ethnic groups were recorded, the most of any "
             "Nepali census. Percentages below are the major groups as reported "
             "by NSO; the long tail (32%+) is spread across the remaining ~130 "
             "groups and is not broken out individually here."),
}

# name, share of national population (%). Source: NSO 2021 census, as reported
# by Kathmandu Post (2023-06-03) and cross-checked against the CIA World
# Factbook's 2023 ethnic-groups table, which cites the same census.
CASTE_ETHNICITY_MAJOR_GROUPS = [
    {"group": "Chhetri", "percent": 16.45},
    {"group": "Brahmin (Hill)", "percent": 11.29},
    {"group": "Magar", "percent": 6.90},
    {"group": "Tharu", "percent": 6.20},
    {"group": "Tamang", "percent": 5.62},
    {"group": "Bishwakarma", "percent": 5.04},
    {"group": "Newar", "percent": 4.60},
    {"group": "Muslim", "percent": 4.86},
    {"group": "Yadav", "percent": 4.21},
    {"group": "Rai", "percent": 2.20},
]

# Broader statutory classifications NSO/the census itself uses, which cut
# across the individual groups above (a Chhetri and a Brahmin are both
# Khas-Arya, for instance) -- included because they are the categories used
# in Nepal's own inclusion policy, not because they are a second head-count.
BROAD_CLASSIFICATIONS = {
    "Dalit (all sub-groups)": 13.6,
    "note": ("Janajati, Khas-Arya (incl. Hill Dalit), Madhesi (incl. Madhesi "
             "Dalit) and Muslim are the four broad statutory categories the "
             "2021 census and national inclusion policy use; they overlap with "
             "the named groups above rather than sitting alongside them."),
}


def demographics() -> dict:
    return {
        "source": CENSUS_SOURCE,
        "total_groups_recorded": 142,
        "major_groups": CASTE_ETHNICITY_MAJOR_GROUPS,
        "broad_classifications": BROAD_CLASSIFICATIONS,
    }


# ---------------------------------------------------------------------------
# Wildlife -- protected areas (DNPWC) and flagship species counts
# ---------------------------------------------------------------------------
@dataclass
class ProtectedArea:
    name: str
    kind: str          # National Park | Wildlife Reserve | Hunting Reserve | Conservation Area
    area_km2: float | None
    established: int | None = None
    note: str = ""


# Nepal's protected-area system: 12 national parks, 1 wildlife reserve, 1
# hunting reserve, 6 conservation areas -- 23.39% of the country's land area,
# per DNPWC (Department of National Parks and Wildlife Conservation).
# area_km2 left as None where no figure could be corroborated across sources
# rather than guessed.
PROTECTED_AREAS = [
    ProtectedArea("Chitwan National Park", "National Park", 952.63, 1973,
                  "Nepal's first national park; UNESCO World Heritage Site."),
    ProtectedArea("Bardiya National Park", "National Park", 968.0),
    ProtectedArea("Banke National Park", "National Park", 550.0),
    ProtectedArea("Shuklaphanta National Park", "National Park", 305.0),
    ProtectedArea("Parsa National Park", "National Park", 627.39),
    ProtectedArea("Sagarmatha National Park", "National Park", 1148.0, 1976,
                  "Includes Mount Everest; UNESCO World Heritage Site."),
    ProtectedArea("Langtang National Park", "National Park", 1710.0),
    ProtectedArea("Rara National Park", "National Park", None, None,
                  "Nepal's smallest national park; area not corroborated across sources."),
    ProtectedArea("Khaptad National Park", "National Park", 225.0),
    ProtectedArea("Makalu Barun National Park", "National Park", 1500.0),
    ProtectedArea("Shivapuri Nagarjun National Park", "National Park", 159.0),
    ProtectedArea("Shey Phoksundo National Park", "National Park", 3555.0,
                  note="Nepal's largest national park."),
    ProtectedArea("Koshi Tappu Wildlife Reserve", "Wildlife Reserve", 175.0),
    ProtectedArea("Dhorpatan Hunting Reserve", "Hunting Reserve", 1325.0, 1987),
    ProtectedArea("Annapurna Conservation Area", "Conservation Area", 7629.0,
                  note="Nepal's largest protected area."),
    ProtectedArea("Kanchenjunga Conservation Area", "Conservation Area", 2035.0),
    ProtectedArea("Gaurishankar Conservation Area", "Conservation Area", 2179.0),
    ProtectedArea("Api Nampa Conservation Area", "Conservation Area", 1903.0),
    ProtectedArea("Manaslu Conservation Area", "Conservation Area", 1663.0),
    ProtectedArea("Blackbuck (Krishnasaar) Conservation Area", "Conservation Area", 16.0,
                  established=2009,
                  note="Nepal's smallest conservation area; sources report "
                       "15.95-16.95 sq km. Gulariya, Bardiya District."),
]

PROTECTED_AREA_SOURCE = {
    "publisher": "Department of National Parks and Wildlife Conservation (DNPWC), "
                 "Government of Nepal",
    "url": "https://dnpwc.gov.np/en/",
    "note": "12 national parks, 1 wildlife reserve, 1 hunting reserve, 6 "
            "conservation areas; ~23.39% of Nepal's land area under protection.",
}

# Flagship species counts, most recent national survey. These are periodic
# (multi-year) census exercises, not annual figures -- the survey year is
# part of the citation, not a footnote.
SPECIES_COUNTS = [
    {
        "species": "Bengal tiger", "count": 429, "survey_year": 2026,
        "breakdown": {"Chitwan": 145, "Bardiya": 112, "Parsa": 71,
                      "Banke": 51, "Shuklaphanta": 50},
        "source": "Nepal national tiger census 2026, via IUCN / Global Tiger Day reporting",
        "source_url": "https://iucn.org/story/202607/tiger-census-nepals-tiger-number-increases-429",
    },
    {
        "species": "Greater one-horned rhinoceros", "count": 752, "survey_year": 2021,
        "note": "Second-largest concentration of the species worldwide, after "
                "Kaziranga National Park, India.",
        "source": "DNPWC national rhino count 2021",
        "source_url": "https://news.mongabay.com/2025/10/amid-challenges-nepal-plans-its-latest-tiger-rhino-counts/",
    },
    {
        "species": "Asian elephant", "count": 230, "survey_year": None,
        "note": "Rounded estimate; population reported as increasing. No single "
                "comprehensive national count year identified in sourcing.",
        "source": "NTNC Elephant Conservation Action Plan for Nepal (2025-2035)",
        "source_url": "https://ntnc.org.np/publication/elephant-conservation-action-plan-nepal-2025-2035",
    },
]


def wildlife() -> dict:
    return {
        "protected_areas": {
            "source": PROTECTED_AREA_SOURCE,
            "areas": [asdict(a) for a in PROTECTED_AREAS],
        },
        "species_counts": SPECIES_COUNTS,
    }
