r"""Reservoir mass redistribution and Earth's rotation -- worked exactly.

This module exists because the claim keeps coming up: that large dams and
mountain excavation in China are slowing Earth's rotation, and that this is
somehow driving floods in Nepal. Half of that is real physics. The other half
does not survive contact with the numbers, so here are the numbers.

THE REAL PART
    Filling a reservoir moves water from the globally distributed ocean to one
    spot, changing its distance from the SPIN AXIS. That changes Earth's moment
    of inertia, and conservation of angular momentum (L = I*omega) means the day
    length changes. This is a genuine, published, measured-in-principle effect.

        dI = m * [ (R+h)^2 * cos^2(lat_res) - R^2 * <cos^2(lat)>_ocean ]
        dLOD / LOD = dI / I

    Note which term dominates: the LATITUDE change, not the 175 m lift. The
    lift-only term is ~1000x smaller, and using it alone is the standard way to
    get this calculation wrong by three orders of magnitude.

THE PART THAT DOES NOT HOLD
    For Three Gorges this comes out at ~0.12 microseconds per day (Chao at NASA
    GSFC publishes ~0.06 us using a finer ocean-source model; same order). Set
    that against what the planet does unaided:

        seasonal atmospheric exchange   ~ +/- 1000 us   (~8,000x larger)
        2004 Sumatra M9.1 earthquake    ~     6.8 us    (~56x larger)
        2011 Tohoku M9.1 earthquake     ~     1.8 us    (~15x larger)
        long-term tidal braking         ~     2.3 ms/century

    So the dam signal sits about four orders of magnitude below the ordinary
    seasonal wobble in day length. And magnitude aside, there is no mechanism:
    length-of-day appears in no term of the Saint-Venant or shallow-water
    equations that govern river flow. Rotation reaches hydrology only through
    the Coriolis parameter f = 2*omega*sin(lat), and a fractional change in
    omega of ~1e-12 changes f by that same fraction -- unmeasurable, and
    irrelevant at the scale of a river.

WHAT IS ACTUALLY WORTH WORRYING ABOUT UPSTREAM
    The transboundary concern is real; it is simply a different one, and it is
    modelled for real in hazards/outburst.py. Barriers -- natural landslide
    dams, moraine dams, glacial lakes -- can form in Tibetan headwaters that
    Nepal's gauge network cannot see, and real-time cross-border hydrological
    data sharing is limited. That observability gap is what produced the July
    2025 Rasuwa surge. It is an information problem, not a rotational one.
"""
from dataclasses import dataclass

G = 9.80665
RHO_W = 1000.0
R_EARTH = 6.371e6          # m, mean radius
I_EARTH = 8.034e37         # kg m^2, polar moment of inertia
LOD_S = 86400.0            # s, nominal length of day
# Mean cos^2(latitude) over a uniform sphere -- where ocean water is drawn from.
MEAN_COS2_OCEAN = 2.0 / 3.0

# Benchmarks for context, in microseconds of length-of-day change.
BENCHMARKS_US = {
    "seasonal atmospheric angular momentum exchange": 1000.0,
    "2011 Tohoku M9.1 earthquake": 1.8,
    "2004 Sumatra M9.1 earthquake": 6.8,
    "one leap second": 1_000_000.0,
}


@dataclass
class Reservoir:
    name: str
    volume_km3: float
    lift_m: float           # mean elevation gain of the impounded water
    latitude_deg: float


# Reference cases. Three Gorges is the one usually named in the claim.
KNOWN = [
    Reservoir("Three Gorges, China", volume_km3=39.3, lift_m=175.0, latitude_deg=30.8),
    Reservoir("All large reservoirs built 1950-2000 (aggregate)",
              volume_km3=10_000.0, lift_m=40.0, latitude_deg=35.0),
]


def delta_lod(reservoir: Reservoir) -> dict:
    """Length-of-day change from filling one reservoir.

    Only distance from the SPIN AXIS matters, which is R*cos(lat), not distance
    from the centre. The water is not created: it is drawn from the global
    ocean, which is spread over all latitudes with mean <cos^2(lat)> = 2/3.
    So the change has two terms:

        dI = m * [ (R+h)^2 * cos^2(lat_res)  -  R^2 * <cos^2(lat)>_ocean ]
                   |___ where it ends up ___|    |__ where it came from __|

    The LATITUDINAL term dominates by ~3 orders of magnitude over the elevation
    lift, because moving mass from a global average latitude to 30.8N changes
    its axis distance far more than raising it 175 m does. Computing only the
    lift term -- an easy mistake -- understates the answer about 1000-fold.
    """
    import math

    mass_kg = reservoir.volume_km3 * 1e9 * RHO_W          # km^3 -> m^3 -> kg
    cos2_res = math.cos(math.radians(reservoir.latitude_deg)) ** 2
    r1 = R_EARTH + reservoir.lift_m

    d_i = mass_kg * (r1 ** 2 * cos2_res - R_EARTH ** 2 * MEAN_COS2_OCEAN)
    # Elevation-only component, kept for the record so the split is visible.
    d_i_lift = mass_kg * (r1 ** 2 - R_EARTH ** 2) * cos2_res

    fractional = d_i / I_EARTH
    d_lod_s = LOD_S * fractional
    d_lod_us = d_lod_s * 1e6

    seasonal = BENCHMARKS_US["seasonal atmospheric angular momentum exchange"]
    return {
        "reservoir": reservoir.name,
        "mass_kg": f"{mass_kg:.3e}",
        "delta_I_kg_m2": f"{d_i:.3e}",
        "delta_I_from_elevation_only_kg_m2": f"{d_i_lift:.3e}",
        "fractional_change_in_I": f"{fractional:.3e}",
        "delta_length_of_day_us": round(d_lod_us, 4),
        "times_smaller_than_seasonal_wobble": round(seasonal / d_lod_us) if d_lod_us else None,
        "affects_river_discharge": False,
        "why_not": ("Length-of-day does not enter the Saint-Venant equations. "
                    "Rotation reaches hydrology only via the Coriolis parameter "
                    "f = 2*omega*sin(lat); a fractional change in omega of "
                    f"{fractional:.1e} changes f by the same fraction, which is "
                    "unmeasurable and dynamically irrelevant at river scale."),
    }


def report() -> dict:
    """Served by /api/explain/earth-rotation."""
    results = [delta_lod(r) for r in KNOWN]
    return {
        "question": ("Do Chinese dams and mountain excavation slow Earth's "
                     "rotation, and does that cause floods in Nepal?"),
        "short_answer": ("They do slow it, by roughly a tenth of a microsecond per "
                         "day for Three Gorges -- the same order as the ~0.06 us "
                         "figure published by Chao at NASA GSFC. That is real. It "
                         "also has no bearing on flooding whatsoever: it is about "
                         "four orders of magnitude below the ordinary seasonal "
                         "swing in day length, and no term linking rotation rate "
                         "to river discharge exists in the governing equations."),
        "calculations": results,
        "benchmarks_microseconds": BENCHMARKS_US,
        "the_real_transboundary_risk": (
            "Upstream barriers -- landslide dams, moraine dams, glacial lakes -- "
            "can form in Tibetan headwaters outside Nepal's gauge network, and "
            "real-time cross-border hydrological data sharing is limited. That "
            "observability gap caused the July 2025 Rasuwa outburst. It is "
            "modelled in hazards/outburst.py, which is where this concern "
            "belongs."
        ),
    }
