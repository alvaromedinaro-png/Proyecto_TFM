"""Catálogo NASA, selección por fecha y preparación de astronomía y clima por evento."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
import argparse
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

try:
    from .prepare_eclipse_data import ROOT, nearest_central_line_point, save_json
except ImportError:
    from prepare_eclipse_data import ROOT, nearest_central_line_point, save_json

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


def load_catalog(client=None, refresh=False, offline=False, root=ROOT):
    client = client or session()
    raw_file = root / "data/bronze/nasa/solar_catalog_2001_2100.html"
    html = fetch(CATALOG_URL, raw_file, client, refresh=refresh, offline=offline)
    catalog = parse_catalog(html)
    # Flujo anterior (--legacy): sus salidas no se mezclan con las del producto.
    for layer in ["silver", "gold"]:
        folder = root / "data/legacy" / layer
        folder.mkdir(parents=True, exist_ok=True)
        catalog.to_csv(folder / "eclipse_catalog.csv", index=False)
    save_json(root / "data/legacy/gold/eclipse_catalog.metadata.json", {
        "source_url": CATALOG_URL, "coverage": [2001, 2100], "rows": len(catalog),
        "type_counts": catalog.eclipse_type.value_counts().to_dict(),
        "raw_sha256": hashlib.sha256(html).hexdigest(),
        "attribution": "Eclipse Predictions by Fred Espenak, NASA's GSFC",
        "time_scale": "Catalog time is TD; UT is approximated by subtracting delta_t_s."})
    return catalog


def select_event(catalog, event_date):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_date):
        raise ValueError("La fecha debe tener formato YYYY-MM-DD.")
    datetime.fromisoformat(event_date)  # comprobar fecha válida antes de crear rutas
    selected = catalog.loc[catalog.event_date.eq(event_date)]
    if selected.empty:
        raise ValueError(f"No existe un eclipse solar catalogado para {event_date} (cobertura 2001–2100).")
    return selected.iloc[0].to_dict()


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


def build_locations(path, event, locations_file=None):
    if locations_file:
        locations = pd.read_csv(locations_file)
        required = {"name", "latitude", "longitude"}
        if not required.issubset(locations):
            raise ValueError(f"El CSV de ubicaciones requiere {sorted(required)}.")
        locations = locations[list(sorted(required))].copy()
        locations["candidate_type"] = "destino"
    else:
        indices = np.unique(np.linspace(0, len(path) - 1, 5, dtype=int))
        locations = path.iloc[indices][["latitude", "longitude"]].copy().reset_index(drop=True)
        locations["name"] = [f"Referencia central {index + 1}" for index in range(len(locations))]
        locations["candidate_type"] = "linea central"
    for column in ["latitude", "longitude"]:
        locations[column] = pd.to_numeric(locations[column], errors="coerce")
    if (locations.empty or locations["name"].isna().any() or locations["name"].duplicated().any()
            or not locations.latitude.between(-90, 90).all() or not locations.longitude.between(-180, 180).all()):
        raise ValueError("Ubicaciones vacías, duplicadas o coordenadas inválidas.")
    circumstances = pd.DataFrame([nearest_central_line_point(row.latitude, row.longitude, path)
                                  for row in locations.itertuples()], index=locations.index)
    locations = pd.concat([locations, circumstances], axis=1)
    half_width = locations.path_width_km / 2
    locations["eligible_totality"] = locations.distance_km.lt(half_width)
    locations["estimated_totality_s"] = locations.duration_center_s * np.sqrt(
        (1 - (locations.distance_km / half_width) ** 2).clip(lower=0))
    locations["event_id"] = event["event_id"]
    locations["event_date"] = event["event_date"]
    # UT de la tabla se trata como UTC aproximado; no es una hora civil local.
    locations["event_time_utc_approx"] = [
        (datetime.fromisoformat(event["event_date"]).replace(tzinfo=timezone.utc)
         + timedelta(minutes=float(value))).isoformat()
        for value in locations.time_utc_min]
    return locations.reset_index(drop=True)


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


def parse_weather(payload, locations, historical_date, event, rain_threshold=0.1):
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(locations):
        raise ValueError("La API devolvió un número de ubicaciones distinto del solicitado.")
    records, missing = [], []
    for location, item in zip(locations.itertuples(), items):
        if item.get("utc_offset_seconds") != 0:
            raise ValueError("La meteorología no está expresada en UTC.")
        hourly, units = item.get("hourly", {}), item.get("hourly_units", {})
        for variable, unit in [("cloud_cover", "%"), ("precipitation", "mm"), ("rain", "mm")]:
            if units.get(variable) != unit or len(hourly.get(variable, [])) != len(hourly.get("time", [])):
                raise ValueError(f"Unidad o longitud inválida para {variable}.")
        timestamps = pd.to_datetime(hourly.get("time", []), utc=True)
        if len(timestamps) != 24 or timestamps.duplicated().any() or not timestamps.is_monotonic_increasing:
            raise ValueError("La respuesta no contiene 24 horas ordenadas y únicas.")
        if not all(timestamps[index] - timestamps[index - 1] == pd.Timedelta(1, unit="h")
                   for index in range(1, len(timestamps))):
            raise ValueError("La respuesta no tiene granularidad horaria regular.")
        if not all(stamp.strftime("%Y-%m-%d") == historical_date for stamp in timestamps):
            raise ValueError("La respuesta meteorológica corresponde a otra fecha.")
        event_minute = location.time_utc_min % 1440
        desired = pd.Timestamp(historical_date, tz="UTC") + pd.Timedelta(event_minute, unit="m")
        cloud_index = int(np.argmin(abs(timestamps - desired)))
        # precipitación/lluvia son acumulados de la HORA PRECEDENTE: elegir intervalo que contiene el máximo.
        rain_indices = np.flatnonzero((timestamps - pd.Timedelta(1, unit="h") < desired) & (timestamps >= desired))
        if len(rain_indices) != 1 or abs(timestamps[cloud_index] - desired) > pd.Timedelta(30, unit="m"):
            raise ValueError("Falta la hora necesaria para muestrear el evento; ajustar ventana de descarga.")
        rain_index = int(rain_indices[0])
        cloud = hourly["cloud_cover"][cloud_index]
        precipitation, rain = hourly["precipitation"][rain_index], hourly["rain"][rain_index]
        if cloud is None or precipitation is None or rain is None:
            missing.append({"name": location.name, "date": historical_date, "reason": "missing_hourly_value"})
            continue
        cloud, precipitation, rain = float(cloud), float(precipitation), float(rain)
        if not (np.isfinite([cloud, precipitation, rain]).all() and 0 <= cloud <= 100
                and precipitation >= 0 and 0 <= rain <= precipitation + 0.11):
            raise ValueError("Valores meteorológicos fuera de rango o lluvia superior a precipitación.")
        if not (-90 <= item["latitude"] <= 90 and -180 <= item["longitude"] <= 180):
            raise ValueError("Coordenadas efectivas de la rejilla fuera de rango.")
        records.append({"event_id": event["event_id"], "event_date": event["event_date"],
            "name": location.name, "year": int(historical_date[:4]),
            "latitude": location.latitude, "longitude": location.longitude,
            "grid_latitude": item["latitude"], "grid_longitude": item["longitude"],
            "grid_elevation_m": item.get("elevation"),
            "weather_time_utc": timestamps[cloud_index].isoformat(),
            "rain_interval_end_utc": timestamps[rain_index].isoformat(),
            "cloud_cover_pct": float(cloud), "precipitation_mm": float(precipitation), "rain_mm": float(rain),
            "clear_sky_proxy": int(cloud <= 20), "rain_proxy": int(rain >= rain_threshold),
            "weather_model_requested": "era5"})
    return records, missing


def acquire_event(event, client=None, locations_file=None, start=None, end=None,
                  rain_threshold=0.1, refresh=False, offline=False, root=ROOT):
    if not event["geometry_supported"]:
        raise ValueError(f"Evento {event['event_date']} ({event['eclipse_type']}) catalogado; "
                         "la geometría implementada admite eclipses solares totales centrales con tabla NASA.")
    if not np.isfinite(rain_threshold) or rain_threshold <= 0:
        raise ValueError("El umbral de lluvia debe ser positivo y finito.")
    client = client or session()
    day = event["event_date"]
    bronze = root / "data/bronze/events" / day
    path_file = bronze / "nasa_path.html"
    html = fetch(event["path_url"], path_file, client, refresh=refresh, offline=offline)
    path = parse_path(html)
    locations = build_locations(path, event, locations_file)
    if (locations.time_utc_min >= 1440).any() or (locations.time_utc_min < 1).any() or (locations.time_utc_min > 1380).any():
        raise ValueError("Este evento necesita una ventana meteorológica que incluya días adyacentes.")
    years = historical_years(day, start, end)
    all_records, failures, missing, inputs = [], [], [], []
    for year in years:
        historical_date = f"{year}-{day[5:]}"
        params = {"latitude": ",".join(locations.latitude.map(lambda x: f"{x:.6f}")),
                  "longitude": ",".join(locations.longitude.map(lambda x: f"{x:.6f}")),
                  "start_date": historical_date, "end_date": historical_date,
                  "hourly": "cloud_cover,precipitation,rain", "timezone": "GMT", "models": "era5"}
        raw_file = bronze / f"weather_{year}.json"
        try:
            body = fetch(WEATHER_URL, raw_file, client, params, refresh, offline)
            records, absent = parse_weather(json.loads(body), locations, historical_date, event, rain_threshold)
            all_records.extend(records)
            missing.extend(absent)
            inputs.append({"file": str(raw_file.relative_to(root)), "sha256": hashlib.sha256(body).hexdigest()})
        except (requests.RequestException, ValueError, KeyError) as error:
            failures.append({"year": year, "error": str(error)})
        save_json(bronze / "manifest.json", {"event": event, "locations": locations.to_dict("records"),
            "requested_years": years, "weather_inputs": inputs, "failures": failures, "missing": missing,
            "rain_threshold_mm": rain_threshold, "path_sha256": hashlib.sha256(html).hexdigest()})
        if not offline:
            time.sleep(0.15)
    if not all_records:
        raise ValueError(f"No hay datos meteorológicos válidos. Revisa {bronze / 'manifest.json'}.")
    weather = pd.DataFrame(all_records).sort_values(["name", "year"]).reset_index(drop=True)
    if weather.duplicated(["event_id", "name", "year"]).any():
        raise ValueError("Observaciones duplicadas por evento/ubicación/año.")
    summary = weather.groupby("name").agg(valid_years=("year", "nunique"),
        mean_cloud_cover_pct=("cloud_cover_pct", "mean"), clear_probability=("clear_sky_proxy", "mean"),
        mean_precipitation_mm=("precipitation_mm", "mean"), mean_rain_mm=("rain_mm", "mean"),
        rain_probability=("rain_proxy", "mean")).reset_index()
    summary["cloud_probability"] = 1 - summary.clear_probability
    for metric in ["clear_probability", "cloud_probability", "rain_probability"]:
        bounds = [wilson_interval(row[metric] * row["valid_years"], row["valid_years"])
                  for row in summary.to_dict("records")]
        summary[f"{metric}_lower95"] = [value[0] for value in bounds]
        summary[f"{metric}_upper95"] = [value[1] for value in bounds]
    sites = locations.merge(summary, on="name", how="left", validate="one_to_one")
    sites["rankable_climate"] = sites.valid_years.fillna(0).ge(15) & sites.eligible_totality
    observations = weather.merge(locations[["name", "candidate_type", "distance_km", "estimated_totality_s",
        "time_utc_min", "eligible_totality"]], on="name", validate="many_to_one")
    observations["sample_id"] = observations.event_id + "|" + observations.name + "|" + observations.year.astype(str)
    for layer in ["silver", "gold"]:
        folder = root / "data" / layer / "events" / day
        folder.mkdir(parents=True, exist_ok=True)
        if layer == "silver":
            path.to_csv(folder / "eclipse_path.csv", index=False)
            weather.to_csv(folder / "weather_history.csv", index=False)
            locations.to_csv(folder / "locations.csv", index=False)
        else:
            observations.to_csv(folder / "eclipse_weather_observations.csv", index=False)
            sites.to_csv(folder / "eclipse_sites.csv", index=False)
    report = {"event_id": event["event_id"], "event_date": day, "path_rows": len(path),
        "locations": len(locations), "requested_years": years, "expected_observations": len(locations) * len(years),
        "observations": len(observations), "missing_observations": len(missing), "failed_years": failures,
        "coverage_by_site": sites.set_index("name").valid_years.fillna(0).astype(int).to_dict(),
        "rain_threshold_mm": rain_threshold, "cloud_threshold_pct": 20,
        "rain_class_counts": weather.rain_proxy.value_counts().to_dict(),
        "cloud_class_counts": weather.clear_sky_proxy.value_counts().to_dict(),
        "weather_nulls": weather.isna().sum().to_dict(),
        "source_manifest": str((bronze / "manifest.json").relative_to(root)),
        "preparation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "geometry": "approximate central-path interpolation, not precise local circumstances",
        "time_scale": "NASA path UT treated as approximate UTC; catalogue TD is not local time"}
    save_json(root / "data/gold/events" / day / "quality_report.json", report)
    return observations, sites, report


def legacy_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", help="Fecha YYYY-MM-DD de un eclipse del catálogo.")
    parser.add_argument("--locations", type=Path, help="CSV con name,latitude,longitude; sin él se usan referencias centrales.")
    parser.add_argument("--history-start", type=int)
    parser.add_argument("--history-end", type=int)
    parser.add_argument("--rain-threshold", type=float, default=0.1, help="Umbral de lluvia en mm/h (>=, defecto 0.1).")
    parser.add_argument("--refresh", action="store_true", help="Volver a descargar respuestas aunque estén guardadas.")
    parser.add_argument("--offline", action="store_true", help="Usar exclusivamente respuestas originales verificadas.")
    args = parser.parse_args(argv)
    if args.refresh and args.offline:
        parser.error("--refresh y --offline son incompatibles.")
    try:
        client = session()
        catalog = load_catalog(client, args.refresh, args.offline)
        if not args.event_date:
            print(catalog[["event_date", "eclipse_type", "geometry_supported"]].to_string(index=False))
            print(f"{len(catalog)} eventos guardados en data/legacy/gold/eclipse_catalog.csv")
            return 0
        event = select_event(catalog, args.event_date)
        _, _, report = acquire_event(event, client, args.locations, args.history_start, args.history_end,
                                     args.rain_threshold, args.refresh, args.offline)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["failed_years"] else 0
    except (ValueError, requests.RequestException, OSError, KeyError) as error:
        parser.exit(1, f"Error de adquisición: {error}\n")


def main(argv=None):
    import sys
    arguments = list(argv if argv is not None else sys.argv[1:])
    if "--legacy" in arguments:
        arguments.remove("--legacy")
        return legacy_main(arguments)
    try:
        from .eclipse_pipeline import main as pipeline_main
    except ImportError:
        from eclipse_pipeline import main as pipeline_main
    return pipeline_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
