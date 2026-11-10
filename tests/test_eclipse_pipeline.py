"""Cobertura futura completa, visibilidad por tipo y ventanas meteorológicas."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from src import eclipse_catalogs as catalogs
from src import eclipse_locations as locations
from src import eclipse_pipeline as pipeline
from src import eclipse_sources as sources


def weather_payload(params, variables):
    dates = pd.date_range(params["start_date"], pd.Timestamp(params["end_date"]) + pd.Timedelta(23, unit="h"), freq="h")
    units = {variable: "unit" for variable in variables}
    units.update({"cloud_cover": "%", "rain": "mm", "precipitation": "mm"})
    return {"utc_offset_seconds": 0, "latitude": 36.0, "longitude": -5.0, "elevation": 10,
        "hourly_units": units, "hourly": {"time": [value.strftime("%Y-%m-%dT%H:%M") for value in dates],
        **{variable: [0.0] * len(dates) for variable in variables}}}


class FutureCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Lee los originales guardados y escribe los catálogos en un directorio temporal.
        def saved(url, path, client, *args, **kwargs):
            return (sources.ROOT / "data/bronze/nasa" / path.name).read_bytes()

        with tempfile.TemporaryDirectory() as directory, patch.object(sources, "fetch", side_effect=saved):
            cls.events, cls.future = catalogs.load_all_catalogs(Mock(), offline=True, as_of="2026-10-06", root=Path(directory))

    def test_all_discovered_centuries_and_families_are_present(self):
        self.assertEqual(len(self.events), 4802)
        self.assertEqual(len(self.future), 4687)
        self.assertEqual(set(self.future.event_family), {"solar", "lunar"})
        self.assertEqual(self.future.event_date.max()[:4], "3000")
        self.assertTrue(self.future.event_date.ge("2026-10-06").all())
        self.assertTrue(self.future.event_id.is_unique)

    def test_lunar_durations_and_future_selection(self):
        event = catalogs.select_event(self.events, "2027-02-20", "lunar")
        self.assertEqual(event["eclipse_type"], "penumbral")
        self.assertGreater(event["penumbral_duration_s"], 0)
        far = self.future.loc[self.future.event_date.gt("2900-01-01")].iloc[0]
        self.assertEqual(catalogs.select_event(self.events, far.event_date)["event_id"], far.event_id)

    def test_lunar_zenith_and_opposite_hemisphere(self):
        event = catalogs.select_event(self.events, "2027-02-20", "lunar")
        overhead = locations.lunar_circumstances(event, event["greatest_latitude"], event["greatest_longitude"])
        self.assertAlmostEqual(overhead["body_altitude_deg"], 90, places=5)
        self.assertGreater(overhead["phase_duration_s"], 0)
        self.assertTrue(np.isnan(overhead["visible_eclipse_duration_s"]))
        opposite = (event["greatest_longitude"] + 360) % 360 - 180
        self.assertIsNone(locations.lunar_circumstances(event, -event["greatest_latitude"], opposite))

    def test_solar_totality_matches_saved_local_geometry(self):
        event = catalogs.select_event(self.events, "2027-08-02", "solar")
        body = (sources.ROOT / "data/bronze/events/2027-08-02/besselian.html").read_bytes()
        elements = locations.parse_besselian(body)
        gibraltar = locations.solar_circumstances(elements, event, 36.1408, -5.3536)
        self.assertEqual(gibraltar["local_phase"], "total")
        self.assertAlmostEqual(gibraltar["central_duration_s"], 267, delta=5)
        self.assertAlmostEqual(gibraltar["obscuration_fraction"], 1)
        self.assertGreater(gibraltar["visible_eclipse_duration_s"], gibraltar["central_duration_s"])
        madrid = locations.solar_circumstances(elements, event, 40.4168, -3.7038)
        self.assertEqual(madrid["local_phase"], "partial")

    def test_hybrid_path_preserves_zero_width_transitions(self):
        # Trayectoria NASA real del híbrido de 2031-11-14, fuera de la ventana del producto.
        path = sources.parse_path((Path(__file__).parent / "fixtures/nasa_path_2031-11-14_hybrid.html").read_bytes())
        self.assertTrue(path.path_width_km.ge(0).all())
        self.assertTrue(path.duration_center_s.ge(0).all())
        self.assertGreater(len(path), 3)

    def test_forecast_endpoint_real_snapshot_has_expected_conditions(self):
        payload = json.loads((sources.ROOT / "data/bronze/validation/forecast_endpoint.json").read_bytes())
        begin, end = payload["hourly"]["time"][0][:10], payload["hourly"]["time"][-1][:10]
        candidates = pd.DataFrame([{"name": "Validation", "latitude": 36.1408, "longitude": -5.3536}])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            frame, units = pipeline.parse_hourly(payload, candidates, pipeline.FORECAST_VARIABLES, begin, end)
        self.assertEqual(units["precipitation_probability"], "%")
        self.assertTrue(frame.precipitation_probability.dropna().between(0, 100).all())
        self.assertIn("visibility", frame)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.event = {"event_id": "TEST", "event_date": "2027-08-02", "event_family": "solar"}
        self.candidates = pd.DataFrame([{"name": "Test", "latitude": 36.0, "longitude": -5.0,
            "event_time_utc_approx": "2027-08-02T08:20:00+00:00"}])

    def fake_fetch(self, url, path, client, params=None, refresh=False, offline=False, min_interval=0.0):
        variables = params["hourly"].split(",")
        body = weather_payload(params, variables)
        dates = body["hourly"]["time"]
        if "2024-08-02T09:00" in dates:
            index = dates.index("2024-08-02T09:00")
            body["hourly"]["rain"][index] = 0.2
            body["hourly"]["precipitation"][index] = 0.3
        path.parent.mkdir(parents=True, exist_ok=True)
        sources.save_json(path.with_suffix(".json.metadata.json"), {"acquired_utc": "2026-10-06T12:00:00+00:00"})
        return json.dumps(body).encode()

    def test_climate_includes_previous_next_days_and_full_hours(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(sources, "fetch", side_effect=self.fake_fetch):
            hourly, samples, report = pipeline.climate_window(self.event, self.candidates, Mock(), [2024],
                before=2, after=2, root=Path(temp), offline=True)
        self.assertEqual(len(hourly), 120)
        self.assertEqual(set(samples.day_offset), {-2, -1, 0, 1, 2})
        event_hour = samples.loc[samples.day_offset.eq(0)].iloc[0]
        self.assertEqual(event_hour.rain, 0.2)
        self.assertEqual(event_hour.rain_proxy, 1)
        self.assertIn("09:00", event_hour.rain_interval_end_utc)
        self.assertEqual(report["failures"], [])

    def test_midnight_and_adjacent_year_dates_are_supported(self):
        self.event["event_date"] = "2027-01-01"
        self.candidates["event_time_utc_approx"] = "2027-01-02T23:55:00+00:00"
        with tempfile.TemporaryDirectory() as temp, patch.object(sources, "fetch", side_effect=self.fake_fetch):
            hourly, samples, _ = pipeline.climate_window(self.event, self.candidates, Mock(), [2024],
                before=2, after=2, root=Path(temp), offline=True)
        self.assertEqual(len(hourly), 120)
        self.assertTrue(samples.reference_year.eq(2024).all())
        self.assertEqual(samples.loc[samples.day_offset.eq(0)].iloc[0].rain_interval_end_utc, "2024-01-03T00:00:00+00:00")

    def test_year_3000_event_keeps_weather_dates_historical(self):
        self.event["event_date"] = "3000-08-02"
        self.candidates["event_time_utc_approx"] = "3000-08-02T08:20:00+00:00"
        with tempfile.TemporaryDirectory() as temp, patch.object(sources, "fetch", side_effect=self.fake_fetch):
            hourly, samples, report = pipeline.climate_window(self.event, self.candidates, Mock(), [2024],
                before=2, after=2, root=Path(temp), offline=True)
        self.assertEqual(len(hourly), 120)
        self.assertTrue(hourly.time_utc.dt.year.eq(2024).all())
        self.assertEqual(set(samples.day_offset), {-2, -1, 0, 1, 2})
        self.assertEqual(report["failures"], [])

    def test_forecast_unavailable_far_future_does_not_request_network(self):
        for day in ["2027-08-02", "3000-08-02"]:
            with self.subTest(day=day):
                self.event["event_date"] = day
                self.candidates["event_time_utc_approx"] = day + "T08:20:00+00:00"
                client = Mock()
                frame, status = pipeline.operational_forecast(self.event, self.candidates, client,
                    now=datetime(2026, 10, 6, tzinfo=timezone.utc))
                self.assertTrue(frame.empty)
                self.assertEqual(status["status"], "outside_forecast_horizon")
                client.get.assert_not_called()

    def test_forecast_available_keeps_probability_separate(self):
        self.event["event_date"] = "2026-10-08"
        self.candidates["event_time_utc_approx"] = "2026-10-08T08:20:00+00:00"
        with tempfile.TemporaryDirectory() as temp, patch.object(sources, "fetch", side_effect=self.fake_fetch):
            frame, status = pipeline.operational_forecast(self.event, self.candidates, Mock(), before=1, after=1,
                root=Path(temp), now=datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.assertEqual(status["status"], "available")
        self.assertEqual(len(frame), 72)
        self.assertIn("precipitation_probability", frame)
        self.assertIn("visibility", frame)

    def test_missing_hours_are_rejected(self):
        params = {"start_date": "2024-08-01", "end_date": "2024-08-03"}
        payload = weather_payload(params, pipeline.HISTORICAL_VARIABLES)
        payload["hourly"]["time"][0] = "2024-08-01T01:00"
        with self.assertRaisesRegex(ValueError, "Horas ausentes"):
            pipeline.parse_hourly(payload, self.candidates, pipeline.HISTORICAL_VARIABLES, "2024-08-01", "2024-08-03")

    def test_missing_values_stay_null_and_invalid_values_are_rejected(self):
        params = {"start_date": "2024-08-01", "end_date": "2024-08-03"}
        payload = weather_payload(params, pipeline.HISTORICAL_VARIABLES)
        payload["hourly"]["rain"][33] = None
        hourly, _ = pipeline.parse_hourly(payload, self.candidates, pipeline.HISTORICAL_VARIABLES, "2024-08-01", "2024-08-03")
        self.assertTrue(pd.isna(hourly.rain.iloc[33]), "Un valor ausente no se convierte en cero.")
        self.assertEqual(hourly.rain.isna().sum(), 1)
        for change, message in [("units", "Unidades"), ("negative", "negativos"), ("rain", "incoherente")]:
            broken = weather_payload(params, pipeline.HISTORICAL_VARIABLES)
            if change == "units":
                broken["hourly_units"]["rain"] = "inch"
            elif change == "negative":
                broken["hourly"]["precipitation"][9] = -1.0
            else:
                broken["hourly"]["rain"][9] = 0.5
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                pipeline.parse_hourly(broken, self.candidates, pipeline.HISTORICAL_VARIABLES, "2024-08-01", "2024-08-03")

    def test_forecast_updates_order_only_when_candidates_have_comparable_coverage(self):
        ranking = pd.DataFrame([{"name": name, "event_time_utc_approx": "2026-10-08T08:20:00+00:00",
            "phase_priority": 1, "astronomy_score": 0.5, "duration_score": 1, "altitude_score": 0.5,
            "score": score, "rank": index + 1} for index, (name, score) in enumerate([("A", 0.9), ("B", 0.7)])])
        forecast = pd.DataFrame([{"name": name, "time_utc": f"2026-10-08T{hour:02d}:00:00+00:00",
            "cloud_cover": cloud, "precipitation_probability": probability}
            for name, cloud, probability in [("A", 90, 80), ("B", 0, 0)] for hour in [8, 9]])
        updated = pipeline.apply_forecast_to_ranking(ranking, forecast, {"status": "available"})
        self.assertEqual(updated.iloc[0]["name"], "B")
        self.assertTrue(updated.ranking_basis.eq("operational_forecast_proxy").all())
        forecast.loc[forecast.name.eq("A"), "precipitation_probability"] = np.nan
        partial = pipeline.apply_forecast_to_ranking(ranking, forecast, {"status": "partially_available"})
        self.assertEqual(partial.iloc[0]["name"], "A")
        self.assertTrue(partial.ranking_basis.eq("historical_climatology").all())



class RankingAndShortlistTests(unittest.TestCase):
    def test_lunar_score_ignores_global_duration_and_counts_altitude_once(self):
        rows = pd.DataFrame([{"name": name, "event_id": "NASA-LE-09714", "phase_priority": 3,
            "body_altitude_deg": altitude, "obscuration_fraction": np.nan, "phase_duration_s": 3600.0}
            for name, altitude in [("Alta", 80.0), ("Baja", 10.0)]])
        samples = pd.DataFrame([{"name": name, "day_offset": 0, "clear_sky_proxy": 1, "rain_proxy": 0}
            for name in ["Alta", "Baja"] for _ in range(15)])
        ranking = pipeline.rank_locations(rows, samples)
        weights = pipeline.SCORE_WEIGHTS["lunar"]
        self.assertEqual(weights["duration"], 0)
        self.assertAlmostEqual(sum(weights.values()), 1)
        self.assertAlmostEqual(ranking.iloc[0].score, 0.45 + 0.20 + 0.35 * np.sin(np.radians(80)))
        self.assertEqual(ranking.iloc[0]["name"], "Alta")

    def test_shortlist_limits_references_and_covers_more_countries(self):
        event = {"event_id": "NASA-LE-09714", "event_date": "2028-12-31", "event_family": "lunar",
            "eclipse_type": "total", "eclipse_magnitude": 1.2, "total_duration_s": 3000.0,
            "greatest_latitude": 40.0, "greatest_longitude": 0.0,
            "greatest_eclipse_ut_approx": "2028-12-31T16:52:00+00:00", "source_url": "https://example.test"}
        cities = pd.DataFrame([{"name": f"{country}-{index}", "latitude": 40.0 - index - offset,
            "longitude": 0.0, "elevation_m": 0.0, "candidate_type": "ciudad NASA",
            "location_source": "fixture", "country_code": country}
            for offset, country in enumerate(["AA", "BB", "CC"]) for index in range(4)])
        with tempfile.TemporaryDirectory() as directory, patch.object(locations, "load_cities", return_value=cities):
            shortlist, _ = locations.prepare_locations(event, Mock(), Path(directory), max_locations=4, offline=True)
        references = shortlist.candidate_type.eq("referencia astronómica")
        self.assertEqual(references.sum(), 1)
        self.assertEqual(sorted(shortlist.loc[~references, "country_code"]), ["AA", "BB", "CC"])


class PlanningDatasetTests(unittest.TestCase):
    def setUp(self):
        self.event = {"event_id": "TEST", "event_date": "2027-08-02", "event_family": "solar"}
        self.locations = pd.DataFrame([{"name": "Test", "latitude": 36.0, "longitude": -15.0, "elevation_m": 5.0,
            "country_code": "ES", "candidate_type": "ciudad NASA", "body_altitude_deg": 60.0,
            "event_time_utc_approx": "2027-08-02T10:30:00+00:00"}])
        clear = {2021: 1, 2022: 0, 2023: 1}
        self.samples = pd.DataFrame([{"name": "Test", "reference_year": year, "day_offset": offset,
            "clear_sky_proxy": clear[year] if offset == 0 else 1, "rain_proxy": 1 - clear[year],
            "cloud_cover": 100 * (1 - clear[year]), "cloud_cover_low": 0.0, "grid_latitude": 36.0, "grid_longitude": -15.0}
            for year in clear for offset in range(-2, 3)])

    def test_climatology_uses_only_previous_years(self):
        dataset = pipeline.planning_dataset(self.event, self.locations, self.samples).set_index("reference_year")
        self.assertEqual(dataset.prior_years.tolist(), [0, 1, 2, 3])
        self.assertTrue(pd.isna(dataset.loc[2021, "prior_clear_frequency"]))
        self.assertEqual(dataset.loc[2022, "prior_clear_frequency"], 1.0)
        self.assertEqual(dataset.loc[2023, "prior_clear_frequency"], 0.5)
        # Ventana −2..+2 de 2021 y 2022: 4 + 4 días despejados más el día 0 de 2021.
        self.assertAlmostEqual(dataset.loc[2023, "prior_window_clear_frequency"], 9 / 10)
        self.assertEqual(dataset.loc[2023, "baseline_clear_probability"], dataset.loc[2023, "prior_clear_frequency"])
        self.assertEqual(dataset.loc[2023, "clear_sky_proxy"], 1)

    def test_query_row_has_all_history_and_no_label(self):
        dataset = pipeline.planning_dataset(self.event, self.locations, self.samples)
        query = dataset.loc[dataset.row_role.eq("query")].iloc[0]
        self.assertEqual(query.reference_year, 2027)
        self.assertEqual(query.prior_years, 3)
        self.assertAlmostEqual(query.prior_clear_frequency, 2 / 3)
        self.assertTrue(pd.isna(query.clear_sky_proxy))
        self.assertAlmostEqual(query.local_solar_hour, 9.5)

    def test_same_year_weather_is_never_a_predictor(self):
        dataset = pipeline.planning_dataset(self.event, self.locations, self.samples)
        predictors = set(pipeline.ML_COLUMNS["predictors"])
        self.assertFalse(predictors & set(pipeline.HISTORICAL_VARIABLES))
        self.assertFalse(predictors & set(pipeline.ML_COLUMNS["labels"]))
        self.assertEqual(list(dataset.columns), [column for role in pipeline.ML_COLUMNS.values() for column in role])
        # Cambiar la observación de un año no altera sus propios predictores.
        changed = self.samples.copy()
        changed.loc[changed.reference_year.eq(2023), ["clear_sky_proxy", "cloud_cover"]] = [0, 100]
        before = dataset.set_index("reference_year").loc[2023, sorted(predictors)]
        after = pipeline.planning_dataset(self.event, self.locations, changed).set_index("reference_year").loc[2023, sorted(predictors)]
        pd.testing.assert_series_equal(before, after)


if __name__ == "__main__":
    unittest.main()
