"""The forecasters themselves: two baselines and three learned models.

scikit-learn is an OPTIONAL dependency (`pip install -r requirements-ml.txt`).
The console runs fully without it — the learned models report themselves
unavailable and the baseline stays active. That keeps the container at 244 MB
for anyone who does not want a 100 MB numeric stack for a marginal gain they
have not yet measured.
"""
import math
import statistics

from . import features
from .base import Forecast, insufficient

try:                                       # optional
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge
    SKLEARN = True
    SKLEARN_ERROR = ""
except ImportError as exc:                 # noqa: BLE001
    SKLEARN = False
    SKLEARN_ERROR = f"scikit-learn not installed ({exc.name}); pip install -r requirements-ml.txt"


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
class Persistence:
    """The level will be what it is now.

    Not a straw man. On river stage at short horizons this is genuinely hard to
    beat, and it is the bar every other model here has to clear.
    """

    name = "persistence"
    needs_fit = False
    min_examples = 0

    def available(self):
        return True, "always"

    def fit(self, series_by_station):
        return self

    def predict(self, series, steps=12, hours_per_step=1.0, context=None):
        clean = [v for v in series if v is not None]
        if not clean:
            return insufficient("no readings", self.name)
        last = clean[-1]
        # Band from observed step-to-step scatter, widened by sqrt(h).
        deltas = [abs(b - a) for a, b in zip(clean, clean[1:])]
        sigma = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
        horizons, values, lo, hi = [], [], [], []
        for h in range(1, steps + 1):
            spread = 1.28 * sigma * math.sqrt(h)
            horizons.append(round(h * hours_per_step, 2))
            values.append(round(last, 3))
            lo.append(round(last - spread, 3))
            hi.append(round(last + spread, 3))
        return Forecast(horizons, values, lo, hi, self.name,
                        "high" if len(clean) >= 8 else "low",
                        f"{len(clean)} readings, no trend term")


class DampedHolt:
    """The shipped model: persistence-anchored, shrunk-trend exponential smoothing.

    Delegates to analytics.holt_forecast so there is exactly one implementation
    and the two can never drift apart.
    """

    name = "damped-holt"
    needs_fit = False
    min_examples = 0

    def available(self):
        return True, "always"

    def fit(self, series_by_station):
        return self

    def predict(self, series, steps=12, hours_per_step=1.0, context=None):
        from ..analytics import holt_forecast
        fc = holt_forecast(series, steps, hours_per_step)
        return Forecast(fc.horizon_hours, fc.values, fc.lower, fc.upper,
                        self.name, fc.confidence, fc.note)


# ---------------------------------------------------------------------------
# Learned models
# ---------------------------------------------------------------------------
class _Learned:
    """Shared plumbing for the scikit-learn estimators.

    Predicts one step and iterates for longer horizons, feeding each prediction
    back as input. That compounds error with distance, which is honest: the
    band widens accordingly, and a tree cannot extrapolate beyond the range it
    was trained on anyway.
    """

    name = "learned"
    needs_fit = True
    min_examples = 10_000
    _make = None

    def __init__(self):
        self.model = None
        self.residual_sigma = 0.0
        self.trained_on = 0

    def available(self):
        if not SKLEARN:
            return False, SKLEARN_ERROR
        return True, "scikit-learn present"

    def fit(self, series_by_station, rain=None, danger=None):
        ok, why = self.available()
        if not ok:
            return self
        X, y = features.build_dataset(series_by_station, rain, danger)
        if len(X) < self.min_examples:
            self.trained_on = len(X)
            self.model = None
            return self
        # Chronological split: the last 20% is held out. Shuffling a time
        # series lets the model see the future and produces a meaningless score.
        cut = int(len(X) * 0.8)
        self.model = self._make()
        self.model.fit(X[:cut], y[:cut])
        preds = self.model.predict(X[cut:])
        resid = [a - b for a, b in zip(y[cut:], preds)]
        self.residual_sigma = statistics.pstdev(resid) if len(resid) > 1 else 0.0
        self.trained_on = len(X)
        return self

    def predict(self, series, steps=12, hours_per_step=1.0, context=None):
        if self.model is None:
            ok, why = self.available()
            return insufficient(
                why if not ok else
                f"not trained: {self.trained_on} examples, needs {self.min_examples:,}",
                self.name)

        clean = [v for v in series if v is not None]
        if len(clean) < features.MIN_HISTORY:
            return insufficient(
                f"need {features.MIN_HISTORY} readings, have {len(clean)}", self.name)

        ctx = context or {}
        window = list(clean)
        horizons, values, lo, hi = [], [], [], []
        for h in range(1, steps + 1):
            row = features.build_row(window, ctx.get("rain_past_24h", 0.0),
                                     ctx.get("rain_next_12h", 0.0), ctx.get("danger_level"))
            yhat = float(self.model.predict([row])[0])
            window.append(yhat)                       # recursive multi-step
            spread = 1.28 * self.residual_sigma * math.sqrt(h)
            horizons.append(round(h * hours_per_step, 2))
            values.append(round(yhat, 3))
            lo.append(round(yhat - spread, 3))
            hi.append(round(yhat + spread, 3))

        return Forecast(horizons, values, lo, hi, self.name,
                        "high" if self.trained_on > 50_000 else "moderate",
                        f"trained on {self.trained_on:,} examples, "
                        f"holdout sigma {self.residual_sigma:.3f} m",
                        features.FEATURE_NAMES)


class GradientBoosting(_Learned):
    """Usually the strongest tabular regressor, and the one to try first.

    Shallow trees and a low learning rate on purpose: stage series are smooth
    and highly autocorrelated, so a deep forest memorises gauge identity rather
    than learning river behaviour.
    """

    name = "gradient-boosting"
    _make = staticmethod(lambda: GradientBoostingRegressor(
        n_estimators=300, learning_rate=0.05, max_depth=3,
        subsample=0.8, random_state=0))


class RandomForest(_Learned):
    """Less prone to overfit than boosting, and it gives honest feature importance."""

    name = "random-forest"
    _make = staticmethod(lambda: RandomForestRegressor(
        n_estimators=200, max_depth=8, min_samples_leaf=5,
        n_jobs=-1, random_state=0))


class RidgeLinear(_Learned):
    """A linear control.

    Worth keeping: if ridge matches the trees, the relationship is linear and
    the trees are ceremony. That is a real possibility on stage data, and this
    is how you would find out.
    """

    name = "ridge"
    min_examples = 1_000        # linear models need far less data
    _make = staticmethod(lambda: Ridge(alpha=1.0))
