"""Analytics layer: descriptive -> diagnostic -> predictive -> prescriptive.

Deliberately dependency-free. Holt's linear method and empirical percentiles do
the job on 15-minute stage data, they run in microseconds for 400 gauges, and
they degrade honestly on short history. Fitting a neural net to a gauge with
nine readings would look more impressive and tell you less.

  descriptive   what the river is doing now          -> scoring.py FSI
  diagnostic    which component is driving it        -> FSI component split
  predictive    where it will be in 1-12 h           -> holt_forecast, time_to_danger
  prescriptive  what someone should therefore do     -> prescribe()
"""
import math
import statistics
from dataclasses import dataclass, asdict

# Holt smoothing constants. alpha weights the level, beta the trend. These are
# tuned for noisy 15-min stage data: responsive enough to catch a real rise,
# damped enough to ignore a single bad telemetry packet.
ALPHA = 0.45
BETA = 0.25
# Trend damping: an unchecked linear trend extrapolates a flood to infinity.
# phi < 1 pulls the forecast back toward flat, which is what rivers actually do.
PHI = 0.90


# ---------------------------------------------------------------------------
# Predictive
# ---------------------------------------------------------------------------
@dataclass
class Forecast:
    horizon_hours: list
    values: list
    lower: list                # 80% band
    upper: list
    method: str
    confidence: str            # low | moderate | high
    note: str


def holt_forecast(series: list[float], steps: int = 12, hours_per_step: float = 1.0) -> Forecast:
    """Damped Holt linear trend forecast with an empirical prediction band.

        level_t = a*y_t + (1-a)*(level_{t-1} + phi*trend_{t-1})
        trend_t = b*(level_t - level_{t-1}) + (1-b)*phi*trend_{t-1}
        yhat_{t+h} = level_t + (phi + phi^2 + ... + phi^h) * trend_t

    The band comes from in-sample one-step residuals widened by sqrt(h), the
    standard random-walk error growth. It is an honest band, not a decorative
    one: if the gauge has been erratic, the band is wide and says so.
    """
    clean = [v for v in series if v is not None]
    if len(clean) < 3:
        last = clean[-1] if clean else 0.0
        return Forecast([], [], [], [], "insufficient-history", "low",
                        f"need 3+ readings, have {len(clean)}")

    level, trend = clean[0], clean[1] - clean[0]
    residuals = []
    for y in clean[1:]:
        prev_level = level
        forecast_1 = level + PHI * trend
        residuals.append(y - forecast_1)
        level = ALPHA * y + (1 - ALPHA) * forecast_1
        trend = BETA * (level - prev_level) + (1 - BETA) * PHI * trend

    sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0

    horizons, values, lower, upper = [], [], [], []
    damp_sum = 0.0
    for h in range(1, steps + 1):
        damp_sum += PHI ** h
        yhat = level + damp_sum * trend
        spread = 1.28 * sigma * math.sqrt(h)          # 80% two-sided
        horizons.append(round(h * hours_per_step, 2))
        values.append(round(yhat, 3))
        lower.append(round(yhat - spread, 3))
        upper.append(round(yhat + spread, 3))

    n = len(clean)
    confidence = "high" if n >= 24 else "moderate" if n >= 8 else "low"
    return Forecast(horizons, values, lower, upper, "holt-damped", confidence,
                    f"{n} readings, residual sigma {sigma:.3f} m")


def time_to_danger(level, rise_rate_mh, danger_level) -> float | None:
    """Hours until the gauge reaches its danger mark at the current rise rate.

    Returns None when it is not rising, has no danger mark, or is already over.
    This is the number an evacuation decision is actually made on -- more
    actionable than a probability, because it converts directly into lead time.
    """
    if not danger_level or level is None:
        return None
    if level >= danger_level:
        return 0.0
    if not rise_rate_mh or rise_rate_mh <= 0:
        return None
    return round((danger_level - level) / rise_rate_mh, 2)


def exceedance_percentile(level, history: list[float]) -> float | None:
    """Where the current stage sits in this gauge's own observed distribution.

    Percentile beats absolute metres for cross-basin comparison: 4 m is routine
    on the Koshi and extraordinary on a hill stream. Needs 20+ readings before
    it means anything.
    """
    clean = [v for v in history if v is not None]
    if level is None or len(clean) < 20:
        return None
    below = sum(1 for v in clean if v < level)
    return round(100.0 * below / len(clean), 1)


def trend_class(rise_rate_mh) -> str:
    if rise_rate_mh is None:
        return "unknown"
    if rise_rate_mh > 0.15:
        return "rising fast"
    if rise_rate_mh > 0.02:
        return "rising"
    if rise_rate_mh < -0.15:
        return "falling fast"
    if rise_rate_mh < -0.02:
        return "falling"
    return "steady"


# ---------------------------------------------------------------------------
# Prescriptive
# ---------------------------------------------------------------------------
# Keyed on band. Each action carries a lead-time gate so the UI can suppress
# advice that no longer has time to be executed.
PLAYBOOK = {
    "SEVERE": [
        ("Trigger evacuation of the floodplain now", 0),
        ("Notify DEOC and the ward chair by phone, not email", 0),
        ("Close river crossings and low bridges", 0),
        ("Escalate to NEOC / toll-free 1155", 0),
    ],
    "DANGER": [
        ("Issue public warning for settlements within 1 km of the channel", 0),
        ("Pre-position rescue teams and boats", 2),
        ("Move livestock and vehicles to high ground", 3),
        ("Confirm the reading against the DHM bulletin before broadcasting", 0),
    ],
    "WARNING": [
        ("Alert ward-level focal points to stand by", 4),
        ("Verify siren and SMS cascade is functional", 6),
        ("Review evacuation routes for the exposed wards", 6),
        ("Increase gauge polling to 5-minute intervals", 0),
    ],
    "WATCH": [
        ("Monitor; no public action yet", 0),
        ("Check upstream rainfall forecast for the next 12 h", 0),
        ("Confirm the gauge is reporting (watch for telemetry gaps)", 0),
    ],
    "NORMAL": [
        ("Routine monitoring", 0),
    ],
}

OUTBURST_ACTIONS = [
    "Treat as potential upstream impoundment, NOT a normal low-flow period",
    "Do not allow riverbed access, sand mining, or crossing on foot",
    "Contact upstream ward/DEOC for visual confirmation of the channel",
    "Assume the surge arrives with under an hour of warning",
]


def prescribe(band, hours_to_danger, impoundment_suspected=False, station_name="") -> dict:
    """Turn a score into a ranked list of actions with the time left to do them.

    Prescriptive means answering "so what do I do", not restating the risk. The
    lead-time gate is the important part: an action needing 6 hours is not
    advice when the river arrives in 40 minutes, it is noise.
    """
    lead = hours_to_danger
    actions = []
    for text, needs_hours in PLAYBOOK.get(band, PLAYBOOK["NORMAL"]):
        feasible = lead is None or needs_hours == 0 or lead >= needs_hours
        actions.append({
            "action": text,
            "requires_hours": needs_hours,
            "feasible": feasible,
            "note": "" if feasible else f"needs ~{needs_hours} h, only {lead} h left",
        })

    if impoundment_suspected:
        # Outburst advice overrides the normal ladder: the gauge is falling, so
        # the standard playbook would say "no action" at exactly the wrong time.
        actions = [{"action": a, "requires_hours": 0, "feasible": True,
                    "note": "outburst protocol"} for a in OUTBURST_ACTIONS] + actions

    urgency = ("immediate" if band in ("SEVERE", "DANGER") or impoundment_suspected
               else "elevated" if band == "WARNING" else "routine")

    return {
        "station": station_name,
        "band": band,
        "hours_to_danger": lead,
        "urgency": urgency,
        "impoundment_override": impoundment_suspected,
        "actions": actions,
    }


def analyse_station(station, score, level_history) -> dict:
    """Bundle every analytic layer for one gauge, ready to serve as JSON."""
    levels = [lv for _, lv in level_history]
    fc = holt_forecast(levels, steps=12)
    ttd = time_to_danger(station.get("level"), score.get("rise_rate"),
                         station.get("danger_level"))
    return {
        "station_id": station["id"],
        "name": station.get("name"),
        "descriptive": {
            "level_m": station.get("level"),
            "fsi": score.get("fsi"),
            "band": score.get("band"),
            "trend": trend_class(score.get("rise_rate")),
            "percentile_vs_own_history": exceedance_percentile(station.get("level"), levels),
        },
        "diagnostic": score.get("components"),
        "predictive": {
            "forecast": asdict(fc),
            "hours_to_danger": ttd,
            "p_exceed_6h": score.get("p_exceed_6h"),
        },
        "prescriptive": prescribe(score.get("band", "NORMAL"), ttd,
                                  score.get("impoundment_suspected", False),
                                  station.get("name", "")),
    }
