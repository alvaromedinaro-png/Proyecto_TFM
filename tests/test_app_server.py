"""Servidor local de la app: validación, emparejamiento de localidad y precios, sin red."""
from pathlib import Path
from urllib.parse import parse_qs
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from src import app_server as server


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeClient:
    """Responde como Xotelo y registra las llamadas."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if url.endswith("/api/search"):
            # Formato real de la búsqueda geográfica: primero un homónimo lejano, que debe descartarse.
            return FakeResponse({"error": None, "result": {"list": [
                {"hotel_key": None, "location_key": "g999", "name": "Malaga", "latitude": 41.9, "longitude": -74.0},
                {"hotel_key": None, "location_key": "g187438", "name": "Malaga", "place_name": "Andalucia, Spain",
                 "latitude": 36.718468, "longitude": -4.422782}]}})
        if url.endswith("/api/list"):
            return FakeResponse({"error": None, "result": {"list": [
                {"name": "Cerca", "key": "g187438-d1", "accommodation_type": "Hotel",
                 "geo": {"latitude": 36.7215, "longitude": -4.4215}, "review_summary": {"rating": 4.5, "count": 10},
                 "url": "https://www.tripadvisor.com/Hotel_Review-g187438-d1.html"},
                {"name": "Lejos", "key": "g187438-d2", "geo": {"latitude": 37.5, "longitude": -4.4}},
                {"name": "Sin tarifa", "key": "g187438-d3", "geo": {"latitude": 36.73, "longitude": -4.42}}]}})
        if url.endswith("/api/rates"):
            rates = [] if params["hotel_key"] == "g187438-d3" else [
                {"code": "BookingCom", "name": "Booking.com", "rate": 120}, {"code": "Agoda", "name": "Agoda.com", "rate": 95}]
            return FakeResponse({"error": None, "result": {"rates": rates}})
        raise AssertionError(url)


def query(**overrides):
    values = {"name": "Málaga", "country": "España", "lat": "36.7213", "lon": "-4.4214",
              "checkin": "2027-08-01", "checkout": "2027-08-03", "radius_km": "8"}
    values.update(overrides)
    return parse_qs("&".join(f"{key}={value}" for key, value in values.items()))


class AppServerTest(unittest.TestCase):
    def setUp(self):
        server._rates_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "locations.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_query_validates_dates_and_coordinates(self):
        params = server.parse_query(query())
        self.assertEqual((params["checkin"], params["checkout"]), ("2027-08-01", "2027-08-03"))
        for bad in ({"lat": "95"}, {"checkin": "2027-13-01"}, {"checkout": "2027-08-01"}, {"name": ""}):
            with self.assertRaises(ValueError):
                server.parse_query(query(**bad))
        self.assertEqual(server.parse_query(query(radius_km="900"))["radius_km"], server.MAX_RADIUS_KM)

    def test_location_key_is_taken_from_any_field(self):
        self.assertEqual(server.location_key({"location_key": "g187438"}), "g187438")
        self.assertEqual(server.location_key({"url": "https://x/Tourism-g187438-Malaga.html"}), "g187438")
        self.assertIsNone(server.location_key({"name": "sin clave"}))

    def test_prices_filter_by_distance_and_take_cheapest_offer(self):
        client = FakeClient()
        service = server.PriceService("clave", client, self.cache)
        result = service.prices(server.parse_query(query()))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["location"]["key"], "g187438")
        self.assertEqual([hotel["name"] for hotel in result["hotels"]], ["Cerca", "Sin tarifa"])
        self.assertEqual(result["hotels"][0]["price"]["min"], 95)
        self.assertEqual(result["hotels"][0]["price"]["provider"], "Agoda.com")
        self.assertIsNone(result["hotels"][1]["price"])
        self.assertEqual(result["nights"], 2)
        rates = [params for url, params, _ in client.calls if url.endswith("/api/rates")]
        self.assertTrue(all(params["currency"] == "EUR" and params["adults"] == 2 for params in rates))
        # La clave solo viaja a RapidAPI, nunca a los endpoints libres.
        self.assertTrue(all("X-RapidAPI-Key" not in headers for url, _, headers in client.calls if "rapidapi" not in url))

    def test_disproportionate_rates_are_flagged(self):
        hotels = [{"price": {"min": 733}}, {"price": {"min": 7911}}, {"price": None}, {"price": {"error": True}}]
        server.mark_outliers(hotels)
        self.assertFalse(hotels[0]["price"]["outlier"])
        self.assertTrue(hotels[1]["price"]["outlier"])
        normal = [{"price": {"min": 90}}, {"price": {"min": 300}}]
        server.mark_outliers(normal)
        self.assertFalse(any(hotel["price"]["outlier"] for hotel in normal))  # caro, pero por debajo de 500 €

    def test_location_search_is_cached_on_disk(self):
        service = server.PriceService("clave", FakeClient(), self.cache)
        service.prices(server.parse_query(query()))
        client = FakeClient()
        server.PriceService(None, client, self.cache).prices(server.parse_query(query()))
        self.assertFalse(any(url.endswith("/api/search") for url, _, _ in client.calls))

    def test_hotel_search_is_the_fallback_and_failures_are_cached(self):
        class GeoFails(FakeClient):
            def get(self, url, params=None, headers=None, timeout=None):
                if url.endswith("/api/search"):
                    self.calls.append((url, dict(params or {}), dict(headers or {})))
                    if params.get("location_type") == "geo" or params["query"] == "Nada":
                        return FakeResponse({"error": {"message": "Failed to search data"}, "result": None})
                    return FakeResponse({"error": None, "result": {"list": [
                        {"name": "Hotel en Portugal", "location_key": "g189180", "latitude": 41.1, "longitude": -8.6},
                        {"name": "Parador De Ceuta", "location_key": "g187504", "latitude": 35.888, "longitude": -5.316}]}})
                return super().get(url, params, headers, timeout)
        client = GeoFails()
        service = server.PriceService("clave", client, self.cache)
        self.assertEqual(service.find_location("Ceuta", "España", 35.889, -5.321)["key"], "g187504")
        self.assertIsNone(service.find_location("Nada", "España", 35.0, -5.0))
        searches = len(client.calls)
        self.assertIsNone(service.find_location("Nada", "España", 35.0, -5.0))
        self.assertEqual(len(client.calls), searches)  # el fallo reciente no gasta otra búsqueda

    def test_without_key_reports_it(self):
        self.assertEqual(server.PriceService(None, FakeClient(), self.cache).prices(server.parse_query(query()))["status"], "no_key")

    def test_only_the_app_is_served(self):
        server.Handler.service = server.PriceService(None, FakeClient(), self.cache)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/app/index.html") as response:
                self.assertEqual(response.status, 200)
            with urllib.request.urlopen(f"{base}/api/status") as response:
                self.assertIn(b'"prices": false', response.read())
            for path in ("/src/app_server.py", "/requirements.txt", "/data/gold/batch_acquisition_report.json"):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + path)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f"{base}/api/prices?name=&lat=1&lon=1")
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
