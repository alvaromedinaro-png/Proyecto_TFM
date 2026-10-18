"""Controles de integridad de entrada y reproducibilidad del Gold."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from src import prepare_eclipse_data as preparation


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.original_reader = pd.read_csv
        self.weather = pd.read_csv(preparation.ROOT / "data/legacy/bronze/weather_history_2027-08-02.csv")
        # Las salidas van a un directorio temporal: los tests no modifican data/.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)

    def reader(self, path, *args, **kwargs):
        if str(path).endswith("weather_history_2027-08-02.csv"):
            return self.weather.copy()
        return self.original_reader(path, *args, **kwargs)

    def test_contradictory_duplicate_stops_preparation(self):
        duplicate = self.weather.iloc[[0]].copy()
        duplicate["cloud_cover_pct"] = 99.0
        self.weather = pd.concat([self.weather, duplicate], ignore_index=True)
        with patch.object(preparation.pd, "read_csv", side_effect=self.reader):
            with self.assertRaisesRegex(ValueError, "contradictorias"):
                preparation.prepare(output=self.output)

    def test_inconsistent_year_stops_preparation(self):
        self.weather.loc[0, "year"] = 2000
        with patch.object(preparation.pd, "read_csv", side_effect=self.reader):
            with self.assertRaisesRegex(ValueError, "Fecha meteorológica"):
                preparation.prepare(output=self.output)

    def test_wrong_hour_stops_preparation(self):
        self.weather.loc[0, "weather_time_utc"] = "1997-08-02T00:00"
        with patch.object(preparation.pd, "read_csv", side_effect=self.reader):
            with self.assertRaisesRegex(ValueError, "hora UTC"):
                preparation.prepare(output=self.output)

    def test_gold_is_reproducible_and_labels_match(self):
        first, sites, report = preparation.prepare(output=self.output)
        second, _, second_report = preparation.prepare(output=self.output)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(report, second_report)
        self.assertTrue(first.sample_id.is_unique)
        self.assertTrue(first.clear_sky_proxy.eq(first.cloud_cover_pct.le(20).astype(int)).all())
        self.assertEqual(len(sites), first.name.nunique())


if __name__ == "__main__":
    unittest.main()
