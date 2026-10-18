"""Snapshot del selector: identidad, límites, datos ausentes y semántica científica."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import eclipse_product as product
from src import eclipse_geography as geography


def event(event_date="2027-08-02", family="solar", kind="total", number="09568"):
    return {"event_id": f"NASA-{'SE' if family == 'solar' else 'LE'}-{number}",
        "event_date": event_date, "event_family": family, "eclipse_type": kind}


def prepared_event(root, selected, names=("Ciudad principal",), years=30):
    directory = root / "data/gold/events" / selected["event_date"]
    directory.mkdir(parents=True)
    rows = []
    for rank, name in enumerate(names, 1):
        rows.append({"name": name, "latitude": 36.0, "longitude": -5.0,
            "candidate_type": "ciudad NASA", "event_id": selected["event_id"],
            "event_date": selected["event_date"], "local_phase": selected["eclipse_type"],
            "event_time_utc_approx": selected["event_date"] + "T10:10:00+00:00",
            "obscuration_fraction": 0.95, "central_duration_s": 180.0,
            "phase_duration_s": 180.0, "published_phase_duration_s": 3600.0,
            "visible_eclipse_duration_s": 7200.0, "body_altitude_deg": 40.0,
            "clear_probability": 0.5, "rain_probability": 0.0, "cloud_probability": 0.5,
            "valid_cloud_years": years, "valid_rain_years": years,
            "ranking_validated_coverage": years >= 15, "destination_rank": rank})
    pd.DataFrame(rows).to_csv(directory / "destination_ranking.csv", index=False)
    summary = [{"name": name, "day_offset": offset, "clear_probability": 0.5,
        "rain_probability": 0.0, "valid_cloud_years": years, "valid_rain_years": years,
        "mean_temperature_c": 20 + offset, "mean_wind_speed_kmh": 8.0, "mean_cloud_cover_pct": 30.0}
        for name in names for offset in (-1, 0, 1)]
    pd.DataFrame(summary).to_csv(directory / "climate_daily_summary.csv", index=False)
    report = {**selected, "climate": {"years": list(range(2026 - years, 2026)),
        "days_before": 1, "days_after": 1, "failures": [], "missing_key_values": []},
        "forecast": {"status": "outside_forecast_horizon"},
        "preparation_config": {"version": 2, "event_id": selected["event_id"]},
        "prepared_at_utc": "2026-10-06T12:00:00+00:00"}
    (directory / "pipeline_quality_report.json").write_text(json.dumps(report), encoding="utf-8")
    return directory


def visible_candidates(root, selected, rows):
    directory = root / "data/silver/events" / selected["event_date"]
    directory.mkdir(parents=True, exist_ok=True)
    defaults = {**selected, "latitude": 36.0, "longitude": -5.0,
        "candidate_type": "ciudad NASA", "visible": True,
        "local_phase": selected["eclipse_type"],
        "event_time_utc_approx": selected["event_date"] + "T10:10:00+00:00",
        "obscuration_fraction": 0.95, "central_duration_s": 180.0,
        "phase_duration_s": 180.0, "published_phase_duration_s": 3600.0,
        "visible_eclipse_duration_s": 7200.0, "body_altitude_deg": 40.0}
    frame = pd.DataFrame([{**defaults, **row} for row in rows])
    frame.to_csv(directory / "all_visible_candidates.csv", index=False)
    return directory


def declared_country(row, root=None):
    return {**row, "country_code": row.get("country_code", "ES"),
        "country_name": row.get("country_name", "España"), "country_source": "test fixture"}


class ProductSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, events):
        return product.build_planner_data(pd.DataFrame(events), "2026-10-06", "2029-10-06", self.root)

    def test_window_is_chronological_and_excludes_exact_end(self):
        payload = self.build([event("2029-10-06", number="09575"), event("2028-01-01", number="09572"),
            event("2026-10-05", number="09567"), event("2026-10-06", number="09568")])
        self.assertEqual([row["event_date"] for row in payload["events"]], ["2026-10-06", "2028-01-01"])
        self.assertEqual(payload["counts"], {"events": 2, "ready": 0, "partial": 0, "pending": 2})
        self.assertEqual(payload["events"][0]["label"], "06-10-2026 · Eclipse solar total")

    def test_export_uses_saved_results_without_network_and_excludes_references(self):
        selected = event()
        directory = prepared_event(self.root, selected, ("Ciudad secundaria", "Ciudad principal", "Punto del mar"))
        ranking = pd.read_csv(directory / "destination_ranking.csv")
        ranking.loc[2, "candidate_type"] = "referencia astronómica"
        ranking.loc[0, "destination_rank"], ranking.loc[1, "destination_rank"] = 2, 1
        ranking.to_csv(directory / "destination_ranking.csv", index=False)
        original = (directory / "destination_ranking.csv").read_bytes()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("El selector no debe consultar la red")):
            payload = product.export_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", self.root)
        self.assertEqual((directory / "destination_ranking.csv").read_bytes(), original)
        output = payload["events"][0]
        self.assertEqual(output["status"], "ready")
        self.assertEqual([row["name"] for row in output["locations"]], ["Ciudad principal", "Ciudad secundaria"])
        self.assertEqual([row["rank"] for row in output["locations"]], [1, 2])
        self.assertEqual(output["locations"][0]["obscuration_pct"], 95.0)
        self.assertFalse(output["locations"][0]["ranking_provisional"])
        self.assertEqual(output["locations"][0]["rain_probability_pct"], 0.0)
        stored = json.loads((self.root / "data/gold/planner_data.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, payload)

    def test_lunar_visibility_is_null_and_duration_remains_global(self):
        selected = event("2028-12-31", "lunar", "total", "09714")
        prepared_event(self.root, selected)
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "ready")
        location = output["locations"][0]
        self.assertIsNone(location["obscuration_pct"])
        self.assertEqual(location["phase_duration_s"], 3600.0)
        self.assertEqual(location["duration_scope"], "global_lunar_phase")

    def test_partial_solar_duration_is_locally_visible_interval(self):
        selected = event("2029-01-14", "solar", "partial", "09572")
        prepared_event(self.root, selected)
        location = self.build([selected])["events"][0]["locations"][0]
        self.assertEqual(location["phase_duration_s"], 7200.0)
        self.assertEqual(location["duration_scope"], "local_visible")

    def test_missing_climate_is_null_and_event_is_partial(self):
        selected = event()
        directory = prepared_event(self.root, selected)
        ranking = pd.read_csv(directory / "destination_ranking.csv")
        ranking["rain_probability"] = np.nan
        ranking["valid_rain_years"] = 0
        ranking.to_csv(directory / "destination_ranking.csv", index=False)
        summary = pd.read_csv(directory / "climate_daily_summary.csv")
        summary["mean_temperature_c"] = np.nan
        summary.to_csv(directory / "climate_daily_summary.csv", index=False)
        payload = product.export_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", self.root)
        output = payload["events"][0]
        self.assertEqual(output["status"], "partial")
        self.assertIsNone(output["locations"][0]["rain_probability_pct"])
        self.assertIsNone(output["locations"][0]["mean_temperature_c"])
        serialized = (self.root / "data/gold/planner_data.json").read_text(encoding="utf-8")
        self.assertNotIn("NaN", serialized)
        self.assertNotIn("Infinity", serialized)

    def test_identity_conflict_never_exposes_another_events_locations(self):
        selected = event()
        directory = prepared_event(self.root, selected)
        report_path = directory / "pipeline_quality_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["event_id"] = "NASA-SE-09569"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "pending")
        self.assertEqual(output["locations"], [])
        self.assertIn("no coincide", output["error"])

    def test_javascript_export_escapes_html_and_has_same_payload(self):
        selected = event()
        prepared_event(self.root, selected, ("</script><script>alert('hola')</script> & Ciudad",))
        payload = product.export_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", self.root)
        script = (self.root / "data/gold/planner_data.js").read_text(encoding="utf-8")
        self.assertNotIn("</script>", script)
        self.assertNotIn("<", script)
        self.assertEqual(json.loads(script.removeprefix("window.ECLIPSE_PLANNER_DATA = ").strip().removesuffix(";")), payload)

    def test_old_forecast_is_stale_without_changing_prepared_report(self):
        selected = event()
        directory = prepared_event(self.root, selected)
        report_path = directory / "pipeline_quality_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["forecast"] = {"status": "available", "acquired_utc": "2001-01-01T00:00:00+00:00"}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        original = report_path.read_bytes()
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "ready")
        self.assertEqual(output["forecast_status"]["status"], "stale")
        self.assertEqual(output["forecast_acquired_utc"], "2001-01-01T00:00:00+00:00")
        self.assertEqual(report_path.read_bytes(), original)

    def test_expired_forecast_uses_saved_historical_scores_without_mutating_gold(self):
        selected = event()
        directory = prepared_event(self.root, selected, ("Ciudad A", "Ciudad B"))
        report_path = directory / "pipeline_quality_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["forecast"] = {"status": "available", "acquired_utc": "2001-01-01T00:00:00+00:00"}
        report["ranking_basis"] = "operational_forecast_proxy"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        ranking_path = directory / "destination_ranking.csv"
        ranking = pd.read_csv(ranking_path)
        ranking["ranking_basis"] = "operational_forecast_proxy"
        ranking["destination_rank"] = [2, 1]
        ranking["phase_priority"] = 3
        ranking["score"] = [0.8, 0.2]
        ranking.to_csv(ranking_path, index=False)
        original = ranking_path.read_bytes()
        output = self.build([selected])["events"][0]
        self.assertEqual([row["name"] for row in output["locations"]], ["Ciudad A", "Ciudad B"])
        self.assertEqual(output["forecast_status"]["status"], "stale")
        self.assertEqual(output["ranking_basis"], "historical_climatology")
        self.assertEqual(ranking_path.read_bytes(), original)
        ranking.drop(columns=["score"]).to_csv(ranking_path, index=False)
        output = self.build([selected])["events"][0]
        self.assertEqual([row["name"] for row in output["locations"]], ["Ciudad B", "Ciudad A"])
        self.assertEqual(output["ranking_basis"], "operational_forecast_proxy")
        self.assertIn("faltan métricas", output["ranking_basis_warning"])

    def test_standalone_cli_exports_window_from_today_without_network(self):
        catalog = pd.DataFrame([event("2027-02-06", number="09567"), event("2030-06-01", "lunar", "partial", "09720")])
        with patch.object(product.sources, "ROOT", self.root), patch("builtins.print"), \
                patch.object(product, "catalog_between", return_value=catalog) as between, \
                patch("requests.sessions.Session.request", side_effect=AssertionError("No red")):
            self.assertEqual(product.main([]), 0)
        start, end = product.planning_window(None, 3)
        _, catalog_end = product.planning_window(end, product.CATALOG_MARGIN_YEARS)
        self.assertEqual(between.call_args.args[:2], (start, catalog_end))
        self.assertTrue(between.call_args.args[4], "La exportación solo lee copias locales.")
        payload = json.loads((self.root / "data/gold/planner_data.json").read_text(encoding="utf-8"))
        self.assertEqual((payload["planning_start"], payload["planning_end_exclusive"]), (start, end))
        self.assertEqual(payload["catalog_end_exclusive"], catalog_end)

    def test_margin_events_after_window_are_exported_as_pending(self):
        catalog = pd.DataFrame([event(), event("2030-06-01", "lunar", "partial", "09720"),
            event("2031-10-06", "lunar", "partial", "09721")])
        payload = product.build_planner_data(catalog, "2026-10-06", "2029-10-06", self.root, "2031-10-06")
        self.assertEqual([row["event_date"] for row in payload["events"]], ["2027-08-02", "2030-06-01"])
        self.assertEqual(payload["events"][1]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "no superar"):
            product.build_planner_data(catalog, "2026-10-06", "2029-10-06", self.root, "2028-10-06")

    def test_standalone_cli_without_local_catalog_keeps_previous_export(self):
        directory = self.root / "data/gold"
        directory.mkdir(parents=True)
        previous = {"planner_data.json": '{"previous":true}\n', "planner_data.js": "window.ECLIPSE_PLANNER_DATA = {};\n"}
        for filename, content in previous.items():
            (directory / filename).write_text(content, encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(product.sources, "ROOT", self.root), redirect_stderr(stderr), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("No red")):
            with self.assertRaises(SystemExit) as raised:
                product.main([])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Falta una copia verificada", stderr.getvalue())
        for filename, content in previous.items():
            self.assertEqual((directory / filename).read_text(encoding="utf-8"), content)

    def test_standalone_cli_accepts_explicit_start_and_horizon(self):
        catalog = pd.DataFrame([event(), event("2028-01-12", "lunar", "partial", "09712")])
        with patch.object(product.sources, "ROOT", self.root), patch("builtins.print"), \
                patch.object(product, "catalog_between", return_value=catalog):
            self.assertEqual(product.main(["--as-of", "2027-10-06", "--future-years", "1"]), 0)
        output = json.loads((self.root / "data/gold/planner_data.json").read_text(encoding="utf-8"))
        self.assertEqual(output["planning_end_exclusive"], "2028-10-06")
        self.assertEqual([row["event_date"] for row in output["events"]], ["2028-01-12"])

    def test_nonmatching_family_and_invalid_date_are_rejected(self):
        selected = event()
        selected["event_family"] = "lunar"
        with self.assertRaisesRegex(ValueError, "no son compatibles"):
            self.build([selected])
        selected = event("2027-08-02/../../outside")
        with self.assertRaisesRegex(ValueError, "Fecha ISO"):
            self.build([selected])

    def test_countries_use_maximum_point_obscuration_and_preserve_prepared_climate(self):
        selected = event()
        prepared_event(self.root, selected)
        visible_candidates(self.root, selected, [
            {"name": "Ciudad principal", "country_code": "ES", "country_name": "España"},
            {"name": "Zona española", "country_code": "ES", "country_name": "España",
                "latitude": 37.0, "local_phase": "partial", "obscuration_fraction": 0.98},
            {"name": "Zona egipcia", "country_code": "EG", "country_name": "Egipto",
                "latitude": 27.0, "longitude": 31.0, "obscuration_fraction": 1.0},
            {"name": "Zona francesa", "country_code": "FR", "country_name": "Francia",
                "latitude": 43.0, "longitude": 3.0, "obscuration_fraction": 0.69},
            {"name": "Referencia en el mar", "candidate_type": "referencia astronómica",
                "country_code": "ES", "country_name": "España", "obscuration_fraction": 1.0}])
        with patch.object(product, "enrich_country", side_effect=declared_country):
            output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "ready")
        self.assertTrue(output["astronomy_ready"])
        self.assertEqual(len(output["locations"]), 4)
        self.assertEqual([row["country_code"] for row in output["countries"]], ["EG", "ES", "FR"])
        spain = output["countries"][1]
        self.assertEqual(spain["max_obscuration_pct"], 98.0)
        self.assertEqual(spain["location_count"], 2)
        self.assertEqual(spain["climate_location_count"], 1)
        main = output["locations"][0]
        self.assertEqual(main["climate_status"], "ready")
        self.assertEqual(main["rain_probability_pct"], 0.0)
        self.assertEqual(main["valid_cloud_years"], 30)
        astronomy = output["locations"][1]
        self.assertEqual(astronomy["climate_status"], "not_prepared")
        self.assertIsNone(astronomy["clear_probability_pct"])
        self.assertEqual(astronomy["phase_duration_s"], 7200.0)
        self.assertEqual(astronomy["duration_scope"], "local_visible")
        self.assertEqual(output["country_catalog_coverage"], {"catalog_status": "available",
            "location_count": 4, "climate_location_count": 1, "not_prepared_location_count": 3})

    def test_coordinates_are_required_to_merge_climate_even_for_same_name(self):
        selected = event()
        prepared_event(self.root, selected)
        visible_candidates(self.root, selected, [
            {"name": "Ciudad principal", "latitude": 36.000009, "longitude": -5.000009},
            {"name": "Ciudad principal", "latitude": 37.0,
                "clear_probability": 0.8, "rain_probability": 0.4,
                "valid_cloud_years": 30, "valid_rain_years": 30}])
        with patch.object(product, "enrich_country", side_effect=declared_country):
            output = self.build([selected])["events"][0]
        self.assertEqual(len(output["locations"]), 2)
        self.assertEqual(output["locations"][0]["clear_probability_pct"], 50.0)
        other = output["locations"][1]
        self.assertEqual(other["latitude"], 37.0)
        self.assertIsNone(other["clear_probability_pct"])
        self.assertIsNone(other["valid_cloud_years"])
        self.assertEqual(other["climate_by_day"], [])

    def test_lunar_astronomy_can_be_selected_while_climate_is_pending(self):
        selected = event("2028-12-31", "lunar", "total", "09714")
        visible_candidates(self.root, selected, [
            {"name": "Zona española", "country_code": "ES", "country_name": "España",
                "body_altitude_deg": 25.0, "obscuration_fraction": 1.0},
            {"name": "Zona egipcia", "country_code": "EG", "country_name": "Egipto",
                "latitude": 27.0, "longitude": 31.0, "body_altitude_deg": 50.0}])
        with patch.object(product, "enrich_country", side_effect=declared_country):
            output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "pending")
        self.assertTrue(output["astronomy_ready"])
        self.assertIn("climatología", output["error"])
        self.assertEqual([row["country_code"] for row in output["countries"]], ["EG", "ES"])
        self.assertTrue(all(row["max_obscuration_pct"] is None for row in output["countries"]))
        self.assertTrue(all(row["climate_location_count"] == 0 for row in output["countries"]))
        for location in output["locations"]:
            self.assertIsNone(location["obscuration_pct"])
            self.assertIsNone(location["clear_probability_pct"])
            self.assertEqual(location["phase_duration_s"], 3600.0)
            self.assertEqual(location["duration_scope"], "global_lunar_phase")

    def test_mismatched_silver_warns_without_hiding_valid_gold(self):
        selected = event()
        prepared_event(self.root, selected)
        visible_candidates(self.root, selected, [{"name": "Evento diferente", "event_id": "NASA-SE-09569"}])
        with patch.object(product, "enrich_country", side_effect=declared_country):
            output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "ready")
        self.assertEqual([row["name"] for row in output["locations"]], ["Ciudad principal"])
        self.assertEqual(output["country_catalog_coverage"]["catalog_status"], "invalid")
        self.assertIn("no coincide", output["warnings"][0])

    def test_invalid_coordinate_rejects_entire_silver_catalogue(self):
        selected = event()
        prepared_event(self.root, selected)
        visible_candidates(self.root, selected, [
            {"name": "Zona aparentemente válida"}, {"name": "Coordenadas dañadas", "latitude": 100.0}])
        with patch.object(product, "enrich_country", side_effect=declared_country):
            output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "ready")
        self.assertEqual([row["name"] for row in output["locations"]], ["Ciudad principal"])
        self.assertIn("coordenadas", output["warnings"][0])

    def test_export_packages_the_local_map_without_network(self):
        selected = event()
        prepared_event(self.root, selected)
        raw_map = self.root / geography.RAW_GEOGRAPHY
        raw_map.parent.mkdir(parents=True)
        raw_map.write_text(json.dumps({"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"ISO_A2_EH": "ES", "NAME": "Spain", "NAME_ES": "España"},
            "geometry": {"type": "Polygon", "coordinates": [[[-10., 30.], [5., 30.], [5., 45.], [-10., 45.], [-10., 30.]]]}}]}),
            encoding="utf-8")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("La cartografía es local")):
            payload = product.export_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", self.root)
        script = (self.root / "app/world-map.js").read_text(encoding="utf-8")
        world_map = json.loads(script.removeprefix("window.ECLIPSE_WORLD_MAP = ").removesuffix(";\n"))
        self.assertEqual(payload["map_status"], {"status": "available", "feature_count": 1})
        self.assertEqual(world_map["features"][0]["properties"], {"country_code": "ES", "country_name": "España"})
        self.assertEqual(payload["events"][0]["countries"][0]["country_code"], "ES")
        saved = json.loads((self.root / "data/gold/planner_data.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, payload)

    def test_export_without_cartography_still_writes_a_valid_selector_snapshot(self):
        selected = event()
        prepared_event(self.root, selected)
        with patch.object(geography.sources, "ROOT", self.root), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("No descarga cartografía")):
            payload = product.export_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", self.root)
        self.assertEqual(payload["map_status"]["status"], "unavailable")
        self.assertEqual(payload["events"][0]["status"], "ready")
        self.assertEqual(len(payload["events"][0]["locations"]), 1)
        self.assertFalse((self.root / "app/world-map.js").exists())
        self.assertTrue((self.root / "data/gold/planner_data.js").is_file())
        saved = json.loads((self.root / "data/gold/planner_data.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, payload)


    def test_legacy_report_without_configuration_is_not_presented_as_prepared(self):
        selected = event()
        directory = prepared_event(self.root, selected)
        report_path = directory / "pipeline_quality_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        del report["preparation_config"]
        report_path.write_text(json.dumps(report), encoding="utf-8")
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "pending")
        self.assertEqual(output["locations"], [])
        self.assertIn("versión anterior", output["error"])

    def test_short_history_is_exported_as_partial_and_provisional(self):
        selected = event()
        prepared_event(self.root, selected, years=2)
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "partial")
        self.assertIn("al menos 15", output["error"])
        self.assertTrue(output["locations"][0]["ranking_provisional"])

    def test_failed_update_marks_previous_results_as_partial(self):
        selected = event()
        directory = prepared_event(self.root, selected)
        marker = directory / product.FAILURE_MARKER
        marker.write_text(json.dumps({"failed_at_utc": "2026-10-07T08:00:00+00:00", "error": "HTTP 429"}), encoding="utf-8")
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "partial")
        self.assertIn("falló", output["error"])
        self.assertEqual(len(output["locations"]), 1)
        # Un fallo anterior a la preparación vigente ya no la afecta.
        marker.write_text(json.dumps({"failed_at_utc": "2026-10-05T08:00:00+00:00"}), encoding="utf-8")
        self.assertEqual(self.build([selected])["events"][0]["status"], "ready")

    def test_interface_reads_the_exported_solar_threshold(self):
        payload = self.build([event()])
        self.assertEqual(payload["solar_obscuration_threshold_pct"], product.SOLAR_OBSCURATION_THRESHOLD_PCT)
        script = (Path(product.__file__).resolve().parents[1] / "app/app.js").read_text(encoding="utf-8")
        self.assertIn("data.solar_obscuration_threshold_pct", script)
        self.assertIn(f": {product.SOLAR_OBSCURATION_THRESHOLD_PCT};", script)
        self.assertNotRegex(script, r"\b60 %")

    def test_duration_outside_polynomial_window_is_not_missing_climate(self):
        selected = event("2028-01-26", "solar", "partial", "09569")
        directory = prepared_event(self.root, selected)
        ranking = pd.read_csv(directory / "destination_ranking.csv")
        ranking["visible_eclipse_duration_s"] = np.nan
        ranking["phase_duration_s"] = np.nan
        ranking["duration_complete"] = False
        ranking.to_csv(directory / "destination_ranking.csv", index=False)
        output = self.build([selected])["events"][0]
        self.assertEqual(output["status"], "ready")
        self.assertIsNone(output["locations"][0]["phase_duration_s"])
        self.assertFalse(output["locations"][0]["duration_complete"])
        # Sin esa explicación geométrica, la duración ausente sigue siendo un dato incompleto.
        ranking["duration_complete"] = True
        ranking.to_csv(directory / "destination_ranking.csv", index=False)
        self.assertEqual(self.build([selected])["events"][0]["status"], "partial")

if __name__ == "__main__":
    unittest.main()
