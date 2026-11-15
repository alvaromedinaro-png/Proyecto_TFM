"""Campos del MVP: hora local, rango al 95 % y categoría de cada probabilidad."""
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import pandas as pd

from src import eclipse_product as product
from src import eclipse_sources as sources
from src import ml_planning_experiment as ml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_eclipse_product import declared_country, event, prepared_event  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def gazetteer(root, places):
    """cities15000.zip mínimo: columnas de GeoNames con latitud, longitud, país y zona horaria."""
    lines = []
    for index, (name, latitude, longitude, country, zone) in enumerate(places, 1):
        fields = [""] * 19
        fields[0], fields[1], fields[4], fields[5], fields[8], fields[17] = str(index), name, str(latitude), str(longitude), country, zone
        lines.append("\t".join(fields))
    path = root / product.GAZETTEER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("cities15000.txt", "\n".join(lines) + "\n")
    product._timezone_index.cache_clear()


class MvpFieldsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        product._timezone_index.cache_clear()

    def tearDown(self):
        product._timezone_index.cache_clear()
        self.temporary.cleanup()

    def export(self, selected):
        with patch.object(product, "enrich_country", side_effect=declared_country):
            return product.build_planner_data(pd.DataFrame([selected]), "2026-10-06", "2029-10-06", self.root)["events"][0]

    def test_bands_use_the_thresholds_of_the_machine_learning_delivery(self):
        self.assertEqual(product.PROBABILITY_BANDS, ml.BANDS)
        self.assertEqual([product._band(value) for value in (0.0, 0.249, 0.25, 0.5, 1.0)], ["baja", "baja", "media", "alta", "alta"])
        self.assertIsNone(product._band(None))

    def test_range_comes_from_gold_or_the_same_wilson_interval(self):
        selected = event()
        directory = prepared_event(self.root, selected)
        ranking = pd.read_csv(directory / "destination_ranking.csv")
        ranking["clear_lower95"], ranking["clear_upper95"] = 0.3321, 0.6679
        ranking.to_csv(directory / "destination_ranking.csv", index=False)
        location = self.export(selected)["locations"][0]
        self.assertEqual(location["clear_range_pct"], [33.2, 66.8])
        self.assertEqual(location["clear_band"], "alta")
        # Sin columnas en el Gold (zonas de Copernicus) se calcula igual: 0 de 30 años con lluvia.
        lower, upper = sources.wilson_interval(0, 30)
        self.assertEqual(location["rain_range_pct"], [round(lower * 100, 1), round(upper * 100, 1)])
        self.assertEqual(location["rain_band"], "baja")

    def test_zone_without_climate_has_no_range_or_band(self):
        selected = event()
        prepared_event(self.root, selected, years=0)
        location = self.export(selected)["locations"][0]
        self.assertIsNone(location["clear_range_pct"])
        self.assertIsNone(location["clear_band"])

    def test_timezone_is_the_nearest_geonames_place_of_the_same_country(self):
        gazetteer(self.root, [("Al otro lado", 36.0, -5.01, "GI", "Europe/Gibraltar"),
                              ("Cercana", 36.2, -5.2, "ES", "Europe/Madrid"),
                              ("Lejana", 28.0, -16.0, "ES", "Atlantic/Canary")])
        self.assertEqual(product._timezone(36.0, -5.0, "ES", self.root), "Europe/Madrid")
        self.assertEqual(product._timezone(36.0, -5.0, "UNKNOWN", self.root), "Europe/Gibraltar")
        self.assertIsNone(product._timezone(0.0, -30.0, "UNKNOWN", self.root))
        selected = event()
        prepared_event(self.root, selected)
        self.assertEqual(self.export(selected)["locations"][0]["timezone"], "Europe/Madrid")

    def test_without_gazetteer_the_timezone_is_null(self):
        selected = event()
        prepared_event(self.root, selected)
        self.assertIsNone(self.export(selected)["locations"][0]["timezone"])

    def test_interface_shows_local_time_range_and_band(self):
        script = (ROOT / "app/app.js").read_text(encoding="utf-8")
        for field in ("location.timezone", "location.clear_range_pct", "location.rain_range_pct", "location.clear_band"):
            self.assertIn(field, script)


if __name__ == "__main__":
    unittest.main()
