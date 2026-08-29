# Nepal Flood Watch

A live flood early-warning console for Nepal. The system scrapes the country's
official hydrological, disaster and news sources every 12 minutes, scores every
river gauge on a 0–100 severity index, forecasts where each gauge is heading,
and publishes the result as an interactive map, an Excel workbook and a JSON
snapshot.

> This is decision support. The Department of Hydrology and Meteorology (DHM)
> and the Ministry of Home Affairs (MoHA) issue Nepal's authoritative warnings.
> Emergency toll-free: **1155**.

---

## What it does

The system monitors **309 DHM river gauges** across Nepal. For each one it
computes a Flood Severity Index from four weighted components, estimates the
probability that the gauge passes its danger mark within six hours, projects the
water level twelve hours ahead, and produces a ranked list of actions gated by
how much lead time remains.

Alongside the river model it tracks earthquakes (which trigger the landslides
that dam rivers), active fires (a burned catchment loses infiltration capacity),
official disaster incidents, and flood coverage from five news feeds including
the state-owned *Rising Nepal*.

It also carries a hazard model that a gauge-threshold system cannot express:
**landslide- and moraine-dammed lake outburst floods**, the class of event that
struck Rasuwa in July 2025.

---

## Quick start

```bash
git clone https://github.com/Adwrells/nepal-flood-watch
cd nepal-flood-watch
```

`launch.py` runs identically on Linux, macOS and Windows. It creates the
virtual environment and installs dependencies on first run, then re-executes
itself inside it, so nothing beyond the standard library is needed to start.

```bash
python launch.py
```

The console comes up at **http://127.0.0.1:8000**. The first cycle runs
immediately, so the map is populated within about thirty seconds.

| Command | Does |
|---------|------|
| `python launch.py` | Serve the console (same as `serve`) |
| `python launch.py serve --host 0.0.0.0 --port 9000` | Serve on a chosen address |
| `python launch.py check` | Deployment preflight, 20 checks |
| `python launch.py check --offline` | Preflight without live source calls |
| `python launch.py cycle` | Run one collection cycle and exit |
| `python launch.py tiles` | Warm the offline map cache (~16,600 tiles, ~195 MB) |
| `python launch.py setup` | Create the venv and install dependencies only |

Requires Python 3.11+.


Configuration is optional — every setting has a working default. Copy
`.env.example` to `.env` only to change the refresh cadence or enable the fire
layer.

---

## Data sources

Five of the six sources need no API key.

| Source | Provides | Key |
|--------|----------|-----|
| [DHM river watch](https://www.dhm.gov.np/hydrology/river-watch) | 309 gauges: stage, warning and danger marks, coordinates | none |
| [BIPAD Portal](https://bipadportal.gov.np/) (MoHA) | Official disaster incidents, filtered to water hazards | none |
| [Open-Meteo](https://open-meteo.com/) | Rainfall: 24 h observed + 12 h forecast per gauge | none |
| [USGS FDSN](https://earthquake.usgs.gov/fdsnws/event/1/) | Earthquakes in the Nepal bounding box | none |
| News RSS ×5 | Rising Nepal, Kathmandu Post, Online Khabar, Nepal News, Ratopati | none |
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/area/) | VIIRS active-fire detections | free |

Without a FIRMS key the fire layer is simply disabled; everything else runs.

**Facebook** is supported through the Graph API only, for Pages the operator
owns or administers. Scraping facebook.com HTML violates their Terms of Service
and is not implemented.

---

## The severity model

```
FSI = 0.50·level + 0.25·rise + 0.18·rain + 0.07·corroboration
```

| Component | What it measures | Full scale |
|-----------|------------------|-----------|
| Level | Proximity to DHM's published danger mark | at danger = 85–100 |
| Rise | Rate of climb, the lead indicator | 0.50 m/h |
| Rain | 24 h observed + 12 h forecast over the gauge | 200 mm |
| Corroboration | Official incidents and headlines nearby | incident within 25 km |

**Bands:** SEVERE ≥ 90 · DANGER ≥ 75 · WARNING ≥ 50 · WATCH ≥ 25 · NORMAL < 25

A slow river at 90% of its danger mark is calmer than a fast one at 60%, which
is why rate of rise carries a quarter of the weight.

---

## Outburst floods

The July 2025 Rasuwa / Bhote Koshi disaster is the reason this module exists. A
mass movement in the Tibetan headwaters impounded the river, the lake filled
over hours, the barrier failed, and the surge reached Rasuwagadhi with almost no
warning.

A threshold model is structurally blind to that event, because the diagnostic
signal is **not a rising river**. It is a river that goes abnormally quiet while
rain is falling on its catchment, because water is being stored behind a
barrier.

The system detects that signature directly, applies published dam-break
relations (Froehlich 1995b; Costa & Schuster 1988) to estimate the peak
discharge envelope, and uses Manning-based wave celerity to give downstream
arrival times. Barrier stability is assessed with the Ermini & Casagli
Dimensionless Blockage Index, validated against Tangjiashan (Wenchuan 2008) on
every preflight run.

Transboundary basins get a lower alarm threshold, because the barrier may form
entirely outside Nepal's observation network. A suspected impoundment floors the
band at WARNING — otherwise the plain score would report a falling river as calm
at exactly the wrong moment.

Full derivations are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## On dams and Earth's rotation

The system includes a worked answer to a claim that comes up often: that Chinese
dams and mountain excavation slow Earth's rotation and thereby cause floods in
Nepal.

Half of that is real physics, and the module computes it properly. Filling the
Three Gorges reservoir does lengthen the day, by about **0.12 microseconds**.

It has no bearing on flooding. That figure is roughly 8,000× smaller than the
ordinary seasonal swing in day length, and no term linking rotation rate to
river discharge exists in the governing equations.

The genuine transboundary risk is different and is modelled for real: barriers
can form in Tibetan headwaters that Nepal's gauge network cannot observe, and
cross-border real-time data sharing is limited. That observability gap is what
produced the Rasuwa surge. `GET /api/explain/earth-rotation` returns the full
calculation.

---

## Outputs

**Interactive console** — dark and light themes, five toggleable map layers
(gauges, impoundment watch, events and alerts, earthquakes, fires), a
severity-ranked gauge rail, and a detail drawer showing the score breakdown, a
forecast sparkline with its prediction band, and the recommended actions.

Event pins are teardrops rather than discs so an event never reads as a gauge,
and headline pins placed at a district centroid are drawn hollow because their
location is inferred rather than surveyed.

**Excel workbook** — six sheets, rewritten atomically every cycle: Dashboard
(band counts and the top 25), Stations (the full scored table with conditional
formatting), Rainfall, Incidents, News, and Method.

**JSON snapshot** and a **REST API** at `/api/*`.

---

## API

| Endpoint | Returns |
|----------|---------|
| `GET /api/stations` | All gauges with current scores; filter by `band` or `min_fsi` |
| `GET /api/summary` | Band counts, KPIs, last-cycle health |
| `GET /api/station/{id}` | Full analytics ladder plus history for one gauge |
| `GET /api/events` | Placeable markers: model alerts, incidents, geolocated news |
| `GET /api/hazards` | Earthquake and fire events |
| `GET /api/outburst/alerts` | Gauges showing the impoundment signature |
| `GET /api/outburst/scenario` | Breach model; accepts `volume_m3` and `head_m` |
| `GET /api/explain/earth-rotation` | The length-of-day calculation |
| `GET /api/health` | Per-source status and data-quality report |
| `POST /api/refresh` | Force a cycle now |
| `GET /api/export.xlsx` · `/api/export.json` | Current exports |

---

## Project layout

```
nepal-flood-watch/
├── backend/app/
│   ├── main.py          FastAPI routes, scheduler, static mount
│   ├── pipeline.py      the 12-minute cycle
│   ├── clean.py         standardisation; runs before anything else
│   ├── scoring.py       Flood Severity Index + breach probability
│   ├── analytics.py     forecast, time-to-danger, action playbook
│   ├── excel.py         six-sheet workbook, atomic write
│   ├── tiles.py         local OSM tile cache, clipped to Nepal
│   ├── regions.py       region registry — the extension point
│   ├── preflight.py     20 deployment checks
│   ├── spiders/         one file per source, Scrapy-shaped
│   └── hazards/         outburst physics, quake, fire, earth rotation
├── frontend/            index.html, app.js, styles.css — no build step
├── docs/ARCHITECTURE.md
└── launch.py            cross-platform launcher
```

The spiders follow Scrapy's `name` / `start_urls` / `parse` shape without the
dependency: Scrapy pulls in Twisted and a reactor that six polite requests every
twelve minutes do not need.

---

## Design decisions worth knowing

**Source isolation.** A dead news feed contributes nothing and the cycle
continues. Nepali news sites move their RSS paths often, and a flood-warning
system that goes dark because a newspaper changed its CMS is worse than useless.

**Never invent a value.** An unparseable reading becomes `None` and is excluded
from scoring, never defaulted to zero. A zero stage would read as "river is
empty", score as safe, and is the most dangerous possible failure mode here.

**Colour is never the only signal.** Every marker and row carries the numeric
index and the band name, the severity ramp is monotonic in lightness as well as
hue, and event pins differ from gauges in shape.

**No machine learning.** With ~300 gauges reporting irregularly and no labelled
flood outcomes, a learned model would be unvalidatable. Every number traces to a
published relation or a stated weight.

---

## Extending to other countries

`regions.py` holds the registry. Nepal is the default and the only enabled
region; Bhutan and Uttarakhand are declared and disabled.

Rainfall, earthquakes and fire detection are already global. Enabling a region
requires one thing: a national gauge adapter. Add a spider, list it in the
region's `sources`, set `enabled = True`.

---

## Attribution

Map tiles © [OpenStreetMap](https://www.openstreetmap.org/copyright)
contributors, © [CARTO](https://carto.com/attributions). Hydrological data from
DHM, Government of Nepal. Incident data from the BIPAD Portal, MoHA. Weather
from Open-Meteo. Seismic data from USGS. Fire detections from NASA FIRMS.
