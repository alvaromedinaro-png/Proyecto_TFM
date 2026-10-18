"""Preparación de la muestra inicial y acceso al flujo de adquisición por eclipse."""
from pathlib import Path
import hashlib
import json
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
# Valor por defecto de la muestra inicial. eclipse_sources resuelve otros eventos por fecha.
EVENT_DATE = "2027-08-02"
THRESHOLD = 20.0
MIN_YEARS = 15


def nearest_central_line_point(latitude, longitude, path):
    earth_radius_km = 6371.0088
    best = None
    for index in range(len(path) - 1):
        first = path.iloc[index]
        second = path.iloc[index + 1]
        mean_latitude = np.radians((latitude + first.latitude + second.latitude) / 3.0)
        x_scale = earth_radius_km * np.cos(mean_latitude) * np.pi / 180.0
        y_scale = earth_radius_km * np.pi / 180.0
        x1 = ((first.longitude - longitude + 180) % 360 - 180) * x_scale
        y1 = (first.latitude - latitude) * y_scale
        x2 = x1 + ((second.longitude - first.longitude + 180) % 360 - 180) * x_scale
        y2 = (second.latitude - latitude) * y_scale
        dx, dy = x2 - x1, y2 - y1
        denominator = dx * dx + dy * dy
        fraction = 0.0 if denominator == 0 else np.clip(-(x1 * dx + y1 * dy) / denominator, 0.0, 1.0)
        distance_km = float(np.hypot(x1 + fraction * dx, y1 + fraction * dy))
        if best is None or distance_km < best["distance_km"]:
            best = {
                "distance_km": distance_km,
                "fraction": float(fraction),
                "time_utc_min": float(first.time_utc + fraction * (second.time_utc - first.time_utc)),
                "duration_center_s": float(first.duration_center_s + fraction * (second.duration_center_s - first.duration_center_s)),
                "path_width_km": float(first.path_width_km + fraction * (second.path_width_km - first.path_width_km)),
            }
    return best



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
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def prepare(root=ROOT, output=None):
    """Muestra inicial de la exploración: lee y escribe en data/legacy, separada del producto."""
    legacy = root / "data" / "legacy"
    output = Path(output) if output else legacy
    raw = legacy / "bronze"
    silver = output / "silver"
    gold = output / "gold"
    silver.mkdir(parents=True, exist_ok=True)
    gold.mkdir(parents=True, exist_ok=True)
    path_file = raw / f"eclipse_path_{EVENT_DATE}.csv"
    weather_file = raw / f"weather_history_{EVENT_DATE}.csv"
    path = pd.read_csv(path_file)
    weather = pd.read_csv(weather_file)
    report = {"input_path_rows": len(path), "input_weather_rows": len(weather),
              "weather_nulls_before": weather.isna().sum().to_dict()}
    for column in path.columns:
        path[column] = pd.to_numeric(path[column], errors="coerce")
    if path.isna().any().any():
        raise ValueError("La trayectoria contiene valores ausentes o no numéricos.")
    if not (path.latitude.between(-90, 90).all() and path.longitude.between(-180, 180).all()
            and path.time_utc.between(0, 1439).all() and path.duration_center_s.gt(0).all()
            and path.path_width_km.gt(0).all()):
        raise ValueError("Coordenadas, tiempos o dimensiones de trayectoria inválidos.")
    if path.groupby("time_utc").nunique().gt(1).any().any():
        raise ValueError("La trayectoria tiene duplicados contradictorios.")
    path = path.drop_duplicates("time_utc").sort_values("time_utc").reset_index(drop=True)
    if len(path) < 3:
        raise ValueError("Se necesitan al menos tres puntos de trayectoria.")
    for column in ["year", "latitude", "longitude", "cloud_cover_pct"]:
        weather[column] = pd.to_numeric(weather[column], errors="coerce")
    weather["weather_time_utc"] = pd.to_datetime(weather.weather_time_utc, utc=True, errors="coerce")
    valid = (weather["name"].notna() & weather.year.notna() & (weather.year % 1 == 0)
             & weather.latitude.between(-90, 90) & weather.longitude.between(-180, 180)
             & weather.cloud_cover_pct.between(0, 100) & weather.weather_time_utc.notna())
    rejected = weather.loc[~valid].copy()
    weather = weather.loc[valid].copy()
    if weather.empty:
        raise ValueError("No hay observaciones meteorológicas válidas.")
    weather["year"] = weather.year.astype(int)
    observed = weather.weather_time_utc
    if not (observed.dt.year.eq(weather.year).all() and observed.dt.strftime("%m-%d").eq(EVENT_DATE[5:]).all()
            and observed.lt(pd.Timestamp.now(tz="UTC")).all() and weather.year.lt(int(EVENT_DATE[:4])).all()):
        raise ValueError("Fecha meteorológica inconsistente, futura o posterior al evento.")
    key = ["name", "year"]
    if weather.groupby(key).nunique(dropna=False).gt(1).any().any():
        raise ValueError("Hay observaciones contradictorias para ubicación/año.")
    report["weather_duplicates_removed"] = int(weather.duplicated(key).sum())
    weather = weather.drop_duplicates(key).sort_values(key).reset_index(drop=True)
    if weather.groupby("name")[["latitude", "longitude"]].nunique().gt(1).any().any():
        raise ValueError("Una ubicación cambia de coordenadas entre años.")
    weather["clear_sky_proxy"] = weather.cloud_cover_pct.le(THRESHOLD).astype(int)
    locations = weather[["name", "latitude", "longitude"]].drop_duplicates().copy()
    # Estas coordenadas proceden de la muestra existente, no del antiguo ranking ni del ML.
    locations["candidate_type"] = np.where(locations.name.str.startswith("Punto de trayectoria "), "linea central", "destino")
    circumstances = pd.DataFrame([
        nearest_central_line_point(row.latitude, row.longitude, path)
        for row in locations.itertuples()
    ], index=locations.index)
    locations = pd.concat([locations, circumstances], axis=1)
    half_width = locations.path_width_km / 2
    locations["eligible_totality"] = locations.distance_km.lt(half_width)
    locations["estimated_totality_s"] = locations.duration_center_s * np.sqrt(
        (1 - (locations.distance_km / half_width) ** 2).clip(lower=0))
    historical_hour = weather.weather_time_utc.dt.hour + weather.weather_time_utc.dt.minute / 60
    expected_hour = weather["name"].map(locations.set_index("name").time_utc_min) / 60
    if (historical_hour - expected_hour).abs().gt(0.5 + 1e-8).any():
        raise ValueError("La muestra no corresponde a la hora UTC más cercana al eclipse.")
    weather = weather.drop(columns=["event_time_utc"], errors="ignore")
    training = weather.merge(locations.drop(columns=["latitude", "longitude"]), on="name", validate="many_to_one")
    training["event_date"] = EVENT_DATE
    training["sample_id"] = EVENT_DATE + "|" + training["name"] + "|" + training.year.astype(str)
    summary = weather.groupby("name").agg(valid_years=("year", "nunique"),
        mean_cloud_cover_pct=("cloud_cover_pct", "mean"), clear_probability=("clear_sky_proxy", "mean")).reset_index()
    sites = locations.merge(summary, on="name", validate="one_to_one")
    sites["event_date"] = EVENT_DATE
    sites["rankable_climate"] = sites.valid_years.ge(MIN_YEARS) & sites.eligible_totality
    training.to_csv(gold / "eclipse_weather_observations.csv", index=False)
    sites.to_csv(gold / "eclipse_sites.csv", index=False)
    path.to_csv(silver / "eclipse_path.csv", index=False)
    weather.to_csv(silver / "weather_history.csv", index=False)
    rejected.to_csv(silver / "weather_rejected.csv", index=False)
    report.update({"path_duplicates_removed": report["input_path_rows"] - len(path),
        "weather_rejected_rows": len(rejected), "gold_observations": len(training),
        "gold_sites": len(sites), "destinations": int(sites.candidate_type.eq("destino").sum()),
        "synthetic_sites": int(sites.candidate_type.eq("linea central").sum()),
        "years": [int(weather.year.min()), int(weather.year.max())],
        "class_counts": {str(k): int(v) for k, v in weather.clear_sky_proxy.value_counts().items()},
        "valid_years_by_site": sites.set_index("name").valid_years.to_dict()})
    save_json(gold / "quality_report.json", report)
    save_json(gold / "manifest.json", {"event_date": EVENT_DATE, "threshold_pct": THRESHOLD,
        "preparation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "min_valid_years": MIN_YEARS, "inputs": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [path_file, weather_file]}, "raw_status": "Legacy extracted CSV samples; original responses were not preserved.",
        "weather_metadata": json.loads(weather_file.with_suffix(".metadata.json").read_text(encoding="utf-8")),
        "path_metadata": json.loads(path_file.with_suffix(".metadata.json").read_text(encoding="utf-8")),
        "model": "not trained", "geometry": "approximation; not validated local ephemerides"})
    return training, sites, report


def main():
    # Las opciones activan el flujo general; sin argumentos se reproduce la muestra inicial.
    if len(sys.argv) > 1:
        try:
            from .eclipse_sources import main as acquire_main
        except ImportError:
            from eclipse_sources import main as acquire_main
        arguments = sys.argv[1:]
        if "--acquire-bronze" in arguments:
            arguments.remove("--acquire-bronze")
            if "--event-date" not in arguments:
                arguments.extend(["--event-date", EVENT_DATE])
            example_locations = ROOT / "data/bronze/example_locations_2027.csv"
            if "--locations" not in arguments and arguments[arguments.index("--event-date") + 1] == EVENT_DATE and example_locations.exists():
                arguments.extend(["--locations", str(example_locations)])
        return acquire_main(arguments)
    observations, sites, report = prepare()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
