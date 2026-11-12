"""Climatología ERA5 del CDS para zonas sin Open-Meteo: mismas reglas, caché y exportación."""
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import zipfile
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import eclipse_era5_cds as era5
from src import eclipse_product as product

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_eclipse_product import declared_country, event, prepared_event, visible_candidates  # noqa: E402


def series(stamps, cloud=0.1, rain_m=0.0):
    return pd.DataFrame({"valid_time": pd.DatetimeIndex(stamps), "u10": 3.0, "v10": 4.0, "t2m": 293.15,
                         "tcc": cloud, "tp": rain_m, "latitude": 36.0, "longitude": -5.25})


class Era5RulesTests(unittest.TestCase):
    def test_cell_is_the_nearest_era5_grid_point(self):
        self.assertEqual(era5.cell(35.889, -5.319), (36.0, -5.25))
        self.assertEqual(era5.cell(-12.1, 179.9), (-12.0, -180.0))

    def test_coastal_zone_uses_the_nearest_land_cell(self):
        mask = np.zeros((721, 1440))
        # Solo es tierra la celda 35,75 / −5,5, como la que usa Open-Meteo para Ceuta.
        mask[int(round((90 - 35.75) / 0.25)), int(round((-5.5 % 360) / 0.25))] = 1.0
        self.assertEqual(era5.cell(35.889, -5.319, mask), (35.75, -5.5))
        self.assertEqual(era5.cell(35.889, -5.319, np.zeros((721, 1440))), (36.0, -5.25))

    def test_cloud_uses_nearest_hour_and_rain_the_interval_that_contains_the_peak(self):
        zone = {"name": "Ceuta", "event_time_utc_approx": "2027-08-02T08:47:24+00:00"}
        stamps = pd.to_datetime(["2024-08-02T09:00Z", "2025-08-02T09:00Z"])
        frame = series(stamps, cloud=[0.15, 0.6], rain_m=[0.0002, 0.0])
        records = era5.zone_samples(frame, zone, "2027-08-02", [2024, 2025], 0, 0, 0.1)
        self.assertEqual([record["clear_sky_proxy"] for record in records], [1.0, 0.0])
        self.assertEqual([record["rain_proxy"] for record in records], [1.0, 0.0])
        self.assertAlmostEqual(records[0]["temperature_2m"], 20.0)
        self.assertAlmostEqual(records[0]["wind_speed_10m"], 18.0)
        summary = era5.summarize(pd.DataFrame(records))
        self.assertEqual(summary.loc[0, "clear_probability"], 0.5)
        self.assertEqual(summary.loc[0, "valid_cloud_years"], 2)

    def test_request_covers_every_year_in_a_single_date_range(self):
        zones = pd.DataFrame([{"name": "Ceuta", "latitude": 35.889, "longitude": -5.319,
                               "event_time_utc_approx": "2027-08-02T08:47:24+00:00", "cell": (36.0, -5.25)}])
        request, stamps = era5.cell_request(zones, "2027-08-02", [1997, 2026], 2, 2)
        self.assertEqual(request["date"], ["1997-07-31/2026-08-04"])
        self.assertEqual(request["location"], {"latitude": 36.0, "longitude": -5.25})
        self.assertEqual(len(stamps), 2 * 5)  # 09:00 es a la vez la hora más cercana y el fin del intervalo

    def test_download_keeps_only_used_hours_and_reuses_the_cache_offline(self):
        stamps = list(pd.to_datetime(["2024-08-02T09:00Z"]))
        full = series(pd.date_range("2024-08-01", periods=72, freq="h", tz="UTC"))
        full["valid_time"] = full.valid_time.dt.strftime("%Y-%m-%d %H:%M:%S")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("serie.csv", full.to_csv(index=False))

        class Client:
            def __init__(self, **kwargs):
                pass

            def retrieve(self, dataset, request, target):
                Path(target).write_bytes(archive.getvalue())

        request = {"variable": era5.VARIABLES, "location": {"latitude": 36.0, "longitude": -5.25},
                   "date": ["2024-08-01/2024-08-03"], "data_format": "csv"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {"cdsapi": types.SimpleNamespace(Client=Client)}):
            path = era5.retrieve_cell(request, stamps, Path(temp) / "cell.csv")
            kept = pd.read_csv(path)
            metadata = json.loads(path.with_suffix(".csv.metadata.json").read_text(encoding="utf-8"))
            again = era5.retrieve_cell(request, stamps, Path(temp) / "cell.csv", offline=True)
        self.assertEqual(len(kept), 1)
        self.assertEqual(metadata["original_rows"], 72)
        self.assertEqual(again, path)


class Era5ExportTests(unittest.TestCase):
    def test_zone_without_open_meteo_gets_cds_climate_and_its_source(self):
        selected = event()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = prepared_event(root, selected)
            visible_candidates(root, selected, [
                {"name": "Ciudad principal"},
                {"name": "Zona nueva", "latitude": 35.9, "longitude": -5.3},
                {"name": "Zona sin datos", "latitude": 35.0, "longitude": -2.0}])
            pd.DataFrame([{"event_id": selected["event_id"], "event_date": selected["event_date"], "name": "Zona nueva",
                "latitude": 35.9, "longitude": -5.3, "day_offset": offset, "clear_probability": 0.6,
                "rain_probability": 0.1, "cloud_probability": 0.4, "valid_cloud_years": 30, "valid_rain_years": 30,
                "mean_temperature_c": 25.0, "mean_wind_speed_kmh": 12.0, "mean_cloud_cover_pct": 20.0}
                for offset in (-1, 0, 1)]).to_csv(directory / "era5_zone_climate.csv", index=False)
            with patch.object(product, "enrich_country", side_effect=declared_country):
                output = product.build_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", root)["events"][0]
        by_name = {location["name"]: location for location in output["locations"]}
        self.assertEqual(by_name["Ciudad principal"]["climate_source"], "open_meteo_era5")
        new = by_name["Zona nueva"]
        self.assertEqual(new["climate_source"], "copernicus_cds_era5")
        self.assertEqual(new["clear_probability_pct"], 60.0)
        self.assertEqual(new["climate_status"], "ready")
        self.assertEqual(len(new["climate_by_day"]), 3)
        self.assertEqual(by_name["Zona sin datos"]["climate_status"], "not_prepared")
        self.assertNotIn("climate_source", by_name["Zona sin datos"])


if __name__ == "__main__":
    unittest.main()
