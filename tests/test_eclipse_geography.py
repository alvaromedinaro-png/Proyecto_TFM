"""Países de las ciudades NASA y cartografía offline sin inferencias de vecindad."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from src import eclipse_geography as geography
from src import eclipse_sources as sources


class GeographyTests(unittest.TestCase):
    def city(self, name, lat, lon, region="na"):
        return geography.enrich_country({"name": name, "latitude": lat, "longitude": lon,
            "location_source": f"https://eclipse.gsfc.nasa.gov/JSEX/city-{region}.js"})

    def test_us_state_and_canadian_province_are_not_iso_country_codes(self):
        self.assertEqual(self.city("Los Angeles, CA", 34.05, -118.25)["country_code"], "US")
        self.assertEqual(self.city("Calgary, AB", 51.05, -114.08)["country_code"], "CA")
        explicit = geography.enrich_country({"name": "Destino canadiense", "latitude": 51., "longitude": -114., "country_code": "CA"})
        self.assertEqual(explicit["country_code"], "CA")

    def test_historical_names_and_territories_have_unambiguous_codes(self):
        cases = [("Ponta Delgada, AZORES", "PT"), ("Las Palmas, G. Canaria, CANARY ISLANDS", "ES"),
                 ("Kaliningrad, KALININGRAD", "RU"), ("Oranjestad, Aruba, NETHERLANDS ANTILLES", "AW"),
                 ("Willemstad, Curacao, NETHERLANDS ANTILLES", "CW"), ("Beograd, SERBIA AND MONTENEGRO", "RS")]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self.city(name, 0, 0, "eu")["country_code"], expected)

    def test_source_csv_preserves_enclave_and_small_territory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "places.csv"
            csv_path.write_text("name,latitude,longitude,country_code\nMelilla,35.2923,-2.9381,ES\nGibraltar,36.1408,-5.3536,GI\n", encoding="utf-8")
            for name, lat, lon, expected in [("Melilla", 35.2923, -2.9381, "ES"), ("Gibraltar", 36.1408, -5.3536, "GI")]:
                output = geography.enrich_country({"name": name, "latitude": lat, "longitude": lon, "location_source": str(csv_path)}, root)
                self.assertEqual(output["country_code"], expected)
                self.assertEqual(output["country_source"], "source_csv")

    def test_polygon_fallback_does_not_assign_nearest_country_to_ocean(self):
        inland = geography.enrich_country({"name": "Observatorio", "latitude": 40.4168, "longitude": -3.7038})
        self.assertEqual(inland["country_code"], "ES")
        self.assertEqual(inland["country_source"], "approximate_polygon")
        offshore = geography.enrich_country({"name": "Punto oceánico", "latitude": 0., "longitude": -140.})
        self.assertEqual(offshore["country_code"], "UNKNOWN")

    def test_all_saved_nasa_cities_resolve_without_network(self):
        rows = pd.read_csv(sources.ROOT / "data/silver/nasa_cities.csv").to_dict("records")
        rows = [{key: value for key, value in row.items() if not key.startswith("country_")} for row in rows]
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Sin red")):
            resolved = [geography.enrich_country(row) for row in rows]
        self.assertEqual(len(resolved), 688)
        self.assertTrue(all(row["country_code"] != "UNKNOWN" for row in resolved))

    def test_world_map_is_local_json_with_country_codes(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.sessions.Session.request", side_effect=AssertionError("Sin red")):
            root = Path(directory)
            payload = geography.export_world_map(root)
            script = (root / "app/world-map.js").read_text(encoding="utf-8")
            decoded = json.loads(script.removeprefix("window.ECLIPSE_WORLD_MAP = ").removesuffix(";\n"))
        self.assertEqual(decoded, payload)
        self.assertEqual(len(payload["features"]), 177)
        self.assertTrue(any(feature["properties"]["country_code"] == "EG" for feature in payload["features"]))


if __name__ == "__main__":
    unittest.main()
