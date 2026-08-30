"""Emergency contacts.

A wrong number in a flood-warning system is worse than no number, so every
entry below records where it came from and whether it was independently
verified. Nothing here is from memory.

An earlier build showed "1155" as *the* emergency line. That is the Nepal
Police public helpline, not the disaster hotline: the National Emergency
Operation Centre is 1149 and the health line is 1115. Both are listed now,
labelled for what they actually are.

Verification status:
  verified   confirmed against the issuing body's own site during development
  official   published by NDRRMA on their emergency helpline notice
"""
from dataclasses import asdict, dataclass


@dataclass
class Contact:
    label: str
    label_ne: str
    number: str
    scope: str          # national | district
    kind: str           # police | medical | disaster | fire
    source: str
    verified: str       # verified | official
    district: str = ""
    priority: int = 50  # lower sorts first


# --- national -------------------------------------------------------------
NATIONAL = [
    Contact("Nepal Police", "नेपाल प्रहरी", "100", "national", "police",
            "NDRRMA emergency helpline notice", "verified", priority=1),
    Contact("Ambulance", "एम्बुलेन्स", "102", "national", "medical",
            "NDRRMA emergency helpline notice", "verified", priority=2),
    Contact("Health helpline (HEOC)", "स्वास्थ्य हेल्पलाइन", "1115", "national", "medical",
            "heoc.mohp.gov.np", "verified", priority=3),
    Contact("National Emergency Operation Centre", "राष्ट्रिय आपत्कालीन कार्य सञ्चालन केन्द्र",
            "1149", "national", "disaster", "NEOC / MoHA", "official", priority=4),
    Contact("District Emergency Operation Centre", "जिल्ला आपत्कालीन कार्य सञ्चालन केन्द्र",
            "1234", "national", "disaster",
            "NDRRMA emergency helpline notice", "official", priority=5),
    Contact("Armed Police Force", "सशस्त्र प्रहरी बल", "1114", "national", "police",
            "NDRRMA emergency helpline notice", "official", priority=6),
    Contact("Fire brigade", "दमकल", "101", "national", "fire",
            "Nepal national short codes", "official", priority=7),
    Contact("Nepal Police public helpline", "नेपाल प्रहरी हेल्पलाइन", "1155",
            "national", "police", "Nepal Police", "official", priority=8),
]

# --- district ---------------------------------------------------------------
# From the NDRRMA notice issued for the 2026 monsoon response. These are the
# districts on that notice, not a complete national list, and they date the
# notice: treat them as best-effort and fall back to 1234 (DEOC).
DISTRICT = [
    Contact("Assistant CDO, Rasuwa", "सहायक प्रमुख जिल्ला अधिकारी, रसुवा",
            "9851164422", "district", "disaster",
            "NDRRMA emergency helpline notice", "official", district="Rasuwa"),
    Contact("Rasuwa Police", "रसुवा प्रहरी", "9851195496", "district", "police",
            "NDRRMA emergency helpline notice", "official", district="Rasuwa"),
    Contact("Assistant CDO, Nuwakot", "सहायक प्रमुख जिल्ला अधिकारी, नुवाकोट",
            "9851194877", "district", "disaster",
            "NDRRMA emergency helpline notice", "official", district="Nuwakot"),
    Contact("Nuwakot Police", "नुवाकोट प्रहरी", "9851192380", "district", "police",
            "NDRRMA emergency helpline notice", "official", district="Nuwakot"),
    Contact("CDO, Dhading", "प्रमुख जिल्ला अधिकारी, धादिङ",
            "9851194777", "district", "disaster",
            "NDRRMA emergency helpline notice", "official", district="Dhading"),
    Contact("Dhading Police", "धादिङ प्रहरी", "9851192986", "district", "police",
            "NDRRMA emergency helpline notice", "official", district="Dhading"),
    Contact("Assistant CDO, Chitwan", "सहायक प्रमुख जिल्ला अधिकारी, चितवन",
            "9855088891", "district", "disaster",
            "NDRRMA emergency helpline notice", "official", district="Chitwan"),
    Contact("Chitwan Police", "चितवन प्रहरी", "9855013999", "district", "police",
            "NDRRMA emergency helpline notice", "official", district="Chitwan"),
]

NOTICE = {
    "issuer": "National Disaster Risk Reduction and Management Authority (NDRRMA)",
    "issuer_ne": "राष्ट्रिय विपद् जोखिम न्यूनीकरण तथा व्यवस्थापन प्राधिकरण",
    "office": "Singha Durbar, Kathmandu",
    "message_ne": "आपतका बेला नआत्तिऔं, सजग र पूर्वतयार रहौं।",
    "message_en": "Do not panic in an emergency. Stay alert and prepared.",
}


def contacts(district: str | None = None) -> dict:
    """National contacts always; district contacts when one is named and known."""
    nat = sorted(NATIONAL, key=lambda c: c.priority)
    dist = [c for c in DISTRICT if not district
            or c.district.lower() == (district or "").strip().lower()]
    if district:
        dist = [c for c in DISTRICT if c.district.lower() == district.strip().lower()]
    return {
        "notice": NOTICE,
        "national": [asdict(c) for c in nat],
        "district": [asdict(c) for c in dist],
        "districts_covered": sorted({c.district for c in DISTRICT}),
        "note": ("District numbers come from a dated NDRRMA notice and may change. "
                 "If one does not answer, dial 1234 for the District Emergency "
                 "Operation Centre."),
    }
