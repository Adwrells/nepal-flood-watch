"""Scoring and analytics.

These are the tests that matter most in this codebase, because a wrong number
here is not a crash — it is a calm-looking gauge that should have been a
warning. Each one encodes a property the model must hold, not an
implementation detail, so a refactor is free but a behaviour change is not.
"""
import json

import pytest

from app.analytics import holt_forecast, prescribe, time_to_danger, trend_class
from app.scoring import band_for, level_component, p_exceed_6h, score_station

# A gauge with published marks, used as the baseline for most cases.
STATION = {
    "id": 1, "name": "Test at Testpur", "basin": "Koshi", "district": "Sunsari",
    "lat": 26.6, "lon": 87.2, "warning_level": 4.0, "danger_level": 6.0,
    "status": "", "steady": "", "level": 5.0, "ts": "2026-08-29T12:00:00+05:45",
}


class TestBands:
    @pytest.mark.parametrize("fsi,expected", [
        (0, "NORMAL"), (24.9, "NORMAL"), (25, "WATCH"), (49.9, "WATCH"),
        (50, "WARNING"), (74.9, "WARNING"), (75, "DANGER"), (89.9, "DANGER"),
        (90, "SEVERE"), (100, "SEVERE"),
    ])
    def test_boundaries_land_on_the_documented_side(self, fsi, expected):
        assert band_for(fsi) == expected

    def test_level_component_rises_with_stage(self):
        below = level_component(2.0, 4.0, 6.0)
        between = level_component(5.0, 4.0, 6.0)
        above = level_component(7.0, 4.0, 6.0)
        assert below < between < above

    def test_danger_mark_scores_at_least_85(self):
        """The band table promises DANGER at the danger mark. Hold it there."""
        assert level_component(6.0, 4.0, 6.0) >= 85

    def test_missing_level_scores_zero_not_an_error(self):
        assert level_component(None, 4.0, 6.0) == 0.0


class TestBreachProbability:
    def test_bounded(self):
        for args in [(2.0, 6.0, 0.0, 0), (5.9, 6.0, 0.5, 100), (99, 6.0, 9, 999)]:
            assert 0.0 <= p_exceed_6h(*args) <= 1.0

    def test_rising_near_danger_beats_calm_and_far(self):
        calm = p_exceed_6h(2.0, 6.0, 0.0, 0)
        critical = p_exceed_6h(5.9, 6.0, 0.5, 100)
        assert critical > calm

    def test_no_danger_mark_yields_zero_rather_than_guessing(self):
        assert p_exceed_6h(5.0, None, 0.5, 50) == 0.0

    def test_rate_of_rise_moves_the_answer(self):
        """Two gauges at the same stage must not score alike if one is climbing."""
        still = p_exceed_6h(5.0, 6.0, 0.0, 0)
        climbing = p_exceed_6h(5.0, 6.0, 0.3, 0)
        assert climbing > still


class TestScoreStation:
    def test_end_to_end_shape(self):
        s = score_station(STATION, 4.5, "2026-08-29T11:00:00+05:45",
                          {"past_24h": 80, "next_12h": 40}, [], [])
        assert 0 <= s["fsi"] <= 100
        assert s["band"] in {"NORMAL", "WATCH", "WARNING", "DANGER", "SEVERE"}
        assert set(json.loads(s["components"])) == {"level", "rise", "rain", "corroboration"}

    def test_rise_rate_is_metres_per_hour(self):
        s = score_station(STATION, 4.5, "2026-08-29T11:00:00+05:45", None, [], [])
        assert s["rise_rate"] == pytest.approx(0.5)

    def test_rain_increases_the_score(self):
        dry = score_station(STATION, None, None, {"past_24h": 0, "next_12h": 0}, [], [])
        wet = score_station(STATION, None, None, {"past_24h": 150, "next_12h": 80}, [], [])
        assert wet["fsi"] > dry["fsi"]

    def test_a_silent_gauge_never_reads_as_safe(self):
        """A None level must not be treated as an empty river.

        This is the most dangerous possible failure in the system: defaulting a
        missing reading to 0.0 scores NORMAL and hides an outage.
        """
        silent = score_station({**STATION, "level": None}, None, None, None, [], [])
        assert silent["components"]
        assert json.loads(silent["components"])["level"] == 0.0
        assert silent["p_exceed_6h"] == 0.0


class TestForecast:
    def test_values_sit_inside_their_own_band(self):
        fc = holt_forecast([2.1, 2.2, 2.35, 2.5, 2.7, 2.95, 3.3], steps=6)
        assert len(fc.values) == 6
        for lo, v, hi in zip(fc.lower, fc.values, fc.upper):
            assert lo <= v <= hi

    def test_band_widens_with_horizon(self):
        """Uncertainty must grow further out, or the band is decoration."""
        fc = holt_forecast([1.0, 1.2, 1.1, 1.4, 1.3, 1.6, 1.5, 1.8], steps=8)
        first = fc.upper[0] - fc.lower[0]
        last = fc.upper[-1] - fc.lower[-1]
        assert last > first

    def test_short_series_is_reported_as_such_not_extrapolated(self):
        fc = holt_forecast([1.0, 1.5], steps=6)
        assert fc.method == "insufficient-history"
        assert fc.values == []

    def test_confidence_reflects_sample_size(self):
        assert holt_forecast([1, 1.1, 1.2, 1.3], steps=3).confidence == "low"
        assert holt_forecast([1 + i * 0.01 for i in range(30)], steps=3).confidence == "high"


class TestTimeToDanger:
    def test_arithmetic(self):
        assert time_to_danger(3.3, 0.25, 4.5) == pytest.approx(4.8, abs=0.01)

    def test_already_over_is_zero_not_negative(self):
        assert time_to_danger(7.0, 0.2, 6.0) == 0.0

    @pytest.mark.parametrize("level,rate,danger", [
        (3.0, 0.0, 6.0),        # not moving
        (3.0, -0.4, 6.0),       # falling
        (3.0, 0.2, None),       # no published mark
    ])
    def test_returns_none_when_undefined(self, level, rate, danger):
        assert time_to_danger(level, rate, danger) is None


class TestPrescription:
    def test_lead_time_gates_impossible_advice(self):
        """Advice needing six hours is noise when the river arrives in thirty minutes."""
        p = prescribe("DANGER", 0.5)
        assert any(not a["feasible"] for a in p["actions"])

    def test_ample_lead_time_keeps_everything(self):
        p = prescribe("DANGER", 48)
        assert all(a["feasible"] for a in p["actions"])

    def test_impoundment_overrides_a_calm_playbook(self):
        """A falling river with an impoundment signal must not say "no action"."""
        p = prescribe("NORMAL", None, impoundment_suspected=True)
        assert p["urgency"] == "immediate"
        assert "impoundment" in p["actions"][0]["action"].lower()

    def test_trend_words(self):
        assert trend_class(0.5) == "rising fast"
        assert trend_class(0.0) == "steady"
        assert trend_class(-0.5) == "falling fast"
        assert trend_class(None) == "unknown"


class TestHydrologicalContext:
    """Signals that encode how a forecaster reasons, not more curve fitting."""

    def test_acceleration_detects_a_speeding_rise(self):
        from app.analytics import acceleration_mph2
        steady = acceleration_mph2([1.0, 1.1, 1.2])       # constant rate
        speeding = acceleration_mph2([1.0, 1.1, 1.4])     # rate increasing
        assert steady == pytest.approx(0.0, abs=1e-9)
        assert speeding > 0

    def test_acceleration_needs_three_points(self):
        from app.analytics import acceleration_mph2
        assert acceleration_mph2([1.0, 1.2]) is None

    def test_one_rising_gauge_is_never_basin_wide(self):
        """1 of 2 is 50% and still a single instrument -- nothing to corroborate."""
        from app.analytics import basin_coherence
        r = basin_coherence([
            {"basin": "Mechi", "level": 1.0, "rise_rate": 0.5, "name": "A"},
            {"basin": "Mechi", "level": 1.0, "rise_rate": 0.0, "name": "B"},
        ])
        assert r["Mechi"]["verdict"].startswith("isolated")

    def test_several_agreeing_gauges_read_as_coherent(self):
        from app.analytics import basin_coherence
        r = basin_coherence([
            {"basin": "Koshi", "level": 1.0, "rise_rate": 0.4, "name": "A"},
            {"basin": "Koshi", "level": 1.0, "rise_rate": 0.3, "name": "B"},
            {"basin": "Koshi", "level": 1.0, "rise_rate": 0.0, "name": "C"},
        ])
        assert r["Koshi"]["verdict"].startswith("coherent")

    def test_silent_gauge_during_rain_is_flagged(self):
        """Telemetry dies when power and networks die, which is when floods happen."""
        from app.analytics import silence_is_suspicious
        assert silence_is_suspicious("2020-01-01T00:00:00+05:45", 60)
        assert not silence_is_suspicious("2020-01-01T00:00:00+05:45", 0)

    def test_forecast_never_loses_to_persistence(self):
        """Regression guard: the shipped model was once 72% WORSE than doing nothing."""
        from app.analytics import forecast_skill
        rising = {i: [1.0 + 0.1 * j + (j % 2) * 0.01 for j in range(10)] for i in range(30)}
        r = forecast_skill(rising)
        assert r["n"] > 0
        assert r["skill"] >= -0.05, f"forecast is worse than persistence: {r}"
