"""Servidor local de la app: sirve el selector y consulta precios de alojamiento para las fechas del eclipse.

    python src/app_server.py            # abre http://127.0.0.1:8000/app/index.html

La app sigue funcionando abierta como archivo; este servidor solo añade los precios, que el
navegador no puede pedir directamente a Xotelo (no admite peticiones de otras páginas).

Flujo de /api/prices para un destino:
1. Localidad de TripAdvisor del destino: búsqueda de Xotelo en RapidAPI (requiere clave gratuita).
   Es la única llamada con clave y se guarda en caché en disco, así que se hace una vez por destino.
2. Alojamientos de esa localidad con coordenadas: /api/list de Xotelo (libre).
3. Tarifa por habitación y noche para las fechas, adultos y habitaciones elegidos: /api/rates de Xotelo (libre).
   Que una agencia publique tarifa indica disponibilidad orientativa; no distingue «completo» de «aún sin precio».

La clave se lee de la variable de entorno RAPIDAPI_KEY o del archivo ~/.rapidapi_key, nunca del repositorio.
Solo se envían a los servicios el nombre del destino, sus coordenadas, las fechas y el número de personas.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import argparse
import json
import math
import os
import re
import threading
import time
import webbrowser

import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE_FILE = ROOT / ".cache" / "xotelo_locations.json"
XOTELO_URL = "https://data.xotelo.com/api"
RAPIDAPI_HOST = "xotelo-hotel-prices.p.rapidapi.com"
USER_AGENT = "TFM-Eclipse-Planner/2.0 (academic project)"
CURRENCY = "EUR"
PRICED_HOTELS = 16          # tarifas consultadas por destino
MIN_REVIEWS = 20            # con opiniones suele haber tarifas; los apartamentos sin ellas rara vez las publican
LIST_LIMIT = 100            # máximo que admite /api/list
MAX_RADIUS_KM = 25
MAX_ADULTS = 8
MAX_ROOMS = 4
# Fotos de los alojamientos: solo se reenvían las del CDN de TripAdvisor que devuelve Xotelo.
IMAGE_PREFIX = "https://dynamic-media-cdn.tripadvisor.com/"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PHOTO_WIDTHS = (300, 500, 700)  # anchos que acepta el CDN al redimensionar (otros devuelven 400)
MAX_LOCATION_KM = 60        # distancia máxima entre el destino y la localidad de TripAdvisor
FAILED_LOCATION_TTL_S = 24 * 3600  # una localidad no encontrada se vuelve a buscar al día siguiente
RATES_TTL_S = 6 * 3600      # las tarifas cambian: se reutilizan como mucho seis horas
# Solo se sirven la app y su exportación, no el resto del proyecto (código, entorno, cachés).
STATIC_PREFIXES = ("/app/", "/data/gold/planner_data.js")

_lock = threading.Lock()
_rates_cache = {}


def rapidapi_key():
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".rapidapi_key"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def distance_km(lat1, lon1, lat2, lon2):
    rad = math.pi / 180
    a = (math.sin((lat2 - lat1) * rad / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin((lon2 - lon1) * rad / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def location_key(item):
    """Clave de localidad de TripAdvisor (g + dígitos) en un resultado de búsqueda geográfica."""
    for field in ("location_key", "geo_key", "key", "hotel_key", "url"):
        match = re.search(r"\bg\d+\b", str(item.get(field) or "")) if isinstance(item, dict) else None
        if match:
            return match.group(0)
    return None


def parse_query(query):
    """Valida los parámetros de /api/prices; lanza ValueError con un mensaje para el usuario."""
    def one(name):
        values = query.get(name) or [""]
        return values[0].strip()
    name = one("name")[:120]
    country = one("country")[:80]
    if not name:
        raise ValueError("Falta el nombre del destino.")
    try:
        lat, lon = float(one("lat")), float(one("lon"))
    except ValueError:
        raise ValueError("Coordenadas no válidas.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Coordenadas fuera de rango.")
    try:
        checkin, checkout = date.fromisoformat(one("checkin")), date.fromisoformat(one("checkout"))
    except ValueError:
        raise ValueError("Fechas no válidas (AAAA-MM-DD).") from None
    if not 1 <= (checkout - checkin).days <= 30:
        raise ValueError("La estancia debe durar entre 1 y 30 noches.")
    try:
        adults = int(one("adults") or 2)
        rooms = int(one("rooms") or 1)
    except ValueError:
        raise ValueError("Número de adultos o habitaciones no válido.") from None
    if not 1 <= adults <= MAX_ADULTS:
        raise ValueError(f"Los adultos deben ser entre 1 y {MAX_ADULTS}.")
    if not 1 <= rooms <= min(MAX_ROOMS, adults):
        raise ValueError(f"Las habitaciones deben ser entre 1 y {min(MAX_ROOMS, adults)} (no más que adultos).")
    try:
        radius = min(MAX_RADIUS_KM, max(1.0, float(one("radius_km") or 8)))
    except ValueError:
        radius = 8.0
    return {"name": name, "country": country, "lat": lat, "lon": lon,
            "checkin": checkin.isoformat(), "checkout": checkout.isoformat(), "radius_km": radius,
            "adults": adults, "rooms": rooms}


def nearest_location(items, name, lat, lon, from_hotel=False):
    """Localidad del resultado más cercano al destino, descartando los que están a más de
    MAX_LOCATION_KM (un homónimo en otro país daría precios de otro lugar)."""
    candidates = []
    for position, item in enumerate(items):
        key = location_key({"location_key": item.get("location_key")}) if from_hotel else location_key(item)
        if not key:
            continue
        try:
            distance = distance_km(lat, lon, float(item["latitude"]), float(item["longitude"]))
        except (KeyError, TypeError, ValueError):
            if from_hotel:
                continue  # un hotel sin coordenadas no permite comprobar que es del destino
            distance = float("inf")  # localidad sin coordenadas: se acepta en el orden de la búsqueda
        if distance != float("inf") and distance > MAX_LOCATION_KM:
            continue
        place = item.get("short_place_name") if from_hotel else item.get("place_name")
        candidates.append((distance, position, {"key": key, "name": name if from_hotel else item.get("name") or name,
                                                "place": place or ""}))
    return min(candidates)[2] if candidates else None


def mark_outliers(hotels, factor=4, floor=500):
    """Marca tarifas desproporcionadas: más de `factor` veces la más barata de otro alojamiento
    y más de `floor` por noche. Para fechas aún no abiertas a reserva, algunas agencias devuelven
    importes de este tipo (p. ej. 7.911 € frente a 733 € en el mismo centro de Málaga)."""
    priced = [hotel["price"] for hotel in hotels if isinstance(hotel.get("price"), dict) and "min" in hotel["price"]]
    for price in priced:
        others = [other["min"] for other in priced if other is not price]
        price["outlier"] = bool(others) and price["min"] > max(floor, factor * min(others))


class PriceService:
    def __init__(self, key=None, client=None, cache_file=CACHE_FILE):
        self.key = key
        self.client = client or requests.Session()
        self.client.headers["User-Agent"] = USER_AGENT
        self.cache_file = cache_file
        try:
            self.locations = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.locations = {}

    def _get(self, url, params, headers=None, timeout=20):
        response = self.client.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        return body.get("result") or {}

    def _save_locations(self):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(self.locations, ensure_ascii=False, indent=1), encoding="utf-8")

    def find_location(self, name, country, lat=None, lon=None):
        """Clave de localidad de TripAdvisor para el destino, con caché en disco.

        Si hay varios resultados se elige el más cercano al destino, y se descartan los que quedan
        a más de MAX_LOCATION_KM: un homónimo en otro país daría precios de otro lugar."""
        cache_key = f"{name}|{country}".lower()
        with _lock:
            cached = self.locations.get(cache_key)
        if cached and cached.get("key"):
            return cached
        # Un fallo reciente no se repite: cada búsqueda gasta cuota de RapidAPI aunque falle.
        if cached and time.time() - cached.get("failed_at", 0) < FAILED_LOCATION_TTL_S:
            return None
        if not self.key:
            return None
        # 1) Búsqueda geográfica («Málaga, España»). 2) Si falla o no hay nada cerca, búsqueda de
        # alojamientos por nombre: cada hotel trae la clave de su localidad (así se encuentran Ceuta
        # o Gibraltar, que la búsqueda geográfica no devuelve).
        attempts = [({"query": f"{name}, {country}" if country else name, "location_type": "geo"}, False),
                    ({"query": name}, True)]
        found = None
        for params, from_hotel in attempts:
            try:
                result = self._get(f"https://{RAPIDAPI_HOST}/api/search", params,
                                   headers={"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": RAPIDAPI_HOST})
            except RuntimeError:
                continue  # Xotelo responde «Failed to search data» a algunas consultas
            found = nearest_location(result.get("list") or [], name, lat, lon, from_hotel)
            if found:
                break
        with _lock:
            self.locations[cache_key] = found or {"key": None, "failed_at": time.time()}
            self._save_locations()
        return found

    def nearby_hotels(self, location, lat, lon, radius_km):
        result = self._get(f"{XOTELO_URL}/list", {"location_key": location["key"], "limit": LIST_LIMIT,
                                                  "offset": 0, "sort": "distance"})
        hotels = []
        for item in result.get("list") or []:
            geo = item.get("geo") or {}
            try:
                h_lat, h_lon = float(geo["latitude"]), float(geo["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            distance = distance_km(lat, lon, h_lat, h_lon)
            if distance > radius_km or not item.get("key"):
                continue
            review = item.get("review_summary") or {}
            image = item.get("image") if str(item.get("image") or "").startswith(IMAGE_PREFIX) else ""
            hotels.append({
                "name": item.get("name") or "", "key": item["key"], "type": item.get("accommodation_type") or "",
                "lat": h_lat, "lon": h_lon, "distance_km": round(distance, 2),
                "rating": review.get("rating"), "reviews": review.get("count"), "url": item.get("url") or "",
                "image": image})
        return sorted(hotels, key=lambda hotel: hotel["distance_km"])

    def rates(self, hotel_key, checkin, checkout, adults=2, rooms=1):
        """Tarifa más baja por habitación y noche entre las agencias, o None si ninguna la publica."""
        cache_key = (hotel_key, checkin, checkout, adults, rooms)
        with _lock:
            cached = _rates_cache.get(cache_key)
        if cached and time.time() - cached[0] < RATES_TTL_S:
            return cached[1]
        result = self._get(f"{XOTELO_URL}/rates", {"hotel_key": hotel_key, "chk_in": checkin, "chk_out": checkout,
                                                   "currency": CURRENCY, "adults": adults, "rooms": rooms})
        offers = sorted(({"provider": rate.get("name") or rate.get("code") or "", "rate": rate["rate"]}
                         for rate in result.get("rates") or [] if isinstance(rate.get("rate"), (int, float))),
                        key=lambda offer: offer["rate"])
        value = {"min": offers[0]["rate"], "provider": offers[0]["provider"], "offers": offers[:4]} if offers else None
        with _lock:
            _rates_cache[cache_key] = (time.time(), value)
        return value

    def prices(self, params):
        cached = self.locations.get(f"{params['name']}|{params['country']}".lower()) or {}
        if not self.key and not cached.get("key"):
            return {"status": "no_key"}
        location = self.find_location(params["name"], params["country"], params["lat"], params["lon"])
        if not location:
            return {"status": "no_location"}
        hotels = self.nearby_hotels(location, params["lat"], params["lon"], params["radius_km"])
        if not hotels:
            # Si la localidad encontrada no tiene alojamientos cerca del punto, probablemente no es la correcta.
            return {"status": "no_hotels", "location": location}
        # Primero los alojamientos con opiniones (casi siempre tienen tarifas) y, dentro, los más cercanos.
        selected = sorted(hotels, key=lambda hotel: ((hotel["reviews"] or 0) < MIN_REVIEWS, hotel["distance_km"]))[:PRICED_HOTELS]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda hotel: self._safe_rates(hotel, params), selected))
        checkin = date.fromisoformat(params["checkin"])
        nights = (date.fromisoformat(params["checkout"]) - checkin).days
        for hotel, price in zip(selected, results):
            if price and "min" in price:
                # Total de la estancia: tarifa por habitación y noche × habitaciones × noches.
                price = {**price, "total": price["min"] * params["rooms"] * nights}
            hotel["price"] = price
            hotel["available"] = bool(price and "min" in price)
        mark_outliers(selected)
        return {"status": "ok", "currency": CURRENCY, "nights": nights, "location": location,
                "checkin": params["checkin"], "checkout": params["checkout"],
                "adults": params["adults"], "rooms": params["rooms"],
                "hotels": selected, "total_nearby": len(hotels),
                "days_ahead": (checkin - datetime.now(timezone.utc).date()).days,
                "retrieved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def photo(self, url, width):
        """Foto de un alojamiento a través del servidor: el navegador no siempre puede cargarla
        directamente del CDN. Solo se aceptan direcciones de IMAGE_PREFIX (no es un proxy abierto)."""
        if not url.startswith(IMAGE_PREFIX) or any(char in url for char in "?#@\\") or ".." in url:
            raise ValueError("Foto no válida.")
        width = min(PHOTO_WIDTHS, key=lambda allowed: abs(allowed - width))
        response = self.client.get(url, params={"w": width, "h": -1, "s": 1}, timeout=20, stream=True)
        if response.status_code == 400:  # si no admite el tamaño, la original
            response = self.client.get(url, timeout=20, stream=True)
        response.raise_for_status()
        kind = response.headers.get("Content-Type", "")
        if not kind.startswith("image/"):
            raise ValueError("La respuesta no es una imagen.")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_IMAGE_BYTES:
                raise ValueError("Foto demasiado grande.")
        return kind, bytes(body)

    def _safe_rates(self, hotel, params):
        try:
            return self.rates(hotel["key"], params["checkin"], params["checkout"], params["adults"], params["rooms"])
        except (requests.RequestException, RuntimeError, ValueError, KeyError):
            return {"error": True}


class Handler(SimpleHTTPRequestHandler):
    service = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format, *args):  # noqa: A002 - firma de la clase base
        pass

    def _json(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_response(302)
            self.send_header("Location", "/app/index.html")
            self.end_headers()
            return
        if url.path == "/api/status":
            self._json(200, {"prices": bool(self.service.key), "currency": CURRENCY})
            return
        if url.path == "/api/prices":
            try:
                params = parse_query(parse_qs(url.query))
            except ValueError as error:
                self._json(400, {"status": "invalid", "message": str(error)})
                return
            try:
                self._json(200, self.service.prices(params))
            except (requests.RequestException, RuntimeError, ValueError) as error:
                self._json(502, {"status": "error", "message": type(error).__name__})
            return
        if url.path == "/api/photo":
            query = parse_qs(url.query)
            try:
                width = int((query.get("w") or ["500"])[0])
                kind, body = self.service.photo((query.get("url") or [""])[0], width)
            except ValueError:
                self.send_error(400)
                return
            except requests.RequestException:
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if not url.path.startswith(STATIC_PREFIXES):
            self.send_error(404)
            return
        super().do_GET()


def main():
    parser = argparse.ArgumentParser(description="Servidor local del planificador con precios de alojamiento.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador.")
    args = parser.parse_args()
    Handler.service = PriceService(rapidapi_key())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    address = f"http://127.0.0.1:{args.port}/app/index.html"
    print(f"Planificador en {address}")
    print("Precios de alojamiento: " + ("activados." if Handler.service.key else
          "sin clave de RapidAPI (RAPIDAPI_KEY o ~/.rapidapi_key); se muestran solo los enlaces de búsqueda."))
    print("Ctrl + C para salir.")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
