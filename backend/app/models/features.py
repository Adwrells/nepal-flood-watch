"""Feature engineering for the learned forecasters.

The target is the NEXT stage reading; the features are what a hydrologist would
actually look at, kept deliberately few. With a few hundred thousand rows and
tree models, adding weak features costs generalisation and buys nothing —
and every feature here has to be computable at prediction time from data the
system already holds.

Nothing here uses the future. `rain_next_12h` is a forecast product that exists
at prediction time; it is not the answer leaking backwards.
"""

LAGS = 3          # last three readings; more adds little on hourly stage data
MIN_HISTORY = LAGS + 1

FEATURE_NAMES = [
    "level",              # current stage -- persistence baked in as a feature
    "lag1", "lag2",       # the two before it
    "delta1",             # most recent change
    "delta2",             # the one before, so the tree can see curvature
    "accel",              # delta1 - delta2
    "mean3", "std3",      # local level and local noise
    "rain_past_24h",
    "rain_next_12h",
    "headroom",           # danger mark minus current stage; None -> -1 sentinel
]


def build_row(window: list, rain_past: float = 0.0, rain_next: float = 0.0,
              danger: float | None = None) -> list:
    """One feature vector from the last LAGS+1 readings, oldest first."""
    if len(window) < MIN_HISTORY:
        raise ValueError(f"need {MIN_HISTORY} readings, got {len(window)}")
    w = window[-MIN_HISTORY:]
    level, lag1, lag2 = w[-1], w[-2], w[-3]
    delta1 = level - lag1
    delta2 = lag1 - lag2
    last3 = w[-3:]
    mean3 = sum(last3) / 3.0
    var3 = sum((x - mean3) ** 2 for x in last3) / 3.0
    return [
        level, lag1, lag2,
        delta1, delta2, delta1 - delta2,
        mean3, var3 ** 0.5,
        rain_past or 0.0,
        rain_next or 0.0,
        # -1 rather than 0: 0 would read as "at the danger mark", the opposite
        # of "no mark published". Trees split on it cleanly either way.
        (danger - level) if danger else -1.0,
    ]


def build_dataset(series_by_station: dict, rain_by_station: dict | None = None,
                  danger_by_station: dict | None = None) -> tuple[list, list]:
    """Roll every gauge's history into (X, y) pairs.

    One example per position where LAGS+1 readings exist and a next reading is
    known. Grouped by station on purpose -- the evaluation harness splits by
    TIME, not at random, because shuffling a time series lets the model see the
    future and produces a score that means nothing.
    """
    rain_by_station = rain_by_station or {}
    danger_by_station = danger_by_station or {}
    X, y = [], []
    for sid, series in series_by_station.items():
        clean = [v for v in series if v is not None]
        if len(clean) < MIN_HISTORY + 1:
            continue
        rain = rain_by_station.get(sid, {})
        danger = danger_by_station.get(sid)
        for i in range(MIN_HISTORY, len(clean)):
            X.append(build_row(clean[i - MIN_HISTORY:i],
                               rain.get("past_24h", 0.0),
                               rain.get("next_12h", 0.0),
                               danger))
            y.append(clean[i])
    return X, y
