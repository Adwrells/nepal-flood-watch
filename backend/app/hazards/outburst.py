"""Outburst flood module: landslide-dammed and moraine-dammed lake failure.

Why this is separate from the river-gauge model
-----------------------------------------------
The Rasuwa / Bhote Koshi event of 8 July 2025 is the pattern this exists for: a
mass movement high in the Tibetan headwaters impounded a river, the lake filled
over hours, the barrier failed, and the surge reached Rasuwagadhi with almost no
warning. A gauge-threshold model is structurally blind to that, because the
diagnostic signal is not a rising river -- it is a river that goes ABNORMALLY
QUIET while it is raining upstream. Water is being stored behind a barrier.

So this module does three things:

1. detect_impoundment()  turns "small events" into a precursor. A negative
   discharge anomaly during upstream rainfall is the impoundment signature.
2. breach_hydrograph()   published dam-break relations give peak discharge,
   then attenuation and celerity give downstream arrival time and magnitude.
3. stability_index()     Ermini & Casagli's DBI says whether a barrier that has
   already formed is likely to hold or to fail.

Every constant is sourced in docs/ARCHITECTURE.md. None of it is invented.
"""
import math
from dataclasses import dataclass, field

G = 9.80665          # m/s^2
RHO_W = 1000.0       # kg/m^3

# Basins with a transboundary or glacial headwater, where the impounding barrier
# can form outside Nepal's gauge network.
#
# These MUST be DHM's own basin vocabulary, not tributary names. An earlier
# version listed "bhote koshi", "trishuli", "tama koshi" and so on; DHM records
# those gauges under the major basin they drain into, so only "Karnali" of ten
# entries ever matched and the lower threshold never reached the Tibet-fed
# headwaters it exists for.
TRANSBOUNDARY_BASINS = {
    "koshi", "narayani", "karnali", "mahakali", "bheri", "babai",
}

# The basin alone is coarse: "Narayani" covers both the Trishuli headwaters at
# the Tibetan border and the plains at Chitwan. Station names carry the actual
# headwater identity, so they refine it.
HEADWATER_NAMES = (
    "bhote", "trishuli", "langtang", "tama koshi", "tamakoshi", "arun",
    "sun koshi", "sunkoshi", "marsyangdi", "budhi gandaki", "budhigandaki",
    "seti", "kali gandaki", "kaligandaki", "humla", "rasuwa", "dudh koshi",
)

# Absolute floors, which the proportional test alone cannot provide.
# A gauge must have enough water in it for a percentage to mean anything, and
# the fall must be large enough in metres to be a river being held back rather
# than a shallow stream receding after rain.
MIN_BASELINE_M = 0.50
MIN_DROP_M = 0.30


def is_transboundary(station) -> bool:
    """True for gauges on a Tibet-fed or glacial headwater reach."""
    basin = (station.get("basin") or "").strip().lower()
    name = (station.get("name") or "").strip().lower()
    return (basin in TRANSBOUNDARY_BASINS
            and any(h in name for h in HEADWATER_NAMES))


# ---------------------------------------------------------------------------
# 1. Precursor detection -- runs every cycle
# ---------------------------------------------------------------------------
@dataclass
class ImpoundmentSignal:
    station_id: int
    station: str
    basin: str
    suspected: bool
    anomaly: float                  # 0-1, fraction of expected flow that is missing
    rain_mm: float
    reason: str
    confidence: str                 # low | moderate | high
    watch_reasons: list = field(default_factory=list)


def detect_impoundment(station, recent_levels, rain_past_24h, rain_next_12h) -> ImpoundmentSignal:
    """Flag a gauge whose level is falling while its catchment is being rained on.

    `recent_levels` is [(ts, level), ...] oldest-first. We compare the newest
    reading against the MEDIAN of the preceding window, not the single previous
    reading, so one dropped telemetry packet cannot raise a false alarm.

    Physical basis: during rainfall, baseflow plus direct runoff means stage
    should be flat or rising. A sustained fall while 20 mm+ is landing on the
    catchment means water is not arriving -- it is being stored somewhere.
    """
    basin = (station.get("basin") or "").strip().lower()
    name = station.get("name", "")
    reasons: list[str] = []

    levels = [lv for _, lv in recent_levels if lv is not None]
    if len(levels) < 4:
        return ImpoundmentSignal(station["id"], name, basin, False, 0.0,
                                 rain_past_24h or 0.0, "insufficient history", "low")

    current = levels[-1]
    prior = levels[:-1]
    baseline = sorted(prior)[len(prior) // 2]          # median of prior window
    if baseline <= 0:
        return ImpoundmentSignal(station["id"], name, basin, False, 0.0,
                                 rain_past_24h or 0.0, "no usable baseline", "low")

    anomaly = max(0.0, (baseline - current) / baseline)
    drop_m = max(0.0, baseline - current)
    rain = (rain_past_24h or 0.0) + (rain_next_12h or 0.0)

    # Transboundary headwaters get a lower bar: the barrier may form entirely
    # outside our observation network, so we cannot wait for confirmation.
    transboundary = is_transboundary(station)
    drop_threshold = 0.10 if transboundary else 0.15
    rain_threshold = 15.0 if transboundary else 25.0

    # A shallow gauge cannot support a proportional test. On a 0.1 m urban
    # khola a 76% fall is 8 cm of ordinary recession, and treating that as a
    # dammed river buries the real signal in noise.
    too_shallow = baseline < MIN_BASELINE_M
    if too_shallow:
        return ImpoundmentSignal(
            station["id"], name, basin, False, round(anomaly, 3), round(rain, 1),
            f"gauge too shallow to assess ({baseline:.2f} m baseline)", "low")

    if anomaly >= drop_threshold:
        reasons.append(f"stage down {anomaly:.0%} ({drop_m:.2f} m) vs prior median")
    if rain >= rain_threshold:
        reasons.append(f"{rain:.0f} mm rain on catchment")
    if transboundary:
        reasons.append("transboundary/glacial headwater - barrier may be unobserved")

    suspected = (anomaly >= drop_threshold
                 and drop_m >= MIN_DROP_M
                 and rain >= rain_threshold)
    if not suspected:
        confidence = "low"
    elif anomaly >= 0.30 and rain >= 50:
        confidence = "high"
    else:
        confidence = "moderate"

    return ImpoundmentSignal(
        station_id=station["id"], station=name, basin=basin, suspected=suspected,
        anomaly=round(anomaly, 3), rain_mm=round(rain, 1),
        reason="; ".join(reasons) or "nominal",
        confidence=confidence, watch_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# 2. Breach physics -- what happens if the barrier fails
# ---------------------------------------------------------------------------
def peak_discharge_froehlich(volume_m3: float, head_m: float) -> float:
    """Froehlich (1995b) regression over 22 documented embankment breaches.

        Qp = 0.607 * Vw^0.295 * Hw^1.24        [m3/s]

    Vw = volume of water above the breach invert, Hw = its depth.
    """
    if volume_m3 <= 0 or head_m <= 0:
        return 0.0
    return 0.607 * volume_m3 ** 0.295 * head_m ** 1.24


def peak_discharge_costa_schuster(volume_m3: float, head_m: float) -> float:
    """Costa & Schuster (1988) potential-energy relation, fitted to LANDSLIDE dams.

        PE = rho * g * V * H          [J]
        Qp = 0.763 * PE^0.42          [m3/s]

    Kept alongside Froehlich because the two disagree by up to a factor of ~2 on
    natural, unengineered barriers. We report the envelope, not a false point
    estimate -- an evacuation decision deserves the honest spread.
    """
    if volume_m3 <= 0 or head_m <= 0:
        return 0.0
    pe = RHO_W * G * volume_m3 * head_m
    return 0.763 * pe ** 0.42


def manning_celerity(slope: float = 0.02, n: float = 0.05, hyd_radius_m: float = 3.0) -> float:
    """Kinematic flood-wave celerity for a steep boulder-bed Himalayan channel.

        V = (1/n) * R^(2/3) * S^(1/2)      Manning
        c = (5/3) * V                      wide-channel kinematic wave

    Defaults: S = 0.02 (2%, typical Nepali middle-mountain reach), n = 0.05
    (large boulders, irregular section), R = 3 m. These give c ~ 9.8 m/s, i.e.
    ~35 km/h, consistent with the observed Rasuwa 2025 propagation.
    """
    v = (1.0 / n) * hyd_radius_m ** (2.0 / 3.0) * math.sqrt(slope)
    return (5.0 / 3.0) * v


def attenuate(qp: float, distance_km: float, k_per_km: float = 0.008) -> float:
    """Peak attenuation with distance: Q(x) = Qp * exp(-k*x).

    k = 0.008/km is the low-loss end, appropriate for confined bedrock gorges
    where a surge holds together. Raise k for braided or terai reaches.
    """
    return qp * math.exp(-k_per_km * max(0.0, distance_km))


def breach_hydrograph(volume_m3, head_m, targets, slope=0.02, n=0.05) -> dict:
    """Downstream picture for one hypothetical barrier failure.

    `targets` is [{"name": ..., "distance_km": ...}, ...] downstream of the site.
    Returns the peak-discharge envelope plus arrival time and attenuated peak at
    each target. Arrival time is what an evacuation order is actually built on.
    """
    q_fro = peak_discharge_froehlich(volume_m3, head_m)
    q_cs = peak_discharge_costa_schuster(volume_m3, head_m)
    q_lo, q_hi = min(q_fro, q_cs), max(q_fro, q_cs)
    c = manning_celerity(slope, n)

    downstream = []
    for t in targets:
        d = t["distance_km"]
        downstream.append({
            "name": t["name"],
            "distance_km": d,
            "eta_minutes": round((d * 1000.0 / c) / 60.0, 1),
            "peak_low_cumecs": round(attenuate(q_lo, d)),
            "peak_high_cumecs": round(attenuate(q_hi, d)),
        })

    return {
        "impounded_volume_m3": volume_m3,
        "head_m": head_m,
        "peak_discharge_cumecs": {
            "froehlich": round(q_fro),
            "costa_schuster": round(q_cs),
            "envelope": [round(q_lo), round(q_hi)],
        },
        "wave_celerity_ms": round(c, 2),
        "downstream": downstream,
    }


def stability_index(dam_height_m, dam_volume_m3, catchment_area_km2) -> dict:
    """Dimensionless Blockage Index, Ermini & Casagli (2003).

        DBI = log10( A * H / V )

    UNITS MATTER and are easy to get wrong: A is the upstream catchment in km2,
    H the barrier height in m, and V the barrier volume in MILLIONS of m3. We
    take V in plain m3 at the call site and convert here, because every other
    volume in this codebase is in m3 and a silent unit switch would be worse.

    DBI < 2.75   barrier likely stable
    2.75 - 3.08  uncertain, monitor continuously
    DBI > 3.08   unstable, failure expected

    Sanity check against Tangjiashan (Wenchuan 2008): A=3550 km2, H=82 m,
    V=20.4e6 m3 gives DBI 4.15 -- unstable, which is exactly why the PLA had to
    cut an emergency spillway. Landslide dams that fail overwhelmingly do so
    within days of forming, which is why the impoundment detector above matters
    more than a field survey that takes a week to mobilise.
    """
    if dam_volume_m3 <= 0 or dam_height_m <= 0 or catchment_area_km2 <= 0:
        return {"dbi": None, "verdict": "insufficient data"}
    volume_mm3 = dam_volume_m3 / 1e6                       # m3 -> 10^6 m3
    dbi = math.log10(catchment_area_km2 * dam_height_m / volume_mm3)
    verdict = ("likely stable" if dbi < 2.75
               else "uncertain - monitor" if dbi <= 3.08
               else "unstable - failure expected")
    return {"dbi": round(dbi, 3), "verdict": verdict}


# ---------------------------------------------------------------------------
# 3. Reference scenario -- Rasuwa / Bhote Koshi, 8 July 2025
# ---------------------------------------------------------------------------
RASUWA_2025 = {
    "name": "Bhote Koshi / Lende Khola outburst, 8 July 2025",
    "note": ("Supraglacial / landslide-dammed lake in the Tibetan headwaters. "
             "The surge destroyed the Miteri bridge at Rasuwagadhi and swept "
             "down the Trishuli. Used here to calibrate the celerity and "
             "attenuation defaults. Volume and head are order-of-magnitude "
             "reconstructions, not surveyed values."),
    "volume_m3": 6.0e6,
    "head_m": 35.0,
    "targets": [
        {"name": "Rasuwagadhi / Miteri Bridge", "distance_km": 5},
        {"name": "Timure", "distance_km": 9},
        {"name": "Syaphrubesi", "distance_km": 22},
        {"name": "Betrawati", "distance_km": 68},
        {"name": "Trishuli confluence", "distance_km": 95},
    ],
}


def reference_scenario() -> dict:
    """Worked Rasuwa case, served by /api/outburst/scenario."""
    out = breach_hydrograph(RASUWA_2025["volume_m3"], RASUWA_2025["head_m"],
                            RASUWA_2025["targets"])
    out["name"] = RASUWA_2025["name"]
    out["note"] = RASUWA_2025["note"]
    return out
