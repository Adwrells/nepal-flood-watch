"""The pluggable forecasters and the gate that governs them.

The gate is the point of this package: a model may only be enabled if it beats
persistence. These tests defend that rule, not any particular model.
"""
import pytest

from app import models


class TestRegistry:
    def test_every_model_implements_the_interface(self):
        for name, cls in models.REGISTRY.items():
            m = cls()
            assert m.name == name
            assert hasattr(m, "available") and hasattr(m, "fit") and hasattr(m, "predict")

    def test_baselines_are_always_available(self):
        """The console must forecast with no optional dependency installed."""
        for name in ("persistence", "damped-holt"):
            ok, _ = models.REGISTRY[name]().available()
            assert ok

    def test_unknown_name_falls_back_rather_than_raising(self):
        assert models.get("no-such-model").name == models.DEFAULT

    def test_learned_models_report_missing_library_not_crash(self):
        """Absence of scikit-learn is a message, never an exception."""
        ok, why = models.REGISTRY["gradient-boosting"]().available()
        assert isinstance(ok, bool) and isinstance(why, str) and why


class TestPersistence:
    def test_predicts_the_last_value(self):
        fc = models.REGISTRY["persistence"]().predict([1.0, 2.0, 3.0, 3.5], steps=3)
        assert fc.values == [3.5, 3.5, 3.5]

    def test_band_widens_with_horizon(self):
        fc = models.REGISTRY["persistence"]().predict([1.0, 1.4, 1.1, 1.6, 1.3], steps=5)
        assert (fc.upper[-1] - fc.lower[-1]) > (fc.upper[0] - fc.lower[0])

    def test_empty_history_refuses_rather_than_guesses(self):
        fc = models.REGISTRY["persistence"]().predict([], steps=3)
        assert fc.values == []


class TestGate:
    def test_untrained_learned_model_refuses(self):
        """Too little data must produce a refusal, not a confident number."""
        m = models.REGISTRY["gradient-boosting"]()
        ok, _ = m.available()
        if not ok:
            pytest.skip("scikit-learn not installed")
        m.fit({i: [1.0 + 0.1 * j for j in range(6)] for i in range(3)})
        assert m.predict([1.0, 1.1, 1.2, 1.3], steps=2).values == []

    def test_bakeoff_ranks_and_recommends(self):
        series = {i: [1.0 + 0.05 * j for j in range(12)] for i in range(25)}
        r = models.evaluate_all(series)
        assert "persistence" in r["models"]
        assert r["best_by_mae"]
        assert r["recommendation"]
        assert "beats persistence" in r["gate"] or "beats" in r["gate"]

    def test_a_worse_model_is_never_recommended(self):
        """The whole reason this package exists."""
        series = {i: [1.0, 1.4, 0.9, 1.6, 1.1, 1.5, 1.0, 1.45] for i in range(20)}
        r = models.evaluate_all(series)
        for name, row in r["models"].items():
            if row.get("usable") and row.get("skill") is not None and row["skill"] < 0:
                assert not row["beats_persistence"], f"{name} claims to beat persistence while losing"


class TestFeatures:
    def test_row_width_matches_the_declared_names(self):
        from app.models.features import FEATURE_NAMES, build_row
        row = build_row([1.0, 1.1, 1.2, 1.3], 20.0, 10.0, 5.0)
        assert len(row) == len(FEATURE_NAMES)

    def test_missing_danger_mark_uses_a_sentinel_not_zero(self):
        """Zero would read as 'at the danger mark' -- the opposite of unknown."""
        from app.models.features import FEATURE_NAMES, build_row
        row = build_row([1.0, 1.1, 1.2, 1.3], 0, 0, None)
        assert row[FEATURE_NAMES.index("headroom")] == -1.0

    def test_short_window_is_rejected(self):
        from app.models.features import build_row
        with pytest.raises(ValueError):
            build_row([1.0, 1.1])

    def test_dataset_never_uses_the_future(self):
        from app.models.features import MIN_HISTORY, build_dataset
        X, y = build_dataset({1: [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]})
        assert len(X) == len(y) == 6 - MIN_HISTORY
        # The target must be the value AFTER the window, never inside it.
        assert y[0] == 1.4
