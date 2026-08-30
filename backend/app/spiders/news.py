"""Official + mainstream news feeds, filtered to flood language.

RSS only, on purpose: it is the sanctioned machine-readable surface, it is
stable, and it keeps us off article HTML that changes weekly. Each item is
geotagged by matching Nepal district names in the headline, which is coarse but
enough to corroborate a gauge in the same district.

Feeds that 404 or go quiet are reported by /api/health rather than crashing the
cycle -- Nepali news sites move their feed paths from time to time.
"""
import hashlib
import re
from xml.etree.ElementTree import ParseError

# defusedxml, not xml.etree: these are five third-party feeds, and the stdlib
# parser is vulnerable to entity-expansion ("billion laughs") and external-entity
# attacks. A publisher does not have to be malicious for this to matter -- a
# compromised CMS is enough.
import defusedxml.ElementTree as ET
import httpx

from .base import Spider

# Verified reachable and actually carrying items. Feeds that 404 or 500 were
# dropped rather than left in to fail every cycle: The Himalayan Times,
# My Republica and Setopati all had dead feed paths at time of writing.
FEEDS = {
    "The Rising Nepal (state-owned)": "https://risingnepaldaily.com/rss",
    "Kathmandu Post": "https://kathmandupost.com/rss",
    "Online Khabar EN": "https://english.onlinekhabar.com/feed",
    "Nepal News": "https://nepalnews.com/feed",
    "Ratopati (Nepali)": "https://ratopati.com/feed",
}

# Matched against the headline; Nepali script included for the local-language feeds.
FLOOD_WORDS = re.compile(
    r"\b(flood|flash flood|inundat|landslide|swollen|overflow|washed away|"
    r"submerg|displaced|rainfall|downpour|monsoon|embankment|"
    r"बाढी|पहिरो|डुबान|वर्षा)\b",
    re.I,
)

DISTRICTS = [
    "Achham", "Arghakhanchi", "Baglung", "Baitadi", "Bajhang", "Bajura", "Banke",
    "Bara", "Bardiya", "Bhaktapur", "Bhojpur", "Chitwan", "Dadeldhura", "Dailekh",
    "Dang", "Darchula", "Dhading", "Dhankuta", "Dhanusha", "Dolakha", "Dolpa",
    "Doti", "Gorkha", "Gulmi", "Humla", "Ilam", "Jajarkot", "Jhapa", "Jumla",
    "Kailali", "Kalikot", "Kanchanpur", "Kapilvastu", "Kaski", "Kathmandu",
    "Kavrepalanchok", "Khotang", "Lalitpur", "Lamjung", "Mahottari", "Makwanpur",
    "Manang", "Morang", "Mugu", "Mustang", "Myagdi", "Nawalparasi", "Nuwakot",
    "Okhaldhunga", "Palpa", "Panchthar", "Parbat", "Parsa", "Pyuthan", "Ramechhap",
    "Rasuwa", "Rautahat", "Rolpa", "Rukum", "Rupandehi", "Salyan", "Sankhuwasabha",
    "Saptari", "Sarlahi", "Sindhuli", "Sindhupalchok", "Siraha", "Solukhumbu",
    "Sunsari", "Surkhet", "Syangja", "Tanahun", "Taplejung", "Terhathum",
    "Udayapur", "Rukum East", "Rukum West",
]


class NewsSpider(Spider):
    name = "news"
    start_urls = list(FEEDS.values())

    def parse(self, response: httpx.Response) -> list[dict]:
        # Match on host, not full URL: several of these feeds redirect (adding a
        # trailing slash or a www.), so an exact URL comparison loses the name.
        host = response.url.host.removeprefix("www.")
        source = next(
            (n for n, u in FEEDS.items() if httpx.URL(u).host.removeprefix("www.") == host),
            host,
        )
        try:
            root = ET.fromstring(response.content)
        except ParseError:
            return []                                   # feed served HTML, skip

        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not FLOOD_WORDS.search(title):
                continue
            link = (item.findtext("link") or "").strip()
            hit = [d for d in DISTRICTS if d.lower() in title.lower()]
            out.append(
                {
                    "id": hashlib.sha256(link.encode()).hexdigest()[:16],
                    "title": title,
                    "url": link,
                    "published": (item.findtext("pubDate") or "").strip(),
                    "source": source,
                    "districts": ",".join(hit),
                }
            )
        return out
