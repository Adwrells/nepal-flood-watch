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
from dataclasses import asdict, dataclass

from dateutil import parser as dtparse

# Holt smoothing constants. alpha weights the level, beta the trend.
#
# BETA and PHI are low deliberately, and that is a backtested choice rather than
# taste. Measured over 167 gauges by hiding each one's last reading:
#
#     persistence ("it will be what it is now")   MAE 0.0347 m
#     alpha .45 / beta .25 / phi .90, unshrunk    MAE 0.0597 m   -72% skill
#     this configuration                          MAE 0.0347 m     0% skill
#
# The original settings were materially worse than doing nothing. See
# forecast_skill() below, and tests/test_forecast_skill.py, which fails if a
# future change makes the model worse than persistence again.
ALPHA = 0.45
BETA = 0.05
# Trend damping: an unchecked linear trend extrapolates a flood to infinity.
# phi < 1 pulls the forecast back toward flat, which is what rivers actually do.
PHI = 0.50


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
    trend = _shrink_trend(trend, sigma)

    # Anchor on the last OBSERVATION, not on the smoothed level. The smoothed
    # level lags by construction, and on river stage the most recent reading is
    # the single best estimate of the next one -- starting anywhere else gives
    # away accuracy before the trend term contributes anything.
    anchor = clean[-1]

    horizons, values, lower, upper = [], [], [], []
    damp_sum = 0.0
    for h in range(1, steps + 1):
        damp_sum += PHI ** h
        yhat = anchor + damp_sum * trend
        spread = 1.28 * sigma * math.sqrt(h)          # 80% two-sided
        horizons.append(round(h * hours_per_step, 2))
        values.append(round(yhat, 3))
        lower.append(round(yhat - spread, 3))
        upper.append(round(yhat + spread, 3))

    n = len(clean)
    confidence = "high" if n >= 24 else "moderate" if n >= 8 else "low"
    return Forecast(horizons, values, lower, upper, "holt-damped", confidence,
                    f"{n} readings, residual sigma {sigma:.3f} m")



def _shrink_trend(trend: float, sigma: float) -> float:
    """Keep a trend only to the extent it stands above the noise.

        kept = trend * trend^2 / (trend^2 + sigma^2)

    With five to ten irregularly spaced readings the raw trend is mostly noise,
    and an unshrunk trend term amplifies it into a confident wrong answer. This
    is ordinary shrinkage: when the slope is large relative to residual scatter
    it passes through almost untouched; when it is the same size as the scatter
    it is halved; when it is smaller it effectively vanishes and the forecast
    degrades to persistence, which is the honest answer for a flat river.
    """
    if not sigma or sigma <= 0:
        return trend
    return trend * (trend * trend) / (trend * trend + sigma * sigma)


def forecast_skill(series_by_station: dict) -> dict:
    """Backtest the forecast against persistence. Used by the test suite.

    Hides each gauge's most recent reading, predicts it, and compares the error
    with simply assuming no change. A model that cannot beat "it will be what
    it is now" is worse than useless on a warning system, because it adds
    confident noise to a decision someone may act on.

    Returns MAE for both and a skill score, where positive means better than
    persistence.
    """
    model_err, naive_err = [], []
    for levels in series_by_station.values():
        clean_levels = [v for v in levels if v is not None]
        if len(clean_levels) < 4:
            continue
        train, actual = clean_levels[:-1], clean_levels[-1]
        fc = holt_forecast(train, steps=1)
        if not fc.values:
            continue
        model_err.append(abs(fc.values[0] - actual))
        naive_err.append(abs(train[-1] - actual))

    if not model_err:
        return {"n": 0, "skill": None}
    mae = statistics.mean(model_err)
    naive = statistics.mean(naive_err)
    return {
        "n": len(model_err),
        "model_mae_m": round(mae, 4),
        "persistence_mae_m": round(naive, 4),
        "skill": round(1 - mae / naive, 4) if naive else None,
    }


# ---------------------------------------------------------------------------
# Hydrological context
# ---------------------------------------------------------------------------
def acceleration_mph2(series: list, hours_per_step: float = 1.0) -> float | None:
    """Second difference of stage: is the rise itself speeding up?

    A gauge holding at 5 m and a gauge at 5 m whose rise is accelerating are
    different situations -- the second is a flood wave arriving. Rate alone
    cannot tell them apart, and nothing else in the model looks at it.

    Uses the last three points, which is the shortest window that can express
    curvature, and returns None rather than guessing on a shorter series.
    """
    clean = [v for v in series if v is not None]
    if len(clean) < 3 or hours_per_step <= 0:
        return None
    r1 = (clean[-2] - clean[-3]) / hours_per_step
    r2 = (clean[-1] - clean[-2]) / hours_per_step
    return round((r2 - r1) / hours_per_step, 5)


def basin_coherence(station_scores: list) -> dict:
    """How much of each basin is rising at once.

    One gauge rising can be a stuck float or a boat moored against the sensor.
    Several rising together across a basin cannot be -- independent instruments
    do not fail in the same direction at the same time. This is how a duty
    forecaster reasons, and it is the cheapest defence available against acting
    on instrument error.

    Returns, per basin, the share of reporting gauges that are rising and a
    verdict. `coherent` means the basin is responding as a unit and a single
    gauge's reading is corroborated; `isolated` means one gauge is doing
    something its neighbours are not, which deserves a look at the sensor
    before it deserves an evacuation.
    """
    basins: dict[str, dict] = {}
    for s in station_scores:
        basin = (s.get("basin") or "").strip() or "unassigned"
        b = basins.setdefault(basin, {"reporting": 0, "rising": 0, "stations": []})
        rate = s.get("rise_rate")
        if s.get("level") is None:
            continue
        b["reporting"] += 1
        if rate and rate > 0.02:
            b["rising"] += 1
            b["stations"].append(s.get("name"))

    out = {}
    for basin, b in basins.items():
        if b["reporting"] < 2:
            verdict, share = "insufficient gauges", None
        else:
            share = b["rising"] / b["reporting"]
            # A share alone is not enough: 1 of 2 gauges is 50% and still just
            # one instrument. Corroboration needs at least two agreeing gauges,
            # by definition -- otherwise there is nothing to corroborate with.
            if b["rising"] >= 2 and share >= 0.5:
                verdict = "coherent - basin-wide rise"
            elif b["rising"] >= 2:
                verdict = "partial - several gauges rising"
            elif b["rising"] == 1:
                verdict = "isolated - check the sensor before the river"
            else:
                verdict = "quiet"
        out[basin] = {
            "reporting": b["reporting"],
            "rising": b["rising"],
            "share_rising": round(share, 3) if share is not None else None,
            "verdict": verdict,
            "rising_stations": b["stations"][:8],
        }
    return dict(sorted(out.items(), key=lambda kv: -(kv[1]["share_rising"] or 0)))


def staleness(reading_ts: str | None, now=None) -> dict:
    """How long since this gauge last reported, and whether that is a problem.

    Silence is not neutral. Telemetry fails when mains power and mobile
    networks fail, which is exactly when a catchment is being hammered. A gauge
    that goes quiet during heavy rain has told you something, and treating the
    gap as merely missing data discards it.
    """
    from datetime import datetime, timedelta, timezone
    npt = timezone(timedelta(hours=5, minutes=45))
    now = now or datetime.now(npt)
    if not reading_ts:
        return {"hours": None, "state": "never reported"}
    try:
        seen = dtparse.parse(reading_ts)
    except (ValueError, TypeError):
        return {"hours": None, "state": "unparseable timestamp"}
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=npt)
    hours = (now - seen).total_seconds() / 3600.0
    if hours < 3:
        state = "current"
    elif hours < 12:
        state = "late"
    elif hours < 72:
        state = "stale"
    else:
        state = "offline"
    return {"hours": round(hours, 1), "state": state}


def silence_is_suspicious(reading_ts, rain_past_24h) -> bool:
    """A gauge that fell silent while its catchment was being rained on.

    Deliberately conservative -- this raises attention, not an alarm. The point
    is that the operator sees the gap rather than reading an empty row as calm.
    """
    st = staleness(reading_ts)
    return bool(st["hours"] and st["hours"] >= 6 and (rain_past_24h or 0) >= 25)


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
    # None means there is no second reading to difference against yet -- which
    # is different from "we looked and could not tell", so it gets its own word.
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
#
# Nepali text (_ne) is shown alongside English, the same convention
# emergency.py already uses for contact labels -- inline, not a toggle. This
# is the plain-language public-safety layer; technical/analytic text (score
# drivers, forecast verdicts, model output) stays English-only by design.
PLAYBOOK = {
    "SEVERE": [
        ("Trigger evacuation of the floodplain now",
         "बाढी क्षेत्रबाट अहिले नै उद्धार/स्थानान्तरण सुरु गर्नुहोस्", 0),
        ("Notify DEOC and the ward chair by phone, not email",
         "इमेल होइन, फोनबाट DEOC र वडा अध्यक्षलाई खबर गर्नुहोस्", 0),
        ("Close river crossings and low bridges",
         "खोला वारपार गर्ने बाटो र होचा पुलहरू बन्द गर्नुहोस्", 0),
        # NEOC's toll-free number is 1149, not 1155 -- 1155 is the Nepal
        # Police public helpline (see emergency.py's own correction of the
        # same mix-up). Fixed here rather than left wrong in an escalation
        # instruction.
        ("Escalate to NEOC / toll-free 1149",
         "NEOC मा सूचना गराउनुहोस् / निःशुल्क नम्बर ११४९", 0),
    ],
    "DANGER": [
        ("Issue public warning for settlements within 1 km of the channel",
         "नदी किनारदेखि १ कि.मी. भित्रका बस्तीहरूलाई सार्वजनिक चेतावनी जारी गर्नुहोस्", 0),
        ("Pre-position rescue teams and boats",
         "उद्धार टोली र डुङ्गाहरू पूर्व-तैनाथ गर्नुहोस्", 2),
        ("Move livestock and vehicles to high ground",
         "पशुधन र सवारीसाधन अग्लो ठाउँमा सार्नुहोस्", 3),
        ("Confirm the reading against the DHM bulletin before broadcasting",
         "प्रसारण गर्नुअघि जल तथा मौसम विज्ञान विभाग (DHM) को बुलेटिनसँग रिडिङ भिडाउनुहोस्", 0),
    ],
    "WARNING": [
        ("Alert ward-level focal points to stand by",
         "वडा तहका फोकल पोइन्टहरूलाई तयारी अवस्थामा रहन सचेत गराउनुहोस्", 4),
        ("Verify siren and SMS cascade is functional",
         "साइरन र SMS सूचना प्रणाली चालु रहेको सुनिश्चित गर्नुहोस्", 6),
        ("Review evacuation routes for the exposed wards",
         "जोखिममा परेका वडाहरूको उद्धार मार्ग पुनरावलोकन गर्नुहोस्", 6),
        ("Increase gauge polling to 5-minute intervals",
         "गेज अवलोकनलाई ५ मिनेटको अन्तरालमा बढाउनुहोस्", 0),
    ],
    "WATCH": [
        ("Monitor; no public action yet",
         "अनुगमन गर्नुहोस्; हाल सार्वजनिक कारबाही आवश्यक छैन", 0),
        ("Check upstream rainfall forecast for the next 12 h",
         "आगामी १२ घण्टाको माथिल्लो तटीय वर्षाको पूर्वानुमान जाँच्नुहोस्", 0),
        ("Confirm the gauge is reporting (watch for telemetry gaps)",
         "गेजले डाटा पठाइरहेको पुष्टि गर्नुहोस् (टेलिमेट्री अवरोधमा ध्यान दिनुहोस्)", 0),
    ],
    "NORMAL": [
        ("Routine monitoring", "नियमित अनुगमन", 0),
    ],
}

OUTBURST_ACTIONS = [
    ("Treat as potential upstream impoundment, NOT a normal low-flow period",
     "यसलाई सामान्य न्यून बहावको अवधि नभई सम्भावित माथिल्लो अवरोध (impoundment) को रूपमा लिनुहोस्"),
    ("Do not allow riverbed access, sand mining, or crossing on foot",
     "नदी बगरमा प्रवेश, बालुवा उत्खनन वा पैदल वारपार गर्न नदिनुहोस्"),
    ("Contact upstream ward/DEOC for visual confirmation of the channel",
     "नदी नियालको प्रत्यक्ष अवस्था पुष्टि गर्न माथिल्लो वडा/DEOC लाई सम्पर्क गर्नुहोस्"),
    ("Assume the surge arrives with under an hour of warning",
     "एक घण्टाभन्दा कम चेतावनी समयमै बाढीको लहर आइपुग्न सक्छ भनी मान्नुहोस्"),
]


def prescribe(band, hours_to_danger, impoundment_suspected=False, station_name="") -> dict:
    """Turn a score into a ranked list of actions with the time left to do them.

    Prescriptive means answering "so what do I do", not restating the risk. The
    lead-time gate is the important part: an action needing 6 hours is not
    advice when the river arrives in 40 minutes, it is noise.
    """
    lead = hours_to_danger
    actions = []
    for text, text_ne, needs_hours in PLAYBOOK.get(band, PLAYBOOK["NORMAL"]):
        feasible = lead is None or needs_hours == 0 or lead >= needs_hours
        actions.append({
            "action": text,
            "action_ne": text_ne,
            "requires_hours": needs_hours,
            "feasible": feasible,
            "note": "" if feasible else f"needs ~{needs_hours} h, only {lead} h left",
        })

    if impoundment_suspected:
        # Outburst advice overrides the normal ladder: the gauge is falling, so
        # the standard playbook would say "no action" at exactly the wrong time.
        actions = [{"action": a, "action_ne": a_ne, "requires_hours": 0, "feasible": True,
                    "note": "outburst protocol"} for a, a_ne in OUTBURST_ACTIONS] + actions

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
            "acceleration_m_per_h2": acceleration_mph2(levels),
            "reporting": staleness(station.get("reading_ts") or station.get("ts")),
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
