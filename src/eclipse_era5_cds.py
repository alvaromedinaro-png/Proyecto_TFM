"""Climatología ERA5 de Copernicus CDS para las zonas sin datos de Open-Meteo.

Open-Meteo cuenta su cuota por ubicación. El Climate Data Store de Copernicus sirve el mismo
reanálisis ERA5 sin esa cuota; su conjunto de series temporales por punto
(reanalysis-era5-single-levels-timeseries) responde en menos de un minuto por celda.

Para cada eclipse solar se toman las zonas del selector con al menos un 70 % de disco solar
oculto y se asigna a cada una la celda ERA5 de 0,25° en tierra más cercana (máscara tierra-mar
de ERA5), como hace Open-Meteo: en la costa, la celda más próxima puede ser de mar. Se pide una
serie por celda con los años de la climatología. De cada serie solo se guardan las horas que se usan (hora más cercana al máximo
y final del intervalo de lluvia, días −2..+2); el hash del ZIP original queda registrado.

Estos datos solo completan el selector: el ranking, el Gold de ML y el experimento siguen
saliendo de Open-Meteo. Las zonas que tienen las dos fuentes se comparan en el informe.

Requiere una cuenta del CDS, aceptar la licencia de ERA5 y el token en ~/.cdsapirc.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

try:
    from . import eclipse_sources as sources
except ImportError:
    import eclipse_sources as sources

DATASET = "reanalysis-era5-single-levels-timeseries"
VARIABLES = ["10m_u_component_of_wind", "10m_v_component_of_wind", "2m_temperature",
             "total_cloud_cover", "total_precipitation"]
GRID = 0.25
SOLAR_MIN_OBSCURATION = 0.70
MASK_FILE = Path("data/bronze/geography/era5_land_sea_mask.nc")
MASK_REQUEST = {"product_type": ["reanalysis"], "variable": ["land_sea_mask"], "year": ["2024"], "month": ["01"],
                "day": ["01"], "time": ["00:00"], "data_format": "netcdf", "download_format": "unarchived"}
# Fracción de tierra a partir de la cual una celda se considera terrestre.
LAND_THRESHOLD = 0.5
ZONE_FILE = "era5_zone_climate.csv"
REPORT_FILE = "era5_zone_report.json"
SOURCE = "copernicus_cds_era5"


def event_settings(event_date, root=sources.ROOT):
    """Años, ventana y umbral de lluvia de la preparación Open-Meteo del mismo evento."""
    report = json.loads((root / "data/gold/events" / event_date / "pipeline_quality_report.json").read_text(encoding="utf-8"))
    config = report["preparation_config"]
    return {"event_id": report["event_id"], "event_family": report["event_family"], "years": list(config["years"]),
            "before": config["days_before"], "after": config["days_after"], "rain_threshold_mm": config["rain_threshold_mm"]}


def land_sea_mask(root=sources.ROOT, offline=False):
    """Máscara tierra-mar de ERA5 (campo fijo, 780 kB), descargada una vez del CDS."""
    path = root / MASK_FILE
    if not path.is_file():
        if offline:
            raise ValueError(f"Falta {MASK_FILE.as_posix()}; ejecútalo una vez con red.")
        import cdsapi
        path.parent.mkdir(parents=True, exist_ok=True)
        cdsapi.Client(quiet=True, progress=False).retrieve("reanalysis-era5-single-levels", MASK_REQUEST, str(path))
        sources.save_json(path.with_suffix(".nc.metadata.json"), {"dataset": "reanalysis-era5-single-levels",
            "request": MASK_REQUEST, "sha256": sources.file_sha256(path), "acquired_utc": datetime.now(timezone.utc).isoformat()})
    return _read_mask(str(path))


@lru_cache(maxsize=2)
def _read_mask(filename):
    import h5py
    with h5py.File(filename, "r") as handle:
        return np.asarray(handle["lsm"][0], dtype=float)


def _land_fraction(mask, latitude, longitude):
    # Rejilla ERA5: latitud de 90 a −90 y longitud de 0 a 359,75 en pasos de 0,25°.
    return float(mask[int(round((90 - latitude) / GRID)), int(round((longitude % 360) / GRID)) % mask.shape[1]])


def cell(latitude, longitude, mask=None):
    """Celda ERA5 para una zona, con la longitud en [-180, 180).

    Sin máscara, la más cercana. Con máscara, la más cercana en tierra entre las nueve que rodean
    el punto; si ninguna lo es (una isla pequeña), la más cercana. Así se reproduce la celda de
    Open-Meteo en 145 de las 150 zonas preparadas, frente a 122 con la más cercana."""
    center_latitude, center_longitude = round(latitude / GRID) * GRID, round(longitude / GRID) * GRID
    options = []
    for row in (-1, 0, 1):
        for column in (-1, 0, 1):
            option_latitude = center_latitude + row * GRID
            option_longitude = center_longitude + column * GRID
            if abs(option_latitude) > 90:
                continue
            distance = np.hypot(option_latitude - latitude, (option_longitude - longitude) * np.cos(np.radians(latitude)))
            land = mask is None or _land_fraction(mask, option_latitude, option_longitude) >= LAND_THRESHOLD
            options.append((not land, distance, option_latitude, option_longitude))
    if mask is None:
        options = [option for option in options if option[2] == center_latitude and option[3] == center_longitude]
    _, _, chosen_latitude, chosen_longitude = min(options)
    return round(chosen_latitude, 2), round(((chosen_longitude + 180) % 360) - 180, 2)


def target_zones(event_date, root=sources.ROOT, mask=None):
    """Zonas del selector (sin referencias), si ya tienen Open-Meteo y su celda ERA5."""
    candidates = pd.read_csv(root / "data/silver/events" / event_date / "all_visible_candidates.csv")
    candidates = candidates.loc[~candidates.candidate_type.eq("referencia astronómica")]
    candidates = candidates.loc[candidates.obscuration_fraction.ge(SOLAR_MIN_OBSCURATION)]
    ranking = pd.read_csv(root / "data/gold/events" / event_date / "location_ranking.csv")
    prepared = set(zip(ranking.name, ranking.latitude.round(5), ranking.longitude.round(5)))
    zones = candidates[["name", "latitude", "longitude", "event_time_utc_approx"]].copy()
    zones["has_open_meteo"] = [(row.name, round(row.latitude, 5), round(row.longitude, 5)) in prepared
                               for row in zones.itertuples()]
    zones = zones.drop_duplicates(["name", "latitude", "longitude"]).reset_index(drop=True)
    zones["cell"] = [cell(row.latitude, row.longitude, mask) for row in zones.itertuples()]
    return zones


def zone_targets(zone, event_date, years, before, after):
    """Hora más cercana al máximo y final del intervalo de lluvia, igual que con Open-Meteo."""
    actual = datetime.fromisoformat(zone["event_time_utc_approx"])
    shift = (actual.date() - datetime.fromisoformat(event_date).date()).days
    targets = []
    for year in years:
        reference = datetime.fromisoformat(f"{year}-{event_date[5:]}").replace(tzinfo=timezone.utc)
        for offset in range(-before, after + 1):
            target = pd.Timestamp(reference + timedelta(days=shift + offset, hours=actual.hour,
                                                        minutes=actual.minute, seconds=actual.second))
            targets.append({"reference_year": year, "day_offset": offset,
                            "nearest": target.round("h"), "interval_end": target.ceil("h")})
    return targets


def cell_request(zones, event_date, years, before, after):
    """Una serie por celda: un único intervalo de fechas que cubre todos los años necesarios."""
    stamps = sorted({stamp for zone in zones.to_dict("records")
                     for target in zone_targets(zone, event_date, years, before, after)
                     for stamp in (target["nearest"], target["interval_end"])})
    latitude, longitude = zones.iloc[0]["cell"]
    request = {"variable": VARIABLES, "location": {"latitude": latitude, "longitude": longitude},
               "date": [f"{stamps[0].date().isoformat()}/{stamps[-1].date().isoformat()}"], "data_format": "csv"}
    return request, stamps


def retrieve_cell(request, stamps, path, offline=False):
    """Descarga la serie, conserva solo las horas usadas y registra el hash del ZIP original."""
    metadata_file = path.with_suffix(path.suffix + ".metadata.json")
    if path.is_file() and metadata_file.is_file():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        if metadata.get("request") == request and metadata.get("sha256") == sources.file_sha256(path):
            return path
    if offline:
        raise ValueError(f"Falta una copia verificada de {path}.")
    import cdsapi
    client = cdsapi.Client(quiet=True, progress=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".zip.part")
    client.retrieve(DATASET, request, str(temporary))
    body = temporary.read_bytes()
    temporary.unlink()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(members) != 1:
            raise ValueError("El CDS no devolvió un único CSV por celda.")
        series = pd.read_csv(io.BytesIO(archive.read(members[0])))
    series["valid_time"] = pd.to_datetime(series.valid_time, utc=True)
    missing = {"valid_time", "tcc", "tp", "t2m", "u10", "v10", "latitude", "longitude"} - set(series.columns)
    if missing:
        raise ValueError(f"Faltan columnas en la serie del CDS: {sorted(missing)}")
    kept = series.loc[series.valid_time.isin(pd.DatetimeIndex(stamps))].copy()
    kept["valid_time"] = kept.valid_time.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    sources.save_csv(kept, path)
    sources.save_json(metadata_file, {"dataset": DATASET, "request": request,
        "original_zip_sha256": hashlib.sha256(body).hexdigest(), "original_rows": len(series),
        "kept_rows": len(kept), "sha256": sources.file_sha256(path),
        "acquired_utc": datetime.now(timezone.utc).isoformat()})
    return path


def zone_samples(series, zone, event_date, years, before, after, rain_threshold):
    """Valores ERA5 de la celda para cada año y día, con las mismas reglas que Open-Meteo."""
    by_time = series.set_index("valid_time")
    records = []
    for target in zone_targets(zone, event_date, years, before, after):
        if target["nearest"] not in by_time.index or target["interval_end"] not in by_time.index:
            continue
        instant, interval = by_time.loc[target["nearest"]], by_time.loc[target["interval_end"]]
        cloud, rain = float(instant.tcc) * 100, float(interval.tp) * 1000
        records.append({"name": zone["name"], "reference_year": target["reference_year"], "day_offset": target["day_offset"],
            "cloud_cover": cloud, "precipitation_mm": rain, "temperature_2m": float(instant.t2m) - 273.15,
            "wind_speed_10m": float(np.hypot(instant.u10, instant.v10)) * 3.6,
            "clear_sky_proxy": float(cloud <= 20) if np.isfinite(cloud) else np.nan,
            "rain_proxy": float(rain >= rain_threshold) if np.isfinite(rain) else np.nan,
            "grid_latitude": float(instant.latitude), "grid_longitude": float(instant.longitude)})
    return records


def summarize(samples):
    """Resumen por zona y día con las columnas de climate_daily_summary.csv."""
    summary = samples.groupby(["name", "day_offset"]).agg(valid_cloud_years=("clear_sky_proxy", "count"),
        valid_rain_years=("rain_proxy", "count"), clear_probability=("clear_sky_proxy", "mean"),
        rain_probability=("rain_proxy", "mean"), mean_temperature_c=("temperature_2m", "mean"),
        mean_wind_speed_kmh=("wind_speed_10m", "mean"), mean_cloud_cover_pct=("cloud_cover", "mean"),
        grid_latitude=("grid_latitude", "first"), grid_longitude=("grid_longitude", "first")).reset_index()
    summary["cloud_probability"] = 1 - summary.clear_probability
    return summary


def compare_with_open_meteo(summary, event_date, root):
    """Diferencia de probabilidades con Open-Meteo en las zonas que tienen las dos fuentes."""
    daily = pd.read_csv(root / "data/gold/events" / event_date / "climate_daily_summary.csv")
    both = summary.merge(daily, on=["name", "day_offset"], suffixes=("_cds", "_om"))
    if both.empty:
        return {"zones": 0}
    clear = (both.clear_probability_cds - both.clear_probability_om).abs()
    rain = (both.rain_probability_cds - both.rain_probability_om).abs()
    return {"zones": int(both.name.nunique()), "rows": len(both),
            "clear_probability_mean_abs_diff": round(float(clear.mean()), 4),
            "clear_probability_max_abs_diff": round(float(clear.max()), 4),
            "rain_probability_mean_abs_diff": round(float(rain.mean()), 4),
            "rain_probability_max_abs_diff": round(float(rain.max()), 4)}


def prepare_event(event_date, root=sources.ROOT, offline=False, workers=1):
    settings = event_settings(event_date, root)
    if settings["event_family"] != "solar":
        raise ValueError("La climatología regional del CDS se prepara para eclipses solares.")
    zones = target_zones(event_date, root, land_sea_mask(root, offline))
    if zones.empty:
        print(f"{event_date}: ninguna zona alcanza el {SOLAR_MIN_OBSCURATION:.0%} de ocultación; no hay nada que pedir.", flush=True)
        return pd.DataFrame(), {"event_date": event_date, "zones": 0, "validation_against_open_meteo": {"zones": 0}}
    years, before, after = settings["years"], settings["before"], settings["after"]
    folder = root / "data/bronze/events" / event_date / "era5_cds"
    groups = [group for _, group in zones.groupby("cell", sort=True)]
    print(f"{event_date}: {len(zones)} zonas en {len(groups)} celdas ERA5.", flush=True)

    def download(group):
        request, stamps = cell_request(group, event_date, years, before, after)
        latitude, longitude = group.iloc[0]["cell"]
        return retrieve_cell(request, stamps, folder / f"cell_{latitude:+07.2f}_{longitude:+08.2f}.csv", offline)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        paths = list(pool.map(download, groups))
    records = []
    for group, path in zip(groups, paths):
        series = pd.read_csv(path)
        series["valid_time"] = pd.to_datetime(series.valid_time, utc=True)
        for zone in group.to_dict("records"):
            records.extend(zone_samples(series, zone, event_date, years, before, after, settings["rain_threshold_mm"]))
    summary = summarize(pd.DataFrame(records))
    summary = summary.merge(zones[["name", "latitude", "longitude", "has_open_meteo"]], on="name", how="left")
    summary.insert(0, "event_id", settings["event_id"])
    summary.insert(1, "event_date", event_date)
    summary["climate_source"] = SOURCE
    gold = root / "data/gold/events" / event_date
    sources.save_csv(summary, gold / ZONE_FILE)
    report = {"event_id": settings["event_id"], "event_date": event_date, "dataset": DATASET, "source": SOURCE,
        "years": years, "days_before": before, "days_after": after,
        "rain_threshold_mm": settings["rain_threshold_mm"], "cloud_threshold_pct": 20,
        "zones": len(zones), "cells": len(groups), "zones_without_open_meteo": int((~zones.has_open_meteo).sum()),
        "rain_variable": "total_precipitation: incluye nieve; Open-Meteo usa rain",
        "cell_rule": f"celda en tierra (máscara ERA5 >= {LAND_THRESHOLD}) más cercana entre las nueve vecinas",
        "inputs": [{"file": path.relative_to(root).as_posix(), "sha256": sources.file_sha256(path)} for path in paths],
        "validation_against_open_meteo": compare_with_open_meteo(summary, event_date, root),
        "output_sha256": {ZONE_FILE: sources.file_sha256(gold / ZONE_FILE)},
        "code_sha256": sources.code_sha256(), "prepared_at_utc": datetime.now(timezone.utc).isoformat()}
    sources.save_json(gold / REPORT_FILE, report)
    return summary, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--event-date", help="Un eclipse concreto; por defecto, todos los solares preparados.")
    parser.add_argument("--offline", action="store_true", help="Usar solo las descargas guardadas.")
    parser.add_argument("--workers", type=int, default=1, help="Peticiones simultáneas; el CDS solo admite una cola por usuario y dataset.")
    args = parser.parse_args(argv)
    dates = [args.event_date] if args.event_date else sorted(
        path.parent.name for path in (sources.ROOT / "data/gold/events").glob("*/pipeline_quality_report.json")
        if event_settings(path.parent.name)["event_family"] == "solar")
    failures = 0
    for event_date in dates:
        try:
            _, report = prepare_event(event_date, offline=args.offline, workers=args.workers)
            print(json.dumps({"event_date": event_date, "zones": report["zones"],
                              "validation": report["validation_against_open_meteo"]}, ensure_ascii=False), flush=True)
        except Exception as error:  # una celda fallida no impide preparar los demás eclipses
            failures += 1
            print(f"{event_date}: error {type(error).__name__}: {error}", flush=True)
    print("Después: python src/eclipse_pipeline.py --export-only para llevarlo al selector.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
