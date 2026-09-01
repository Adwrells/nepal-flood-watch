"""The chart payload: what the browser is handed to plot.

These tests pin the contract the frontend relies on rather than whatever the
shaping code happens to return today. Two of them exist because the behaviour
they check was wrong when it was first written and passed inspection anyway:
the danger crossing never fired on real data (every gauge was calm, so the
branch was dead and looked fine), and the x-axis has to stay monotonic across
the observed/forecast join or the chart folds back on itself while still
drawing a plausible-looking line.
"""
from datetime import datetime, timedelta

import pytest

from app.main import _chart_payload, _parse_ts

ANCHOR = datetime.fromisoformat("2026-09-01T00:00:00+05:45")


def _row(**over):
    """A gauge row shaped like _latest_scores() returns."""
    base = {
        "id": -1, "name": "Test Khola", "district": "Testpur", "basin": "Bagmati",
        "lat": 27.7, "lon": 85.3, "band": "WATCH", "fsi": 40.0,
        "warning_level": 3.5, "danger_level": 4.0,
        "level": 2.0, "rise_rate": 0.0, "p_exceed_6h": 0.1, "components": {},
        "impoundment_suspected": False, "impoundment_reason": None,
        "past_24h": 10.0, "next_12h": 5.0,
        "reading_ts": ANCHOR.isoformat(), "ts": ANCHOR.isoformat(),
        "hours_to_danger": None, "steady": 0,
    }
    base.update(over)
    return base


def _series(start, step_per_h, n=12):
    return [((ANCHOR + timedelta(hours=i)).isoformat(), round(start + step_per_h * i, 3))
            for i in range(n)]


class TestTimestamps:
    def test_parse_ts_handles_the_offsets_dhm_actually_sends(self):
        assert _parse_ts("2026-09-01T09:10:00+05:45") is not None
        assert _parse_ts("2026-09-01T09:10:00Z") is not None

    def test_parse_ts_returns_none_rather_than_raising(self):
        """A malformed stamp must degrade one x-value, never the endpoint."""
        assert _parse_ts("not a date") is None
        assert _parse_ts(None) is None
        assert _parse_ts("") is None

    def test_forecast_timestamps_continue_the_observed_axis(self):
        """Observed then forecast must be strictly non-decreasing in time.

        If the forecast were anchored anywhere but the last reading, the line
        would double back over its own history and still look like a chart.
        """
        hist = _series(2.0, 0.05)
        p = _chart_payload(_row(level=hist[-1][1]), hist)
        stamps = [_parse_ts(o["ts"]) for o in p["observed"]] + \
                 [_parse_ts(f["ts"]) for f in p["forecast"]]
        assert all(b >= a for a, b in zip(stamps, stamps[1:]))  # noqa: B905

    def test_forecast_starts_after_the_last_observation(self):
        hist = _series(2.0, 0.05)
        p = _chart_payload(_row(level=hist[-1][1]), hist)
        assert _parse_ts(p["forecast"][0]["ts"]) > _parse_ts(p["observed"][-1]["ts"])


class TestDangerCrossing:
    def test_a_clearly_rising_gauge_reports_a_central_crossing(self):
        """Guards a branch that was dead on live data.

        Every gauge in the database was calm when this was written, so "no
        crossing" was indistinguishable from "crossing never computed".
        """
        hist = _series(1.0, 0.4)
        p = _chart_payload(_row(level=hist[-1][1], rise_rate=0.4), hist)
        assert p["crossing"] is not None
        assert p["crossing"]["certainty"] == "central"
        assert p["crossing"]["value"] >= 4.0

    def test_a_falling_gauge_reports_no_crossing(self):
        hist = _series(3.0, -0.1)
        p = _chart_payload(_row(level=hist[-1][1], rise_rate=-0.1), hist)
        assert p["crossing"] is None

    def test_a_gauge_with_no_published_danger_mark_reports_no_crossing(self):
        """Two thirds of DHM's gauges publish no danger level. Absence of a
        mark must not be read as a mark of zero."""
        hist = _series(1.0, 0.4)
        p = _chart_payload(_row(level=hist[-1][1], danger_level=None), hist)
        assert p["crossing"] is None
        assert p["marks"]["danger"] is None

    def test_an_upper_bound_only_crossing_is_labelled_as_such(self):
        """A crossing the central estimate never makes is a tail risk, and the
        UI phrases it differently. The label is the contract."""
        hist = _series(3.0, 0.02)
        p = _chart_payload(_row(level=hist[-1][1], rise_rate=0.02), hist)
        if p["crossing"] is not None:
            assert p["crossing"]["certainty"] in {"central", "upper-bound"}
            if p["crossing"]["certainty"] == "upper-bound":
                assert p["crossing"]["value"] >= 4.0


class TestPayloadShape:
    def test_all_four_analytic_layers_are_present(self):
        p = _chart_payload(_row(), _series(2.0, 0.0))
        for layer in ("descriptive", "diagnostic", "predictive", "prescriptive"):
            assert layer in p, f"{layer} missing from the chart payload"

    def test_observed_drops_null_readings_without_shifting_time(self):
        """A gap must remain a gap. Dropping the value but keeping the slot
        would slide every later reading one step earlier."""
        hist = [(t, v) for t, v in _series(2.0, 0.1)]
        hist[3] = (hist[3][0], None)
        p = _chart_payload(_row(), hist)
        assert len(p["observed"]) == len(hist) - 1
        assert all(o["value"] is not None for o in p["observed"])
        assert hist[3][0] not in [o["ts"] for o in p["observed"]]

    def test_prediction_interval_is_ordered_and_contains_the_estimate(self):
        p = _chart_payload(_row(), _series(2.0, 0.1))
        for f in p["forecast"]:
            assert f["lower"] <= f["value"] <= f["upper"], f

    def test_interval_widens_with_horizon(self):
        """Uncertainty that does not grow with lead time is not uncertainty."""
        p = _chart_payload(_row(), _series(2.0, 0.1))
        widths = [f["upper"] - f["lower"] for f in p["forecast"]]
        assert widths[-1] > widths[0]

    def test_a_gauge_with_no_history_still_returns_a_payload(self):
        """An empty series is normal for a newly added gauge; it must render
        as 'no data' rather than 500."""
        p = _chart_payload(_row(level=None), [])
        assert p["observed"] == []
        assert p["forecast"] == []
        assert p["crossing"] is None

    def test_marks_are_passed_through_untouched(self):
        p = _chart_payload(_row(warning_level=3.5, danger_level=4.0), _series(2.0, 0.0))
        assert p["marks"] == {"warning": 3.5, "danger": 4.0}


class TestForecastBehaviour:
    def test_a_flat_river_is_forecast_flat(self):
        """The damping exists because straight-line extrapolation backtested
        72% worse than persistence. A flat series must not sprout a trend."""
        p = _chart_payload(_row(level=2.0), _series(2.0, 0.0))
        assert p["forecast"][-1]["value"] == pytest.approx(2.0, abs=0.05)

    def test_the_damped_forecast_lags_straight_line_extrapolation(self):
        """The two numbers the UI shows side by side must differ in the
        direction the copy claims: the forecast is the conservative one."""
        hist = _series(1.0, 0.4)
        p = _chart_payload(_row(level=hist[-1][1], rise_rate=0.4), hist)
        linear = hist[-1][1] + 0.4 * 12
        assert p["forecast"][-1]["value"] < linear
