"""Cobertura astronómica ampliada con GeoNames: zonas de la franja central que NASA no lista."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

import pandas as pd

from src import eclipse_locations as locations
from src import eclipse_pipeline as pipeline
from src import eclipse_sources as sources

EVENT_DATE = "2027-08-02"


def total_eclipse():
    root = sources.ROOT
    elements = json.loads((root / "data/bronze/events" / EVENT_DATE / "besselian.parsed.json").read_text(encoding="utf-8"))
    catalog = pd.read_csv(root / "data/silver/planning_eclipse_catalog.csv")
    event = catalog.loc[catalog.event_date.eq(EVENT_DATE)].iloc[0].to_dict()
    return elements, event


def place(name, latitude, longitude, country, population, geonames_id):
    return {"name": name, "latitude": latitude, "longitude": longitude, "elevation_m": 0.0,
        "elevation_is_assumed": True, "country_code": country, "population": population,
        "geonames_id": str(geonames_id), "candidate_type": locations.GAZETTEER_TYPE,
        "location_source": locations.GAZETTEER_URL}


class GazetteerTests(unittest.TestCase):
    def test_central_places_missing_from_nasa_are_added_with_totality(self):
        elements, event = total_eclipse()
        gazetteer = pd.DataFrame([
            place("Ceuta", 35.889, -5.319, "ES", 83179, 1),
            place("Málaga", 36.720, -4.420, "ES", 592346, 2),
            place("Sevilla", 37.383, -5.973, "ES", 703206, 3),
            place("Madrid", 40.417, -3.704, "ES", 3255944, 4),
            place("Oslo", 59.913, 10.739, "NO", 580000, 5)])
        known = pd.DataFrame([{"name": "Sevilla, SPAIN", "latitude": 37.383, "longitude": -5.983, "country_code": "ES"}])
        found = locations.gazetteer_candidates(elements, event, gazetteer, known)
        by_name = found.set_index("name")
        self.assertEqual(by_name.loc["Ceuta", "local_phase"], "total")
        self.assertEqual(by_name.loc["Ceuta", "country_code"], "ES")
        self.assertGreater(by_name.loc["Ceuta", "central_duration_s"], 240)
        self.assertEqual(by_name.loc["Málaga", "local_phase"], "total")
        # Sevilla ya está en la lista NASA; Oslo no llega al 70 % de ocultación.
        self.assertNotIn("Sevilla", by_name.index)
        self.assertNotIn("Oslo", by_name.index)
        # La totalidad va antes que la fase parcial y, dentro de ella, la mayor duración.
        self.assertEqual(list(found.name[:2]), ["Ceuta", "Málaga"])
        self.assertEqual(by_name.loc["Madrid", "local_phase"], "partial")

    def test_screen_agrees_with_detailed_calculation(self):
        elements, event = total_eclipse()
        points = [(35.889, -5.319), (36.720, -4.420), (40.417, -3.704), (26.467, 31.800)]
        screened = locations.screen_solar_magnitude(elements, event, *zip(*points), [0.0] * len(points))
        for (latitude, longitude), magnitude in zip(points, screened):
            detailed = locations.solar_circumstances(elements, event, latitude, longitude, 0.0)
            self.assertAlmostEqual(magnitude, detailed["local_magnitude"], delta=0.005)

    def test_each_country_keeps_longest_and_largest_places_only(self):
        elements, event = total_eclipse()
        # Puntos a lo largo de la franja central en el sur de España, más que el límite por país.
        rows = [place(f"Lugar {index}", 36.0 + index * 0.02, -5.6 + index * 0.08, "ES", 1000 * (index + 1), index)
                for index in range(20)]
        found = locations.gazetteer_candidates(elements, event, pd.DataFrame(rows), pd.DataFrame())
        limit = locations.GAZETTEER_LONGEST_PER_COUNTRY + locations.GAZETTEER_LARGEST_PER_COUNTRY
        self.assertLessEqual(len(found), limit)
        self.assertTrue(found.name.is_unique)
        self.assertIn("Lugar 19", set(found.name))  # la más poblada se conserva aunque dure menos

    def test_climate_is_added_only_where_geonames_improves_the_phase(self):
        shortlist = pd.DataFrame([
            {"name": "Sevilla, SPAIN", "country_code": "ES", "phase_priority": 1, "candidate_type": "ciudad NASA"},
            {"name": "Luxor, EGYPT", "country_code": "EG", "phase_priority": 3, "candidate_type": "ciudad NASA"},
            {"name": "Referencia", "country_code": "UNKNOWN", "phase_priority": 3, "candidate_type": "referencia astronómica"}])
        extra = pd.DataFrame([
            {**place("Ceuta", 35.9, -5.3, "ES", 83179, 1), "phase_priority": 3, "central_duration_s": 288.0},
            {**place("Tarifa", 36.0, -5.6, "ES", 18169, 2), "phase_priority": 3, "central_duration_s": 278.0},
            {**place("Málaga", 36.7, -4.4, "ES", 592346, 3), "phase_priority": 3, "central_duration_s": 111.0},
            {**place("Madrid", 40.4, -3.7, "ES", 3255944, 4), "phase_priority": 1, "central_duration_s": 0.0},
            {**place("Sohag", 26.6, 31.7, "EG", 200000, 5), "phase_priority": 3, "central_duration_s": 382.0},
            {**place("Faro", 37.0, -7.9, "PT", 40000, 6), "phase_priority": 1, "central_duration_s": 0.0}])
        chosen = locations.climate_supplement(shortlist, extra)
        # España: la de mayor duración y la más poblada en totalidad. Egipto ya tenía totalidad;
        # Portugal solo tiene fase parcial, que no justifica nuevas descargas.
        self.assertEqual(list(chosen.name), ["Ceuta", "Málaga"])

    def test_new_climate_batch_keeps_previous_requests_and_cache(self):
        event = {"event_id": "TEST", "event_date": "2027-08-02", "event_family": "solar"}
        base = pd.DataFrame([{"name": "Sevilla, SPAIN", "latitude": 37.38, "longitude": -5.98,
            "event_time_utc_approx": "2027-08-02T08:47:00+00:00"}])
        extra = pd.DataFrame([{"name": "Ceuta", "latitude": 35.89, "longitude": -5.32,
            "event_time_utc_approx": "2027-08-02T08:47:00+00:00"}])
        both = pd.concat([base.assign(climate_batch=locations.CLIMATE_BATCH_BASE),
                          extra.assign(climate_batch=locations.CLIMATE_BATCH_GAZETTEER)], ignore_index=True)
        requests = []

        def fake_fetch(url, path, client, params=None, refresh=False, offline=False, min_interval=0.0):
            requests.append((path.name, params["latitude"]))
            times = pd.date_range(params["start_date"], pd.Timestamp(params["end_date"]) + pd.Timedelta(23, unit="h"), freq="h")
            variables = params["hourly"].split(",")
            units = {variable: "unit" for variable in variables}
            units.update({"cloud_cover": "%", "rain": "mm", "precipitation": "mm"})
            body = {"utc_offset_seconds": 0, "latitude": 36.0, "longitude": -5.0, "elevation": 10, "hourly_units": units,
                "hourly": {"time": [value.strftime("%Y-%m-%dT%H:%M") for value in times],
                           **{variable: [0.0] * len(times) for variable in variables}}}
            return json.dumps(body).encode()

        with tempfile.TemporaryDirectory() as temp, patch.object(sources, "fetch", side_effect=fake_fetch):
            pipeline.climate_window(event, base, Mock(), [2024], root=Path(temp), offline=True)
            alone = list(requests)
            requests.clear()
            _, samples, report = pipeline.climate_window(event, both, Mock(), [2024], root=Path(temp), offline=True)
        self.assertEqual(requests[0], alone[0])  # misma petición y mismo archivo de caché para Sevilla
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1][1], "35.890000")
        self.assertEqual(set(samples.name), {"Sevilla, SPAIN", "Ceuta"})
        self.assertEqual(report["failures"], [])

    def test_load_gazetteer_reads_geonames_dump(self):
        lines = [
            "2519240\tCeuta\tCeuta\t\t35.88919\t-5.31979\tP\tPPLA\tES\t\t\t\t\t\t83179\t\t22\tAfrica/Ceuta\t2024-01-01",
            "1\tSin país\tSin pais\t\t10\t10\tP\tPPL\t\t\t\t\t\t\t20000\t\t-9999\tUTC\t2024-01-01",
            "2\tFuera\tFuera\t\t95\t10\tP\tPPL\tFR\t\t\t\t\t\t20000\t150\t\tUTC\t2024-01-01"]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(locations.GAZETTEER_MEMBER, "\n".join(lines) + "\n")
        with patch.object(sources, "fetch", return_value=archive.getvalue()) as fetch:
            places = locations.load_gazetteer(Mock(), Path("."), offline=True)
        self.assertEqual(fetch.call_args.args[0], locations.GAZETTEER_URL)
        self.assertEqual(list(places.name), ["Ceuta"])
        self.assertEqual(places.iloc[0].elevation_m, 22)
        self.assertFalse(places.iloc[0].elevation_is_assumed)
        self.assertEqual(places.iloc[0].population, 83179)


if __name__ == "__main__":
    unittest.main()
