"""Acceso a las fuentes: caché verificada, cuotas de Open-Meteo y lectura de tablas NASA."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
import hashlib
import json
import re
import time

from bs4 import BeautifulSoup
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]

CATALOG_URL = "https://eclipse.gsfc.nasa.gov/SEcat5/SE2001-2100.html"
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"
MONTHS = {month: index for index, month in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
TYPES = {"T": "total", "A": "annular", "P": "partial", "H": "hybrid"}
# Cuotas gratuitas de Open-Meteo: 600 llamadas/minuto, 5.000/hora y 10.000/día.
# Cada ubicación cuenta como una llamada y se multiplica por cada bloque de 10
# variables y de 14 días solicitados.
HOURLY_CALL_LIMIT = 5000
_last_network_request = [0.0]


def save_json(path, value):
    def clean(item):
        if isinstance(item, dict):
            return {str(key): clean(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(value) for value in item]
        if isinstance(item, np.generic):
            return clean(item.item())
        if item is pd.NA or item is pd.NaT or isinstance(item, float) and not np.isfinite(item):
            return None
        return item
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8", newline="\n")


def save_csv(frame, path):
    """Fin de línea LF en cualquier sistema: el hash registrado coincide con el archivo entregado."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n")


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_sha256(folder=None):
    """Versión del código que genera los datos, independiente del fin de línea del checkout."""
    folder = Path(folder or Path(__file__).resolve().parent)
    return {path.name: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for path in sorted(folder.glob("eclipse_*.py"))}


class RateLimitExceeded(requests.RequestException):
    """Cuota diaria agotada: continuar solo acumularía fallos hasta el día siguiente."""


def weighted_calls(locations, variables, days):
    return max(1, locations) * max(1, -(-variables // 10)) * max(1, -(-days // 14))


def request_interval(locations, variables, days):
    """Pausa mínima entre peticiones para no superar la cuota horaria."""
    return weighted_calls(locations, variables, days) * 3600 / HOURLY_CALL_LIMIT


def session():
    client = requests.Session()
    client.headers["User-Agent"] = "TFM-Eclipse-Planner/2.0 (academic data acquisition)"
    # 429 se gestiona en fetch para distinguir la cuota por minuto, hora o día.
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    client.mount("https://", HTTPAdapter(max_retries=retry))
    return client


def _rate_limit_wait(response):
    try:
        reason = str(response.json().get("reason", ""))
    except ValueError:
        reason = response.text[:200]
    lowered = reason.lower()
    if "daily" in lowered or "día" in lowered:
        raise RateLimitExceeded(f"Cuota diaria de la API agotada: {reason or 'HTTP 429'}. "
                                "Vuelve a ejecutar más tarde; las respuestas guardadas se reutilizan.")
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return int(retry_after), reason
    return (3600 if "hour" in lowered else 60), reason


def fetch(url, path, client, params=None, refresh=False, offline=False, min_interval=0.0):
    """Cache exacta del cuerpo y de la petición, con hash y fecha de adquisición."""
    signature = {"source_url": url, "params": params or {}}
    metadata_file = path.with_suffix(path.suffix + ".metadata.json")
    if path.exists() and not refresh and metadata_file.exists():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        if (all(metadata.get(k) == v for k, v in signature.items())
                and metadata.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()):
            return path.read_bytes()
    if offline:
        raise ValueError(f"Falta una copia verificada de {path}. Adquiere primero los datos con red.")
    rate_limited = 0
    attempt = 0
    while attempt < 3:
        # Solo las peticiones reales esperan; las copias en caché no consumen cuota.
        wait = min_interval - (time.monotonic() - _last_network_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_network_request[0] = time.monotonic()
        response = client.get(url, params=params, timeout=45)
        if response.status_code == 429:
            rate_limited += 1
            seconds, reason = _rate_limit_wait(response)
            if rate_limited > 3:
                raise requests.HTTPError(f"Límite de peticiones persistente: {reason or 'HTTP 429'}", response=response)
            print(f"Límite de peticiones alcanzado; nueva prueba en {seconds} s.", flush=True)
            time.sleep(seconds)
            continue
        attempt += 1
        response.raise_for_status()
        # Algunos errores transitorios llegan con HTTP 200 y cuerpo vacío; tampoco se cachean.
        try:
            payload = response.json() if path.suffix == ".json" else None
        except ValueError:
            if attempt == 3:
                raise ValueError("La API devolvió tres respuestas JSON vacías o inválidas.")
            time.sleep(0.5 * attempt)
            continue
        if isinstance(payload, dict) and payload.get("error"):
            raise ValueError(payload.get("reason", "Error de la API"))
        if path.suffix == ".html" and "<html" not in response.text.lower():
            raise ValueError("La fuente no devolvió un documento HTML.")
        break
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    save_json(metadata_file, {**signature, "resolved_url": response.url,
        "acquired_utc": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(response.content).hexdigest(), "status_code": response.status_code})
    return response.content


def coordinate(token):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([NSEW])", token)
    if not match:
        raise ValueError(f"Coordenada no reconocida: {token}")
    return float(match[1]) * (-1 if match[2] in "SW" else 1)


def duration_seconds(token):
    match = re.fullmatch(r"(\d+)m(\d+(?:\.\d+)?)s", token)
    return int(match[1]) * 60 + float(match[2]) if match else np.nan


def parse_catalog(html, source_url=CATALOG_URL):
    """Lee filas y enlaces originales; conserva TD y aproxima UT usando ΔT."""
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for block in soup.find_all("pre"):
        for line in block.decode_contents().splitlines():
            row = BeautifulSoup(line, "html.parser")
            tokens = row.get_text().split()
            if not tokens or not re.fullmatch(r"\d{5}", tokens[0]):
                continue
            if len(tokens) < 15:
                raise ValueError(f"Fila incompleta del catálogo: {tokens}")
            date = f"{int(tokens[1]):04d}-{MONTHS[tokens[2]]:02d}-{int(tokens[3]):02d}"
            if tokens[8][0] not in TYPES:
                raise ValueError(f"Tipo de eclipse desconocido: {tokens[8]}")
            links = {anchor.get("target"): urljoin(source_url, anchor["href"])
                     for anchor in row.find_all("a", href=True)}
            # Las efemérides llegan a 3000; pandas puede convertir la aritmética a
            # nanosegundos (límite en 2262). Mantener aquí fechas de Python e ISO.
            td = datetime.fromisoformat(f"{date}T{tokens[4]}").replace(tzinfo=timezone.utc)
            delta_t = float(tokens[5])
            records.append({
                "event_id": f"NASA-SE-{tokens[0]}", "event_date": date,
                "catalog_number": tokens[0], "eclipse_type": TYPES[tokens[8][0]],
                "type_code": tokens[8], "greatest_eclipse_td": tokens[4],
                "delta_t_s": delta_t,
                "greatest_eclipse_ut_approx": (td - timedelta(seconds=delta_t)).isoformat(),
                "saros_number": int(tokens[7]), "gamma": float(tokens[10]),
                "eclipse_magnitude": float(tokens[11]),
                "greatest_latitude": coordinate(tokens[12]),
                "greatest_longitude": coordinate(tokens[13]),
                "sun_altitude_deg": float(tokens[14]),
                "path_width_km": float(tokens[15]) if len(tokens) > 15 and tokens[15] != "-" else np.nan,
                "central_duration_at_greatest_s": duration_seconds(tokens[16]) if len(tokens) > 16 else np.nan,
                "source_url": source_url, "map_url": links.get("MAP"),
                "path_url": links.get("PATH"), "besselian_url": links.get("BES"),
                "geometry_supported": tokens[8] == "T" and bool(links.get("PATH")),
            })
    catalog = pd.DataFrame(records)
    if catalog.empty or catalog.event_id.duplicated().any() or catalog.event_date.duplicated().any():
        raise ValueError("Catálogo vacío o identificadores/fechas duplicados.")
    if not (catalog.greatest_latitude.between(-90, 90).all()
            and catalog.greatest_longitude.between(-180, 180).all()):
        raise ValueError("Coordenadas del catálogo fuera de rango.")
    return catalog.sort_values("event_date").reset_index(drop=True)


def parse_path(html):
    """Tabla NASA: límites, línea central, UT, altura solar, anchura y duración."""
    soup = BeautifulSoup(html, "html.parser")
    coordinate_pattern = re.compile(
        r"(\d{1,2})\s+(\d{1,2}(?:\.\d+)?)([NS])\s+"
        r"(\d{1,3})\s+(\d{1,2}(?:\.\d+)?)([EW])")
    records = []
    for block in soup.find_all("pre"):
        for row in block.get_text().splitlines():
            time_match = re.match(r"^\s*(\d{2}):(\d{2})\s+", row)
            points = list(coordinate_pattern.finditer(row))
            duration = re.search(r"\d+m\d+(?:\.\d+)?s", row)
            if not time_match or len(points) != 3 or not duration:
                continue  # límites sin hora y filas de entrada/salida sin línea central
            record = {"time_utc": int(time_match[1]) * 60 + int(time_match[2])}
            for label, point in zip(["north", "south", "central"], points):
                lat_d, lat_m, ns, lon_d, lon_m, ew = point.groups()
                latitude = (float(lat_d) + float(lat_m) / 60) * (-1 if ns == "S" else 1)
                longitude = (float(lon_d) + float(lon_m) / 60) * (-1 if ew == "W" else 1)
                record["latitude" if label == "central" else f"{label}_latitude"] = latitude
                record["longitude" if label == "central" else f"{label}_longitude"] = longitude
            tail = row[points[-1].end():duration.start()].split()
            if len(tail) != 4:
                raise ValueError(f"Columnas astronómicas no reconocidas: {row}")
            record.update({"diameter_ratio": float(tail[0]), "sun_altitude_deg": float(tail[1]),
                           "sun_azimuth_deg": float(tail[2]), "path_width_km": float(tail[3]),
                           "duration_center_s": duration_seconds(duration[0])})
            records.append(record)
    path = pd.DataFrame(records)
    if len(path) < 3:
        raise ValueError("La fuente no contiene una trayectoria central interpretable.")
    # Mantener el orden cronológico de la fuente cuando el evento cruza medianoche.
    minutes = path.time_utc.to_numpy(copy=True)
    for index in range(1, len(minutes)):
        while minutes[index] < minutes[index - 1]:
            minutes[index] += 1440
    path["time_utc"] = minutes
    if path.time_utc.duplicated().any() or path.isna().any().any():
        raise ValueError("Trayectoria con valores ausentes o tiempos duplicados.")
    if not (path.latitude.between(-90, 90).all() and path.longitude.between(-180, 180).all()
            and path.path_width_km.ge(0).all() and path.duration_center_s.ge(0).all()
            and path.path_width_km.gt(0).any()):
        raise ValueError("Dimensiones o coordenadas de trayectoria inválidas.")
    return path


def historical_years(event_date, start=None, end=None, as_of=None):
    """Años de referencia; con as_of, la selección no depende del día de ejecución."""
    event = datetime.fromisoformat(event_date)
    available_until = datetime.now(timezone.utc).date() - timedelta(days=5)
    if as_of is not None:
        available_until = min(available_until, datetime.fromisoformat(as_of).date() - timedelta(days=5))
    latest_year = min(event.year - 1, available_until.year)
    # Para eventos lejanos, usar climatología reciente disponible, no décadas futuras.
    while latest_year >= 1940:
        try:
            latest_day = datetime(latest_year, event.month, event.day).date()
        except ValueError:
            latest_year -= 1
            continue
        if latest_day <= available_until:
            break
        latest_year -= 1
    end = end if end is not None else latest_year
    start = start if start is not None else max(1940, min(end, latest_year) - 29)
    if start < 1940 or end >= event.year or start > end:
        raise ValueError("El histórico debe comenzar en 1940 o después y finalizar antes del año del eclipse.")
    years = []
    for year in range(start, end + 1):
        try:
            day = datetime(year, event.month, event.day).date()
        except ValueError:
            continue
        if day <= available_until:
            years.append(year)
    if not years:
        raise ValueError("La ventana solicitada no tiene fechas históricas disponibles.")
    return years


def wilson_interval(successes, total):
    """Intervalo binomial nominal del 95 %; no corrige dependencia entre años."""
    if total <= 0:
        return np.nan, np.nan
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * np.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)
