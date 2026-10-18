"""Horizonte del producto y reutilización sin nuevas adquisiciones."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src import eclipse_catalogs as catalogs
from src import eclipse_pipeline as pipeline
from src import eclipse_sources as sources


class PlanningCatalogTests(unittest.TestCase):
    def test_three_calendar_years_and_leap_day(self):
        self.assertEqual(catalogs.planning_window("2026-10-06"), ("2026-10-06", "2029-10-06"))
        self.assertEqual(catalogs.planning_window("2028-02-29"), ("2028-02-29", "2031-02-28"))
        for years in [0, -1, 11]:
            with self.assertRaises(ValueError):
                catalogs.planning_window("2026-10-06", years)

    def test_only_required_centuries_are_acquired(self):
        requests = []
        def fetch(url, path, client, *args, **kwargs):
            requests.append(url)
            return (sources.ROOT / "data/bronze/nasa" / path.name).read_bytes()

        with tempfile.TemporaryDirectory() as directory, patch.object(sources, "fetch", side_effect=fetch):
            root = Path(directory)
            events = catalogs.load_planning_catalog(Mock(), offline=True, as_of="2026-10-06", root=root)
            metadata = json.loads((root / "data/gold/planning_eclipse_catalog.metadata.json").read_text())
        self.assertEqual(len(events), 14)
        self.assertEqual(events.event_family.value_counts().to_dict(), {"solar": 7, "lunar": 7})
        self.assertEqual(len(requests), 4)  # Dos índices y un siglo por familia.
        self.assertEqual(len(metadata["century_sources"]), 2)
        self.assertTrue(events.event_date.ge("2026-10-06").all())
        self.assertTrue(events.event_date.lt("2029-10-06").all())
        self.assertFalse(any("2101-2200" in url for url in requests))

    def test_window_crossing_century_fetches_both_required_pages(self):
        requests = []
        def fetch(url, path, client, *args, **kwargs):
            requests.append(url)
            return (sources.ROOT / "data/bronze/nasa" / path.name).read_bytes()

        with tempfile.TemporaryDirectory() as directory, patch.object(sources, "fetch", side_effect=fetch):
            events = catalogs.load_planning_catalog(Mock(), offline=True, as_of="2099-10-06", root=Path(directory))
        self.assertEqual(len(requests), 6)
        self.assertTrue(any("SE2101-2200" in url for url in requests))
        self.assertTrue(any("LE2101-2200" in url for url in requests))
        self.assertTrue(events.event_date.lt("2102-10-06").all())


class PreparationReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.event = {"event_id": "NASA-SE-09568", "event_date": "2027-08-02", "event_family": "solar", "eclipse_type": "total"}
        self.locations = pd.DataFrame([{**self.event, "name": "Destino", "latitude": 36., "longitude": -5.,
            "candidate_type": "ciudad NASA", "phase_priority": 3, "local_phase": "total",
            "event_time_utc_approx": "2027-08-02T08:20:00+00:00", "body_altitude_deg": 40.,
            "central_duration_s": 180., "phase_duration_s": 180., "visible_eclipse_duration_s": 7200.,
            "obscuration_fraction": 1.}])

    def weather_fetch(self, url, path, client, params=None, *args, **kwargs):
        self.assertEqual(url, sources.WEATHER_URL)
        dates = pd.date_range(params["start_date"], pd.Timestamp(params["end_date"]) + pd.Timedelta(23, unit="h"), freq="h")
        variables = params["hourly"].split(",")
        units = {key: "unit" for key in variables}
        units.update({"cloud_cover": "%", "precipitation": "mm", "rain": "mm"})
        payload = {"utc_offset_seconds": 0, "latitude": 36., "longitude": -5., "elevation": 0,
            "hourly_units": units, "hourly": {"time": dates.strftime("%Y-%m-%dT%H:%M").tolist(),
                **{key: [20. if key == "temperature_2m" else 0.] * len(dates) for key in variables}}}
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload).encode()
        path.write_bytes(body)
        return body

    def prepare(self, **options):
        options.setdefault("offline", True)
        return pipeline.acquire_complete_event(self.event, root=self.root, start=2024, end=2024, **options)

    def initial_preparation(self):
        with patch.object(pipeline, "prepare_locations", return_value=(self.locations, {"method": "fixture"})), \
             patch.object(sources, "fetch", side_effect=self.weather_fetch), \
             patch.object(pipeline, "operational_forecast", return_value=(pd.DataFrame(), {"status": "outside_forecast_horizon"})):
            return self.prepare(client=Mock())

    def test_second_run_reuses_complete_gold_without_sources(self):
        first, report = self.initial_preparation()
        self.assertFalse(report["reused_preparation"])
        with patch.object(pipeline, "prepare_locations", side_effect=AssertionError("No recalcular")), \
             patch.object(sources, "fetch", side_effect=AssertionError("No descargar")), \
             patch.object(sources, "session", side_effect=AssertionError("No crear cliente")):
            reused, saved = self.prepare()
        self.assertTrue(saved["reused_preparation"])
        self.assertEqual(first.name.tolist(), reused.name.tolist())

    def test_changed_configuration_and_tampered_outputs_are_not_reused(self):
        _, report = self.initial_preparation()
        config = report["preparation_config"]
        self.assertIsNone(pipeline.saved_preparation(self.event, {**config, "days_after": 3}, self.root))
        path = self.root / "data/gold/events/2027-08-02/destination_ranking.csv"
        path.write_text("changed", encoding="utf-8")
        self.assertIsNone(pipeline.saved_preparation(self.event, config, self.root))

    def test_partial_preparation_is_not_reused(self):
        _, report = self.initial_preparation()
        report["climate"]["failures"] = [{"reference_year": 2024, "error": "partial"}]
        sources.save_json(self.root / "data/gold/events/2027-08-02/pipeline_quality_report.json", report)
        self.assertIsNone(pipeline.saved_preparation(self.event, report["preparation_config"], self.root))

    def test_refresh_rebuilds_even_when_gold_is_complete(self):
        self.initial_preparation()
        with patch.object(pipeline, "prepare_locations", return_value=(self.locations, {"method": "fixture"})) as geometry, \
             patch.object(sources, "fetch", side_effect=self.weather_fetch) as fetch, \
             patch.object(pipeline, "operational_forecast", return_value=(pd.DataFrame(), {"status": "outside_forecast_horizon"})):
            _, report = self.prepare(client=Mock(), offline=False, refresh=True)
        self.assertFalse(report["reused_preparation"])
        geometry.assert_called_once()
        fetch.assert_called_once()

    def test_cli_without_options_prepares_window_and_export_only_uses_no_network(self):
        ranking, report = self.initial_preparation()
        with patch.object(sources, "ROOT", self.root), \
             patch.object(sources, "session", return_value=Mock()), \
             patch.object(pipeline, "load_planning_catalog", return_value=pd.DataFrame([self.event])) as load, \
             patch.object(pipeline, "acquire_complete_event", return_value=(ranking, report)) as acquire, \
             patch("src.eclipse_product.export_current_window") as export, redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main([]), 0)
            acquire.assert_called_once()
        self.assertEqual(load.call_args.args[4], 3)
        export.assert_called_once()
        self.assertEqual(export.call_args.args[0], load.call_args.args[3], "Catálogo y selector comparten el inicio de hoy.")
        folder = self.root / "data/gold"
        with patch.object(sources, "ROOT", self.root), \
             patch.object(sources, "session", side_effect=AssertionError("Exportar no consulta fuentes")), \
             patch("src.eclipse_product.catalog_between", return_value=pd.DataFrame([self.event])), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(["--export-only"]), 0)
        payload = json.loads((folder / "planner_data.json").read_text())
        # Un solo año histórico se exporta, pero no se presenta como clasificación validada.
        self.assertEqual(payload["counts"]["partial"], 1)
        self.assertIn("al menos 15", payload["events"][0]["error"])


if __name__ == "__main__":
    unittest.main()
