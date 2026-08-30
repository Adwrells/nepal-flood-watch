"""Model registry and the bake-off that decides which one is allowed to run.

    A model may only be enabled if it beats persistence on the hold-out
    backtest. Reputation does not qualify it.

The damped-Holt model shipped here measured 72% WORSE than assuming no change
and looked entirely reasonable in review. Gradient boosting can fail the same
way with more ceremony, so every model faces the same test on the same data.
"""
import statistics

from .base import Forecast, Forecaster, insufficient
from .estimators import (
    SKLEARN,
    SKLEARN_ERROR,
    DampedHolt,
    GradientBoosting,
    Persistence,
    RandomForest,
    RidgeLinear,
)
from .features import FEATURE_NAMES, MIN_HISTORY

REGISTRY = {
    c.name: c for c in
    (Persistence, DampedHolt, GradientBoosting, RandomForest, RidgeLinear)
}
BASELINE = "persistence"
DEFAULT = "damped-holt"

__all__ = ["REGISTRY", "BASELINE", "DEFAULT", "Forecast", "Forecaster",
           "insufficient", "evaluate_all", "get", "FEATURE_NAMES", "MIN_HISTORY",
           "SKLEARN", "SKLEARN_ERROR"]


def get(name: str | None = None):
    """Instantiate a forecaster by name, falling back to the default."""
    cls = REGISTRY.get(name or DEFAULT, REGISTRY[DEFAULT])
    return cls()


def _backtest(model, series_by_station, rain=None, danger=None) -> dict:
    """Hold out each gauge's newest reading, predict it, measure the error."""
    errs, naive = [], []
    for sid, series in series_by_station.items():
        clean = [v for v in series if v is not None]
        if len(clean) < MIN_HISTORY + 1:
            continue
        train, actual = clean[:-1], clean[-1]
        ctx = {**(rain or {}).get(sid, {}), "danger_level": (danger or {}).get(sid)}
        fc = model.predict(train, steps=1, context=ctx)
        if not fc.values:
            continue
        errs.append(abs(fc.values[0] - actual))
        naive.append(abs(train[-1] - actual))

    if not errs:
        # Say what is actually missing. "No usable gauges" is true but useless;
        # an untrained model needs data, which is a different problem from a
        # gauge set with no history.
        probe = model.predict([1.0, 1.1, 1.2, 1.3, 1.4], steps=1)
        reason = probe.note or "produced no forecast"
        return {"n": 0, "mae_m": None, "skill": None,
                "status": reason, "usable": False}

    mae = statistics.mean(errs)
    base = statistics.mean(naive)
    skill = (1 - mae / base) if base else None
    return {
        "n": len(errs),
        "mae_m": round(mae, 4),
        "persistence_mae_m": round(base, 4),
        "skill": round(skill, 4) if skill is not None else None,
        "beats_persistence": bool(skill is not None and skill > 0),
        "usable": True,
    }


def evaluate_all(series_by_station, rain=None, danger=None) -> dict:
    """Train and score every registered model on the same data. The bake-off.

    Returns each model's availability, its skill against persistence, and a
    recommendation. Nothing is switched automatically -- the number is
    published and a human decides, because "the model got better this week" is
    the kind of change that should be noticed.
    """
    results = {}
    for name, cls in REGISTRY.items():
        model = cls()
        ok, why = model.available()
        if not ok:
            results[name] = {"available": False, "reason": why}
            continue
        if getattr(model, "needs_fit", False):
            model.fit(series_by_station, rain, danger)
        row = _backtest(model, series_by_station, rain, danger)
        row["available"] = True
        row["trained_on"] = getattr(model, "trained_on", None)
        results[name] = row

    ranked = sorted(
        ((n, r) for n, r in results.items() if r.get("usable")),
        key=lambda kv: kv[1]["mae_m"],
    )
    best = ranked[0][0] if ranked else None

    return {
        "models": results,
        "baseline": BASELINE,
        "active": DEFAULT,
        "best_by_mae": best,
        "recommendation": _recommend(results, best),
        "gate": ("A model is only worth enabling if skill > 0, i.e. it beats "
                 "assuming no change. The shipped model once measured -72%."),
    }


def _recommend(results, best) -> str:
    if not best:
        return "No model produced a usable forecast on this data."
    r = results[best]
    if best == BASELINE:
        return ("Persistence is currently the most accurate. That is a normal "
                "result in quiet weather and not a failure -- there is no trend "
                "to find. Keep the current model and re-run when rivers are moving.")
    if r.get("beats_persistence"):
        return (f"{best} beats persistence by {r['skill']:.1%} over {r['n']} gauges. "
                f"Worth switching: set forecast_model in config.")
    return (f"{best} is the most accurate model tried but does not beat "
            f"persistence (skill {r['skill']:+.1%}). Do not switch on this evidence.")
