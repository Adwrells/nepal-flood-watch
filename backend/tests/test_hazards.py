"""Outburst physics, cleaning, and the security guards.

The outburst cases are pinned to published events (Tangjiashan) rather than to
whatever the code currently returns, so the tests stay meaningful if someone
retunes a constant. The cleaning cases encode the rule that matters most here:
standardise aggressively, never invent.
"""
import pytest

from app import clean, reference_data, tiles
from app.hazards import earth_rotation, glof_watch, outburst


class TestBarrierStability:
    def test_tangjiashan_is_unstable(self):
        """Wenchuan 2008: A=3550 km2, H=82 m, V=20.4e6 m3.

        This barrier did require an emergency spillway, so any model that calls
        it stable is wrong regardless of what its constants say.
        """
        r = outburst.stability_index(82, 20.4e6, 3550)
        assert r["verdict"].startswith("unstable")
        assert r["dbi"] == pytest.approx(4.15, abs=0.05)

    def test_small_barrier_on_a_small_catchment_holds(self):
        assert outburst.stability_index(15, 8e6, 120)["verdict"] == "likely stable"

    def test_units_are_metres_cubed_not_millions(self):
        """The DBI formula wants 10^6 m3; the function must convert, not assume."""
        assert outburst.stability_index(82, 20.4e6, 3550)["dbi"] > 3
        assert outburst.stability_index(82, 20.4, 3550)["dbi"] > 6   # absurd input, absurd output

    def test_missing_inputs_do_not_crash(self):
        assert outburst.stability_index(0, 0, 0)["dbi"] is None


class TestBreachHydrograph:
    def test_two_relations_agree_on_magnitude(self):
        s = outburst.reference_scenario()
        lo, hi = s["peak_discharge_cumecs"]["envelope"]
        assert lo > 0
        assert hi / lo < 100, "the two relations disagree implausibly"

    def test_celerity_is_a_plausible_mountain_river(self):
        assert 3 < outburst.manning_celerity() < 20

    def test_arrival_time_grows_with_distance(self):
        s = outburst.reference_scenario()
        etas = [d["eta_minutes"] for d in s["downstream"]]
        assert etas == sorted(etas)

    def test_peak_attenuates_downstream(self):
        s = outburst.reference_scenario()
        peaks = [d["peak_high_cumecs"] for d in s["downstream"]]
        assert peaks == sorted(peaks, reverse=True)


class TestImpoundmentDetector:
    """The precursor. Over-firing here trains operators to ignore it, which is
    its own failure mode, so both directions are tested."""

    FALLING = [("t1", 5.0), ("t2", 5.1), ("t3", 5.0), ("t4", 3.9)]

    def test_fires_on_a_real_drop_during_rain(self):
        st = {"id": 1, "name": "Trishuli at Betrawati", "basin": "Narayani"}
        sig = outburst.detect_impoundment(st, self.FALLING, 40.0, 20.0)
        assert sig.suspected

    def test_ignores_a_drop_with_no_rain(self):
        st = {"id": 1, "name": "Trishuli at Betrawati", "basin": "Narayani"}
        assert not outburst.detect_impoundment(st, self.FALLING, 0.0, 0.0).suspected

    def test_shallow_gauge_cannot_fire_on_a_big_percentage(self):
        """A 76% fall on a 0.1 m urban khola is recession, not a dammed river.

        This is a regression test: it fired in production before absolute
        floors were added.
        """
        st = {"id": 2, "name": "Nakkhu at Bungmati", "basin": "Bagmati"}
        levels = [("t1", 0.45), ("t2", 0.44), ("t3", 0.43), ("t4", 0.10)]
        sig = outburst.detect_impoundment(st, levels, 60.0, 20.0)
        assert not sig.suspected
        assert "shallow" in sig.reason

    def test_small_absolute_drop_does_not_fire(self):
        st = {"id": 3, "name": "Trishuli at Betrawati", "basin": "Narayani"}
        levels = [("t1", 2.0), ("t2", 2.0), ("t3", 2.0), ("t4", 1.79)]  # 0.21 m
        assert not outburst.detect_impoundment(st, levels, 60.0, 20.0).suspected

    def test_insufficient_history_is_not_an_alarm(self):
        st = {"id": 4, "name": "Trishuli at Betrawati", "basin": "Narayani"}
        assert not outburst.detect_impoundment(st, [("t", 5.0)], 90.0, 90.0).suspected

    def test_transboundary_uses_dhm_basin_vocabulary(self):
        """Regression: the list once held tributary names DHM never emits."""
        assert outburst.is_transboundary(
            {"basin": "Narayani", "name": "Bhote Koshi at Rasuwagadi"})
        assert not outburst.is_transboundary(
            {"basin": "Bagmati", "name": "Nakkhu at Bungmati"})


class TestEarthRotation:
    def test_three_gorges_is_the_published_order_of_magnitude(self):
        r = earth_rotation.report()["calculations"][0]
        assert 0.01 < r["delta_length_of_day_us"] < 1.0

    def test_latitude_term_dominates_the_elevation_lift(self):
        """Computing only the lift understates the answer ~1000x."""
        r = earth_rotation.report()["calculations"][0]
        assert abs(float(r["delta_I_kg_m2"])) > 100 * abs(
            float(r["delta_I_from_elevation_only_kg_m2"]))

    def test_conclusion_is_stated_not_implied(self):
        assert earth_rotation.report()["calculations"][0]["affects_river_discharge"] is False


class TestCleaning:
    @pytest.mark.parametrize("raw,expected", [
        (" 2.34 m ", 2.34), ("2.34", 2.34), ("1,234.5", 1234.5),
        (" ", None), ("", None), ("N/A", None), ("-", None), (None, None),
    ])
    def test_number_parsing(self, raw, expected):
        assert clean.norm_float(raw) == expected

    def test_blank_is_none_never_zero(self):
        """A zero stage reads as an empty river and scores as safe."""
        # `is None`, not `== 0`: 0.0 would score NORMAL and hide the outage.
        assert clean.norm_float(" ") is None
        assert clean.norm_float(" ") != 0.0

    @pytest.mark.parametrize("raw,expected", [
        ("sindhupalchowk", "Sindhupalchok"), ("kavre", "Kavrepalanchok"),
        ("KATHMANDU", "Kathmandu"),
    ])
    def test_district_canonicalisation(self, raw, expected):
        assert clean.norm_district(raw) == expected

    def test_station_titlecase_keeps_minor_words_lower(self):
        assert clean.title_case_station(
            "kokhajor khola at hariharpurgadi") == "Kokhajor Khola at Hariharpurgadi"

    def test_bbox_is_enforced(self):
        assert clean.in_nepal(27.7, 85.3)
        assert not clean.in_nepal(48.8, 2.3)

    def test_impossible_stage_jump_is_rejected(self):
        assert clean.reject_stage_outlier(
            7.0, "2026-08-29T12:00:00+05:45", 2.0, "2026-08-29T11:00:00+05:45")

    def test_plausible_rise_is_kept(self):
        assert not clean.reject_stage_outlier(
            2.4, "2026-08-29T12:00:00+05:45", 2.0, "2026-08-29T11:00:00+05:45")

    def test_naive_timestamps_are_read_as_nepal_time(self):
        """Getting this wrong shifts every rise rate by 5h45m."""
        assert clean.norm_timestamp("2026-08-29 12:00:00").endswith("+05:45")

    def test_inconsistent_marks_are_discarded_not_reordered(self):
        st = clean.clean_station({
            "id": 1, "name": "x", "lat": 27.7, "lon": 85.3,
            "warning_level": "6.0", "danger_level": "4.0",
        })
        assert st["danger_level"] is None


class TestSecurityGuards:
    """Regression tests for findings CodeQL raised, so they cannot come back."""

    def test_unknown_tile_style_is_refused(self):
        assert tiles.cache_path("dark", 8, 188, 107)          # known style works
        with pytest.raises(KeyError):
            _ = tiles.STYLES["../../etc/passwd"]


class TestGlofWatch:
    """The known-lake ranking, not a breach predictor -- see the module's own
    scope statement. These tests pin the ranking order and the live
    cross-check, not any invented probability."""

    def test_six_priority_lakes_are_present(self):
        names = {lake.name for lake in glof_watch.PRIORITY_LAKES}
        assert {"Tsho Rolpa", "Imja Tsho", "Thulagi (Dona)",
                "Lower Barun (Tallopokhari)", "Lumding Tsho",
                "Hongu 2 (Chamlang South)"} == names

    def test_all_priority_lakes_are_rank_i(self):
        """Every named lake in the ICIMOD/UNDP 2026 inventory used here is Rank I."""
        assert all(lake.rank == "I" for lake in glof_watch.PRIORITY_LAKES)

    def test_active_impoundment_surfaces_as_live_corroboration(self):
        scores = [{"id": 1, "name": "Dudh Koshi at Rabuwabazar", "basin": "koshi",
                   "district": "Solukhumbu", "impoundment_suspected": True,
                   "impoundment_reason": "stage down 22% vs prior median"}]
        out = glof_watch.rank_glof_watch(scores)
        imja = next(r for r in out["lakes"] if r["lake"]["name"] == "Imja Tsho")
        assert imja["live_corroboration"] is True
        assert "Dudh Koshi at Rabuwabazar" in imja["note"]

    def test_no_matching_gauge_is_not_treated_as_corroboration(self):
        out = glof_watch.rank_glof_watch([])
        assert all(not r["live_corroboration"] for r in out["lakes"])

    def test_a_quiet_gauge_in_the_same_basin_does_not_falsely_corroborate(self):
        scores = [{"id": 1, "name": "Dudh Koshi at Rabuwabazar", "basin": "koshi",
                   "district": "Solukhumbu", "impoundment_suspected": False}]
        out = glof_watch.rank_glof_watch(scores)
        imja = next(r for r in out["lakes"] if r["lake"]["name"] == "Imja Tsho")
        assert imja["live_corroboration"] is False
        assert imja["nearby_stations"]              # monitored, just not flagged

    def test_live_signal_sorts_above_a_quiet_lake_of_the_same_rank(self):
        scores = [{"id": 1, "name": "Dudh Koshi at Rabuwabazar", "basin": "koshi",
                   "district": "Solukhumbu", "impoundment_suspected": True,
                   "impoundment_reason": "stage down 22% vs prior median"}]
        out = glof_watch.rank_glof_watch(scores)
        first = out["lakes"][0]
        assert first["live_corroboration"] is True

    def test_scope_statement_disclaims_prediction(self):
        """This is the one assertion that must never be quietly deleted: the
        module must keep saying what it is not."""
        out = glof_watch.rank_glof_watch([])
        assert "not a" in out["scope"].lower()


class TestReferenceData:
    """Country-profile static data: shape and internal consistency, not the
    specific numbers, since those are edited when a newer census/survey lands."""

    def test_demographics_has_a_cited_source(self):
        d = reference_data.demographics()
        assert d["source"]["publisher"]
        assert d["source"]["url"].startswith("https://")

    def test_major_groups_are_a_minority_of_the_long_tail(self):
        """142 groups exist; the named top 10 should not overclaim the total."""
        d = reference_data.demographics()
        assert sum(g["percent"] for g in d["major_groups"]) < 100

    def test_wildlife_protected_area_counts_match_dnpwc_totals(self):
        """12 national parks, 1 wildlife reserve, 1 hunting reserve, 6
        conservation areas -- the officially stated system, not just "some"."""
        areas = reference_data.wildlife()["protected_areas"]["areas"]
        by_kind = {}
        for a in areas:
            by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
        assert by_kind == {"National Park": 12, "Wildlife Reserve": 1,
                            "Hunting Reserve": 1, "Conservation Area": 6}

    def test_species_counts_cite_a_survey_year_or_say_why_not(self):
        for row in reference_data.wildlife()["species_counts"]:
            assert row["survey_year"] is not None or "note" in row

    def test_gibs_rejects_a_malformed_date(self):
        import asyncio
        assert asyncio.run(tiles.fetch_gibs(None, "flood", "not-a-date", 8, 188, 107)) is None

    def test_remote_content_type_cannot_inject_a_log_line(self):
        assert "\n" not in tiles._content_kind("image/png\nFAKE ENTRY")
        assert tiles._content_kind("image/png\nFAKE ENTRY") == "image"

    def test_exception_text_never_reaches_the_log(self):
        import httpx
        described = tiles._fault(httpx.ConnectTimeout("https://secret.example/path?token=abc"))
        assert "secret.example" not in described
        assert described == "ConnectTimeout"

    def test_tiles_outside_nepal_are_refused(self):
        assert tiles.in_nepal_tile(*tiles.deg2num(27.7, 85.3, 8), 8)
        assert not tiles.in_nepal_tile(*tiles.deg2num(48.8, 2.3, 8), 8)
