"""Parsers con datos NASA reales y casos meteorológicos que comprueban la semántica."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src import eclipse_sources as sources


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (sources.ROOT / "data/bronze/nasa/solar_catalog_2001_2100.html").read_bytes()
        cls.catalog = sources.parse_catalog(cls.html)

    def test_catalog_has_all_types_and_preserves_source_links(self):
        self.assertEqual(len(self.catalog), 224)
        self.assertEqual(self.catalog.eclipse_type.value_counts().to_dict(),
                         {"partial": 77, "total": 68, "annular": 72, "hybrid": 7})
        event = sources.select_event(self.catalog, "2027-08-02")
        self.assertEqual(event["event_id"], "NASA-SE-09568")
        self.assertTrue(event["path_url"].endswith("SE2027Aug02Tpath.html"))
        self.assertEqual(event["greatest_eclipse_td"], "10:07:50")
        self.assertEqual(event["greatest_eclipse_ut_approx"], "2027-08-02T10:06:34+00:00")

    def test_unknown_date_and_unsafe_format_rejected(self):
        for day in ["2027-08-03", "../../2027-08-02", "2027-02-30"]:
            with self.assertRaises(ValueError):
                sources.select_event(self.catalog, day)

    def test_far_future_catalog_with_nanosecond_timedeltas(self):
        # Reproduce la resolución que provoca el desbordamiento en pandas;
        # las fechas NASA deben seguir disponibles sin recortar el catálogo.
        original_timedelta = pd.Timedelta
        def nanosecond_timedelta(*args, **kwargs):
            return original_timedelta(*args, **kwargs).as_unit("ns")

        cases = [("SE2201-2300.html", "2262-06-17", "2262-06-17T14:09:10+00:00"),
                 ("SE2901-3000.html", "3000-10-19", "3000-10-19T14:56:28+00:00")]
        with patch.object(pd, "Timedelta", side_effect=nanosecond_timedelta):
            for filename, day, expected in cases:
                with self.subTest(day=day):
                    html = (sources.ROOT / "data/bronze/nasa" / filename).read_bytes()
                    event = sources.select_event(sources.parse_catalog(html), day)
                    self.assertEqual(event["greatest_eclipse_ut_approx"], expected)

    def test_partial_is_cataloged_without_totality_geometry(self):
        event = sources.select_event(self.catalog, "2025-03-29")
        self.assertFalse(event["geometry_supported"])
        self.assertTrue(pd.isna(event["central_duration_at_greatest_s"]))
        with self.assertRaisesRegex(ValueError, "geometría"):
            sources.acquire_event(event, client=Mock())

    def test_real_path_normalizes_boundaries_and_duration(self):
        html = (sources.ROOT / "data/bronze/events/2027-08-02/nasa_path.html").read_bytes()
        path = sources.parse_path(html)
        self.assertEqual(len(path), 101)
        self.assertAlmostEqual(path.iloc[0].duration_center_s, 204.6)
        self.assertAlmostEqual(path.iloc[0].longitude, -36.23333333333333)
        self.assertTrue(path.time_utc.is_monotonic_increasing)

    def test_interpolation_crosses_antimeridian_by_short_segment(self):
        path = pd.DataFrame({"latitude": [0.0, 0.0], "longitude": [179.0, -179.0],
            "time_utc": [100, 102], "duration_center_s": [200.0, 200.0], "path_width_km": [100.0, 100.0]})
        circumstances = sources.nearest_central_line_point(0.0, 180.0, path)
        self.assertAlmostEqual(circumstances["distance_km"], 0.0)
        self.assertAlmostEqual(circumstances["time_utc_min"], 101.0)

    def test_verified_cache_runs_without_network(self):
        client = Mock()
        path = sources.ROOT / "data/bronze/nasa/solar_catalog_2001_2100.html"
        self.assertEqual(sources.fetch(sources.CATALOG_URL, path, client, offline=True), self.html)
        client.get.assert_not_called()

    def test_tampered_cache_is_not_silently_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.html"
            path.write_text("tampered", encoding="utf-8")
            sources.save_json(path.with_suffix(".html.metadata.json"), {
                "source_url": sources.CATALOG_URL, "params": {}, "sha256": "bad"})
            with self.assertRaisesRegex(ValueError, "verificada"):
                sources.fetch(sources.CATALOG_URL, path, Mock(), offline=True)


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.locations = pd.DataFrame([{"name": "Test", "latitude": 36.0, "longitude": -5.0,
                                        "time_utc_min": 8 * 60 + 20}])
        self.event = {"event_id": "NASA-SE-test", "event_date": "2027-08-02"}
        self.item = {
            "utc_offset_seconds": 0, "latitude": 36.1, "longitude": -5.1, "elevation": 20,
            "hourly_units": {"cloud_cover": "%", "precipitation": "mm", "rain": "mm"},
            "hourly": {"time": [f"2020-08-02T{hour:02d}:00" for hour in range(24)],
                       "cloud_cover": [0.0] * 24, "precipitation": [0.0] * 24, "rain": [0.0] * 24}}

    def test_rain_uses_containing_hour_not_nearest_cloud_hour(self):
        self.item["hourly"]["cloud_cover"][8] = 50
        self.item["hourly"]["rain"][9] = 0.2
        self.item["hourly"]["precipitation"][9] = 0.3
        records, missing = sources.parse_weather(self.item, self.locations, "2020-08-02", self.event)
        self.assertFalse(missing)
        self.assertEqual(records[0]["cloud_cover_pct"], 50)
        self.assertEqual(records[0]["rain_mm"], 0.2)
        self.assertEqual(records[0]["rain_proxy"], 1)
        self.assertIn("09:00", records[0]["rain_interval_end_utc"])

    def test_missing_rain_is_not_treated_as_zero(self):
        self.item["hourly"]["rain"][9] = None
        records, missing = sources.parse_weather(self.item, self.locations, "2020-08-02", self.event)
        self.assertFalse(records)
        self.assertEqual(len(missing), 1)

    def test_units_wrong_day_and_negative_rain_rejected(self):
        for change in ["units", "day", "rain"]:
            item = copy.deepcopy(self.item)
            if change == "units":
                item["hourly_units"]["rain"] = "inch"
            elif change == "day":
                item["hourly"]["time"][0] = "2019-08-02T00:00"
            else:
                item["hourly"]["rain"][9] = -1
            with self.assertRaises(ValueError):
                sources.parse_weather(item, self.locations, "2020-08-02", self.event)

    def test_zero_events_has_nonzero_uncertainty_bound(self):
        lower, upper = sources.wilson_interval(0, 30)
        self.assertAlmostEqual(lower, 0)
        self.assertGreater(upper, 0.1)
        self.assertLess(upper, 0.12)

    def test_request_years_never_include_event_year_or_future_days(self):
        with self.assertRaises(ValueError):
            sources.historical_years("2027-08-02", 2025, 2027)
        years = sources.historical_years("2100-08-02", 2020, 2099)
        self.assertLessEqual(max(years), pd.Timestamp.now(tz="UTC").year)

    def test_distant_future_event_uses_recent_available_history(self):
        for day in ["2100-08-02", "2262-06-17", "3000-10-19"]:
            with self.subTest(day=day):
                years = sources.historical_years(day)
                self.assertEqual(len(years), 30)
                self.assertLessEqual(max(years), pd.Timestamp.now(tz="UTC").year)
                self.assertEqual(max(years) - min(years), 29)


    def test_fixed_cutoff_makes_reference_years_independent_of_run_date(self):
        years = sources.historical_years("2028-12-31", as_of="2026-10-06")
        self.assertEqual((years[0], years[-1]), (1996, 2025))
        self.assertEqual(sources.historical_years("2029-06-12", as_of="2026-10-06")[-1], 2026)


class RateLimitTests(unittest.TestCase):
    def response(self, status, reason="", content=b"{}"):
        response = Mock(status_code=status, headers={}, content=content, url="https://api.test", text=reason)
        response.json.return_value = {"error": True, "reason": reason} if status == 429 else {}
        return response

    def test_daily_quota_stops_without_caching(self):
        client = Mock()
        client.get.return_value = self.response(429, "Daily API request limit exceeded. Please try again tomorrow.")
        with tempfile.TemporaryDirectory() as directory, patch.object(sources.time, "sleep") as sleep:
            path = Path(directory) / "climate.json"
            with self.assertRaises(sources.RateLimitExceeded):
                sources.fetch(sources.WEATHER_URL, path, client, {"a": 1})
            self.assertFalse(path.exists())
        sleep.assert_not_called()

    def test_minute_quota_waits_and_retries(self):
        client = Mock()
        client.get.side_effect = [self.response(429, "Minutely API request limit exceeded."), self.response(200)]
        with tempfile.TemporaryDirectory() as directory, patch.object(sources.time, "sleep") as sleep, \
                patch("builtins.print"):
            body = sources.fetch(sources.WEATHER_URL, Path(directory) / "climate.json", client, {"a": 1})
        self.assertEqual(body, b"{}")
        sleep.assert_any_call(60)

    def test_interval_respects_hourly_weighted_quota(self):
        # 12 ubicaciones, 19 variables y 7 días: 24 llamadas ponderadas por petición.
        self.assertEqual(sources.weighted_calls(12, 19, 7), 24)
        self.assertAlmostEqual(sources.request_interval(12, 19, 7), 24 * 3600 / 5000)

if __name__ == "__main__":
    unittest.main()
