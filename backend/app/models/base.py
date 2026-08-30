"""Pluggable forecasters.

Every model in this package implements the same tiny interface and is judged by
the same backtest, so swapping one in is a configuration change rather than a
rewrite — and so no model can be adopted on the strength of its reputation.

    THE GATE: a model may only be enabled if it beats persistence on the
    hold-out backtest. Nothing else qualifies it.

That rule exists because the damped-Holt model shipped here was measured at 72%
WORSE than assuming no change, and looked entirely reasonable in code review. A
gradient-boosted tree can fail the same way with more ceremony.
"""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class Forecast:
    """What every forecaster returns. Matches analytics.Forecast field-for-field."""
    horizon_hours: list
    values: list
    lower: list
    upper: list
    method: str
    confidence: str            # low | moderate | high
    note: str
    features_used: list = field(default_factory=list)


@runtime_checkable
class Forecaster(Protocol):
    """The whole interface. Deliberately small.

    `name`        stable identifier used in config and in the API
    `needs_fit`   True for learned models; the harness trains before evaluating
    `min_examples` how much supervised data before this model is worth trying
    """

    name: str
    needs_fit: bool
    min_examples: int

    def available(self) -> tuple[bool, str]:
        """(usable_here, reason). Reports a missing library rather than raising."""
        ...

    def fit(self, series_by_station: dict) -> "Forecaster":
        """Train. A no-op for models that carry no parameters."""
        ...

    def predict(self, series: list, steps: int = 12,
                hours_per_step: float = 1.0, context: dict | None = None) -> Forecast:
        """Forecast `steps` ahead from one gauge's level history, oldest first."""
        ...


def insufficient(reason: str, method: str) -> Forecast:
    """A refusal, not a guess.

    Returning an empty forecast that says why is safer than extrapolating from
    two points: the UI renders "not enough history" instead of a confident line
    nobody should act on.
    """
    return Forecast([], [], [], [], method, "low", reason)
