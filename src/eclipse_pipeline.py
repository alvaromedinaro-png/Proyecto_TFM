"""Flujo general: eclipses NASA futuros, candidatos, ventanas climáticas y ranking."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    from . import eclipse_sources as sources
    from .eclipse_catalogs import load_planning_catalog, planning_window, select_event
    from .eclipse_locations import SHORTLIST_POLICY, prepare_locations
except ImportError:
    import eclipse_sources as sources
    from eclipse_catalogs import load_planning_catalog, planning_window, select_event
    from eclipse_locations import SHORTLIST_POLICY, prepare_locations

HISTORICAL_VARIABLES = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
    "pressure_msl", "surface_pressure", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation", "rain", "snowfall", "weather_code", "wind_speed_10m", "wind_direction_10m",
    "wind_gusts_10m", "shortwave_radiation", "sunshine_duration"]
FORECAST_VARIABLES = HISTORICAL_VARIABLES + ["precipitation_probability", "visibility", "showers"]
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREPARATION_VERSION = 3
# En lunares la duración publicada es global (igual en todos los destinos) y la
# altura ya es la componente astronómica: no se cuentan dos veces.
SCORE_WEIGHTS = {
    "solar": {"clear_probability": 0.40, "no_rain_probability": 0.15, "astronomy": 0.20, "duration": 0.15, "altitude": 0.10},
    "lunar": {"clear_probability": 0.45, "no_rain_probability": 0.20, "astronomy": 0.35, "duration": 0.0, "altitude": 0.0}}
FAILURE_MARKER = "preparation_failure.json"
SNAPSHOT_FILES = ("climate_event_samples.csv", "planning_ml_dataset.csv", "location_ranking.csv", "destination_ranking.csv",
                  "astronomical_references.csv", "climate_daily_summary.csv", "climate_daily_conditions.csv",
                  "operational_forecast.csv", "forecast_status.json")
# Papel de cada columna de planning_ml_dataset.csv. Las 19 variables observadas a la hora
# del máximo de ese mismo año (y los días −2..+2 de ese año) definen las etiquetas o se
# observan con ellas: no se conocen al planificar y no son predictores.
ML_COLUMNS = {
    "identifiers": ["sample_id", "event_id", "event_date", "event_family", "name", "country_code",
                    "candidate_type", "reference_year", "row_role"],
    "predictors": ["latitude", "longitude", "elevation_m", "grid_latitude", "grid_longitude",
                   "day_of_year", "local_solar_hour", "body_altitude_deg",
                   "prior_years", "prior_clear_frequency", "prior_rain_frequency",
                   "prior_window_clear_frequency", "prior_mean_cloud_cover_pct", "prior_mean_cloud_cover_low_pct"],
    "baseline": ["baseline_clear_probability", "baseline_rain_probability"],
    "labels": ["clear_sky_proxy", "rain_proxy"]}
MIN_PRIOR_YEARS_FOR_EVALUATION = 10


def preparation_config(event, locations_file, max_locations, years, before, after, rain_threshold):
    """Configuración estable para reusar Gold sin volver a consultar fuentes."""
    return {"version": PREPARATION_VERSION, "event_id": event["event_id"],
        "years": years, "max_locations": max_locations, "days_before": before, "days_after": after,
        "rain_threshold_mm": rain_threshold, "cloud_threshold_pct": 20,
        "shortlist_policy": None if locations_file else SHORTLIST_POLICY,
        "locations_sha256": hashlib.sha256(Path(locations_file).read_bytes()).hexdigest() if locations_file else None}


def saved_preparation(event, config, root):
    """Solo se reusan preparaciones completas, compatibles y sin archivos alterados."""
    gold = root / "data/gold/events" / event["event_date"]
    try:
        report = json.loads((gold / "pipeline_quality_report.json").read_text(encoding="utf-8"))
        if (report.get("preparation_config") != config or report.get("event_id") != event["event_id"]
                or report["climate"]["failures"] or report["climate"]["missing_key_values"]):
            return None
        hashes = report.get("output_sha256", {})
        for filename in SNAPSHOT_FILES:
            path = gold / filename
            if not path.is_file() or hashes.get(filename) != hashlib.sha256(path.read_bytes()).hexdigest():
                return None
        ranking = pd.read_csv(gold / "location_ranking.csv")
        if ranking.empty or not ranking.event_id.eq(event["event_id"]).all():
            return None
        return ranking, {**report, "reused_preparation": True}
    except (OSError, ValueError, KeyError):
        return None


def parse_hourly(payload, locations, required_variables, start_date, end_date):
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(locations):
        raise ValueError("Respuesta meteorológica incompatible con las ubicaciones solicitadas.")
    frames, units = [], {}
    for location, item in zip(locations.to_dict("records"), items):
        if item.get("utc_offset_seconds") != 0:
            raise ValueError("Las horas meteorológicas deben estar en UTC.")
        hourly = item.get("hourly", {})
        timestamps = pd.to_datetime(hourly.get("time", []), utc=True)
        expected = pd.date_range(start_date, pd.Timestamp(end_date) + pd.Timedelta(23, unit="h"), freq="h", tz="UTC")
        if not timestamps.equals(expected):
            raise ValueError("Horas ausentes, duplicadas, desordenadas o fuera de la ventana solicitada.")
        current_units = item.get("hourly_units", {})
        if any(current_units.get(key) != unit for key, unit in [("cloud_cover", "%"), ("rain", "mm"), ("precipitation", "mm")]):
            raise ValueError("Unidades meteorológicas incompatibles.")
        if units and units != current_units:
            raise ValueError("Las unidades cambian entre ubicaciones.")
        units = current_units
        frame = pd.DataFrame({"time_utc": timestamps})
        for variable in required_variables:
            values = hourly.get(variable)
            if values is None or len(values) != len(timestamps):
                raise ValueError(f"La fuente no proporciona la variable {variable} en esta respuesta.")
            numeric = pd.to_numeric(pd.Series(values), errors="coerce")
            if np.isinf(numeric.dropna()).any():
                raise ValueError(f"Valores infinitos en {variable}.")
            frame[variable] = numeric.to_numpy()
        for variable in ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "relative_humidity_2m", "precipitation_probability"]:
            if variable in frame and not frame[variable].dropna().between(0, 100).all():
                raise ValueError(f"Porcentajes inválidos en {variable}.")
        for variable in ["precipitation", "rain", "snowfall", "wind_speed_10m", "wind_gusts_10m", "visibility"]:
            if variable in frame and frame[variable].dropna().lt(0).any():
                raise ValueError(f"Valores negativos en {variable}.")
        if ((frame.rain - frame.precipitation) > 0.11).any():
            raise ValueError("Lluvia incoherente con precipitación total.")
        frame["name"] = location["name"]
        frame["latitude"] = location["latitude"]
        frame["longitude"] = location["longitude"]
        frame["grid_latitude"] = item["latitude"]
        frame["grid_longitude"] = item["longitude"]
        frame["grid_elevation_m"] = item.get("elevation")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True), units


def weather_params(locations, begin, end, variables, historical=True):
    params = {"latitude": ",".join(locations.latitude.map(lambda x: f"{x:.6f}")),
        "longitude": ",".join(locations.longitude.map(lambda x: f"{x:.6f}")),
        "start_date": begin, "end_date": end, "hourly": ",".join(variables), "timezone": "GMT"}
    if historical:
        params["models"] = "era5"
    return params


def climate_window(event, locations, client, years, before=2, after=2, rain_threshold=0.1,
                   root=sources.ROOT, offline=False, refresh=False):
    bronze = root / "data/bronze/events" / event["event_date"]
    hourly_frames, records, failures, inputs, missing = [], [], [], [], []
    units = {}
    date_shifts = {row["name"]: (datetime.fromisoformat(row["event_time_utc_approx"]).date()
        - datetime.fromisoformat(event["event_date"]).date()).days for row in locations.to_dict("records")}
    days_requested = max(date_shifts.values()) - min(date_shifts.values()) + before + after + 3
    pause = sources.request_interval(len(locations), len(HISTORICAL_VARIABLES), days_requested)
    for year in years:
        reference = datetime.fromisoformat(f"{year}-{event['event_date'][5:]}").replace(tzinfo=timezone.utc)
        begin = (reference + timedelta(days=min(date_shifts.values()) - before - 1)).date().isoformat()
        end = (reference + timedelta(days=max(date_shifts.values()) + after + 1)).date().isoformat()
        available_until = datetime.now(timezone.utc).date() - timedelta(days=5)
        if datetime.fromisoformat(end).date() > available_until or datetime.fromisoformat(begin).year < 1940:
            failures.append({"reference_year": year, "error": "window_not_fully_available"})
            continue
        params = weather_params(locations, begin, end, HISTORICAL_VARIABLES)
        config_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
        raw = bronze / f"climate_{year}_{config_hash}.json"
        try:
            body = sources.fetch(sources.WEATHER_URL, raw, client, params, refresh, offline, min_interval=pause)
            hourly, units = parse_hourly(json.loads(body), locations, HISTORICAL_VARIABLES, begin, end)
            year_records, year_missing = [], []
            hourly["reference_year"] = year
            hourly["event_id"] = event["event_id"]
            hourly["event_date"] = event["event_date"]
            hourly["day_offset"] = ((hourly.time_utc.dt.normalize() - pd.Timestamp(reference)).dt.days
                                    - hourly.name.map(date_shifts))
            # Acumulados del día civil: intervalo que COMIENZA en cada hora (la API entrega el que termina).
            hourly["rain_next_hour_mm"] = hourly.groupby("name")["rain"].shift(-1)
            hourly["precipitation_next_hour_mm"] = hourly.groupby("name")["precipitation"].shift(-1)
            for row in locations.to_dict("records"):
                group = hourly.loc[hourly.name.eq(row["name"])].set_index("time_utc")
                actual = datetime.fromisoformat(row["event_time_utc_approx"])
                date_shift = (actual.date() - datetime.fromisoformat(event["event_date"]).date()).days
                for offset in range(-before, after + 1):
                    target = reference + timedelta(days=date_shift + offset, hours=actual.hour, minutes=actual.minute, seconds=actual.second)
                    nearest = pd.Timestamp(target).round("h")
                    interval_end = pd.Timestamp(target).ceil("h")
                    if nearest not in group.index or interval_end not in group.index:
                        raise ValueError("La ventana no cubre las horas necesarias del evento.")
                    instant, interval = group.loc[nearest], group.loc[interval_end]
                    values = {key: instant[key] for key in HISTORICAL_VARIABLES}
                    # Los acumulados y medias del intervalo se seleccionan por final de hora, no por proximidad.
                    for key in ["rain", "precipitation", "snowfall", "shortwave_radiation", "sunshine_duration"]:
                        values[key] = interval[key]
                    cloud, rain = values["cloud_cover"], values["rain"]
                    record = {**values, "event_id": event["event_id"], "event_date": event["event_date"],
                        "name": row["name"], "reference_year": year, "day_offset": offset,
                        "weather_time_utc": nearest.isoformat(), "rain_interval_end_utc": interval_end.isoformat(),
                        "grid_latitude": instant.grid_latitude, "grid_longitude": instant.grid_longitude,
                        "clear_sky_proxy": int(cloud <= 20) if pd.notna(cloud) else np.nan,
                        "rain_proxy": int(rain >= rain_threshold) if pd.notna(rain) else np.nan}
                    year_records.append(record)
                    if pd.isna(cloud) or pd.isna(rain):
                        year_missing.append({"name": row["name"], "reference_year": year, "day_offset": offset})
            records.extend(year_records)
            missing.extend(year_missing)
            hourly_frames.append(hourly.loc[hourly.day_offset.between(-before, after)].copy())
            inputs.append({"file": raw.relative_to(root).as_posix(), "sha256": hashlib.sha256(body).hexdigest()})
        except sources.RateLimitExceeded:
            raise
        except (ValueError, KeyError, requests.RequestException) as error:
            failures.append({"reference_year": year, "error": str(error)})
        sources.save_json(bronze / "climate_manifest.json", {"event_id": event["event_id"], "event_date": event["event_date"],
            "locations": locations.to_dict("records"), "years": years, "days_before": before, "days_after": after,
            "variables": HISTORICAL_VARIABLES, "units": units, "inputs": inputs, "failures": failures,
            "missing_key_values": missing, "model_requested": "era5", "rain_threshold_mm": rain_threshold})
    sources.save_json(bronze / "climate_manifest.json", {"event_id": event["event_id"], "event_date": event["event_date"],
        "locations": locations.to_dict("records"), "years": years, "days_before": before, "days_after": after,
        "variables": HISTORICAL_VARIABLES, "units": units, "inputs": inputs, "failures": failures,
        "missing_key_values": missing, "model_requested": "era5", "rain_threshold_mm": rain_threshold})
    if not hourly_frames or not records:
        raise ValueError("No se obtuvo una ventana climática válida; revisar climate_manifest.json.")
    samples = pd.DataFrame(records)
    samples["sample_id"] = (samples.event_id + "|" + samples.name + "|" + samples.reference_year.astype(str)
                            + "|" + samples.day_offset.astype(str))
    if samples.sample_id.duplicated().any():
        raise ValueError("Muestras climáticas duplicadas.")
    return pd.concat(hourly_frames, ignore_index=True), samples, {"years": years, "failures": failures,
        "missing_key_values": missing, "units": units, "days_before": before, "days_after": after,
        "hourly_rows": sum(len(frame) for frame in hourly_frames), "sample_rows": len(samples)}


def planning_dataset(event, locations, samples):
    """Una fila por ubicación y año: la etiqueta de ese año y solo predictores conocidos al planificar.

    La climatología de cada fila usa exclusivamente años anteriores, como una consulta real.
    La fila «query» es la del propio eclipse: todos los años preparados y etiqueta desconocida."""
    yearly = samples.loc[samples.day_offset.eq(0), ["name", "reference_year", "clear_sky_proxy", "rain_proxy",
                                                   "cloud_cover", "cloud_cover_low", "grid_latitude", "grid_longitude"]]
    window = samples.groupby(["name", "reference_year"]).clear_sky_proxy.agg(window_clear_sum="sum", window_clear_n="count")
    yearly = yearly.merge(window.reset_index(), on=["name", "reference_year"], how="left")
    query = yearly.drop_duplicates("name").assign(reference_year=int(event["event_date"][:4]), clear_sky_proxy=np.nan,
        rain_proxy=np.nan, cloud_cover=np.nan, cloud_cover_low=np.nan, window_clear_sum=0, window_clear_n=0)
    yearly = pd.concat([yearly.assign(row_role="history"), query.assign(row_role="query")], ignore_index=True)
    yearly = yearly.sort_values(["name", "reference_year"]).reset_index(drop=True)

    def prior(values, counts=None):
        # Suma acumulada exclusiva: la fila de un año nunca ve su propio valor.
        counts = values.notna().astype(int) if counts is None else counts
        filled = values.fillna(0)
        total = filled.groupby(yearly.name).cumsum() - filled
        n = counts.groupby(yearly.name).cumsum() - counts
        return total / n.where(n > 0), n

    yearly["prior_clear_frequency"], yearly["prior_years"] = prior(yearly.clear_sky_proxy)
    yearly["prior_rain_frequency"], _ = prior(yearly.rain_proxy)
    yearly["prior_window_clear_frequency"], _ = prior(yearly.window_clear_sum, yearly.window_clear_n)
    yearly["prior_mean_cloud_cover_pct"], _ = prior(yearly.cloud_cover)
    yearly["prior_mean_cloud_cover_low_pct"], _ = prior(yearly.cloud_cover_low)
    yearly["baseline_clear_probability"] = yearly.prior_clear_frequency
    yearly["baseline_rain_probability"] = yearly.prior_rain_frequency
    static = locations.copy()
    peak = pd.to_datetime(static.event_time_utc_approx, utc=True)
    static["local_solar_hour"] = ((peak.dt.hour + peak.dt.minute / 60 + static.longitude / 15) % 24).round(3)
    static["day_of_year"] = peak.dt.dayofyear
    static["event_family"] = event["event_family"]
    for column in ["country_code", "elevation_m"]:
        if column not in static:
            static[column] = np.nan
    dataset = yearly.merge(static[["name", "latitude", "longitude", "elevation_m", "country_code", "candidate_type",
                                   "body_altitude_deg", "local_solar_hour", "day_of_year", "event_family"]], on="name", how="left")
    dataset["event_id"] = event["event_id"]
    dataset["event_date"] = event["event_date"]
    dataset["sample_id"] = dataset.event_id + "|" + dataset.name + "|" + dataset.reference_year.astype(str)
    return dataset[[column for role in ML_COLUMNS.values() for column in role]]


def operational_forecast(event, locations, client, before=2, after=2, root=sources.ROOT,
                         offline=False, now=None):
    now = now or datetime.now(timezone.utc)
    date = datetime.fromisoformat(event["event_date"]).date()
    event_days = [datetime.fromisoformat(value).date() for value in locations.event_time_utc_approx]
    requested_begin, requested_end = min(event_days) - timedelta(days=before), max(event_days) + timedelta(days=after)
    begin, end = requested_begin, requested_end
    available_start, available_end = now.date(), now.date() + timedelta(days=15)
    status = {"requested_start": begin.isoformat(), "requested_end": end.isoformat(),
              "forecast_horizon_start": available_start.isoformat(), "forecast_horizon_end": available_end.isoformat()}
    if begin > available_end or end < available_start:
        return pd.DataFrame(), {**status, "status": "outside_forecast_horizon",
            "reason": "No operational forecast exists for these dates; historical climatology remains separate."}
    begin, end = max(begin, available_start), min(end, available_end)
    params = weather_params(locations, begin.isoformat(), end.isoformat(), FORECAST_VARIABLES, historical=False)
    raw = root / "data/bronze/events" / event["event_date"] / "forecast.json"
    try:
        # Las previsiones operativas se renuevan al ejecutar con red; offline conserva la fecha del proveedor.
        body = sources.fetch(FORECAST_URL, raw, client, params, refresh=not offline, offline=offline)
        frame, units = parse_hourly(json.loads(body), locations, FORECAST_VARIABLES, begin.isoformat(), end.isoformat())
        metadata = json.loads(raw.with_suffix(".json.metadata.json").read_text(encoding="utf-8"))
        frame["event_id"] = event["event_id"]
        frame["event_date"] = event["event_date"]
        availability = {variable: int(frame[variable].notna().sum()) for variable in FORECAST_VARIABLES}
        complete_values = all(count == len(frame) for count in availability.values())
        age_hours = max(0.0, (now - datetime.fromisoformat(metadata["acquired_utc"])).total_seconds() / 3600)
        forecast_state = "available" if complete_values and begin == requested_begin and end == requested_end else "partially_available"
        if offline and age_hours > 6:
            forecast_state = "stale"
        return frame, {**status, "status": forecast_state,
            "available_start": begin.isoformat(), "available_end": end.isoformat(), "units": units,
            "acquired_utc": metadata["acquired_utc"], "is_cached": offline,
            "cache_age_hours": age_hours, "stale_after_hours": 6,
            "non_null_values_by_variable": availability,
            "event_day_available": begin <= date <= end}
    except (ValueError, KeyError, requests.RequestException) as error:
        return pd.DataFrame(), {**status, "status": "unavailable", "error": str(error)}


def event_family(frame, family=None):
    if family:
        return family
    if "event_family" in frame and frame.event_family.notna().any():
        return str(frame.event_family.dropna().iloc[0])
    if "event_id" in frame and frame.event_id.astype(str).str.startswith("NASA-LE").any():
        return "lunar"
    return "solar"


def weighted_score(weights, clear_probability, no_rain_probability, row):
    return (weights["clear_probability"] * clear_probability + weights["no_rain_probability"] * no_rain_probability
            + weights["astronomy"] * row["astronomy_score"] + weights["duration"] * row["duration_score"]
            + weights["altitude"] * row["altitude_score"])


def rank_locations(locations, samples, family=None):
    weights = SCORE_WEIGHTS[event_family(locations, family)]
    records = []
    duration_column = "phase_duration_s" if "phase_duration_s" in locations else "central_duration_s"
    max_duration = locations[duration_column].max()
    for row in locations.to_dict("records"):
        group = samples.loc[samples.name.eq(row["name"]) & samples.day_offset.eq(0)]
        clear, rain = group.clear_sky_proxy.dropna(), group.rain_proxy.dropna()
        clear_probability = float(clear.mean()) if len(clear) else np.nan
        rain_probability = float(rain.mean()) if len(rain) else np.nan
        lower, upper = sources.wilson_interval(float(clear.sum()), len(clear))
        rlower, rupper = sources.wilson_interval(float(rain.sum()), len(rain))
        altitude_score = max(0.0, np.sin(np.radians(row["body_altitude_deg"])))
        astronomy_score = row["obscuration_fraction"] if pd.notna(row.get("obscuration_fraction")) else altitude_score
        duration_score = row[duration_column] / max_duration if max_duration > 0 and pd.notna(row[duration_column]) else 0.0
        components = {"astronomy_score": astronomy_score, "duration_score": duration_score, "altitude_score": altitude_score}
        score = (weighted_score(weights, clear_probability, 1 - rain_probability, components)
                 if pd.notna(clear_probability) and pd.notna(rain_probability) else np.nan)
        records.append({**row, "valid_cloud_years": len(clear), "valid_rain_years": len(rain),
            "clear_probability": clear_probability, "cloud_probability": 1 - clear_probability,
            "rain_probability": rain_probability, "clear_lower95": lower, "clear_upper95": upper,
            "rain_lower95": rlower, "rain_upper95": rupper, "astronomy_score": astronomy_score,
            "altitude_score": altitude_score, "duration_score": duration_score, "score": score,
            "ranking_validated_coverage": len(clear) >= 15 and len(rain) >= 15})
    ranking = pd.DataFrame(records).sort_values(["phase_priority", "ranking_validated_coverage", "score"], ascending=False,
                                               na_position="last").reset_index(drop=True)
    ranking["rank"] = np.arange(1, len(ranking) + 1)
    return ranking


def apply_forecast_to_ranking(ranking, forecast, status, family=None):
    """Añade condiciones operativas; cambia la ordenación solo con cobertura comparable en todos los candidatos."""
    weights = SCORE_WEIGHTS[event_family(ranking, family)]
    ranking = ranking.copy()
    ranking["forecast_cloud_cover_pct"] = np.nan
    ranking["forecast_precipitation_probability_pct"] = np.nan
    ranking["forecast_score"] = np.nan
    ranking["ranking_basis"] = "historical_climatology"
    if forecast.empty or status.get("status") not in {"available", "partially_available"}:
        return ranking
    for index, row in ranking.iterrows():
        group = forecast.loc[forecast.name.eq(row["name"])].copy()
        if group.empty:
            continue
        group["time_utc"] = pd.to_datetime(group.time_utc, utc=True)
        target = pd.Timestamp(row["event_time_utc_approx"])
        group = group.set_index("time_utc")
        cloud_hour, rain_hour = target.round("h"), target.ceil("h")
        if cloud_hour not in group.index or rain_hour not in group.index:
            continue
        cloud = group.loc[cloud_hour, "cloud_cover"]
        probability = group.loc[rain_hour, "precipitation_probability"]
        if pd.isna(cloud) or pd.isna(probability):
            continue
        ranking.loc[index, "forecast_cloud_cover_pct"] = cloud
        ranking.loc[index, "forecast_precipitation_probability_pct"] = probability
        ranking.loc[index, "forecast_score"] = weighted_score(weights, 1 - cloud / 100, 1 - probability / 100, row)
    if ranking.forecast_score.notna().all():
        ranking["ranking_basis"] = "operational_forecast_proxy"
        ranking = ranking.sort_values(["phase_priority", "forecast_score"], ascending=False).reset_index(drop=True)
        ranking["rank"] = np.arange(1, len(ranking) + 1)
    return ranking


def acquire_complete_event(event, client=None, root=sources.ROOT, locations_file=None, max_locations=12,
                           start=None, end=None, before=2, after=2, rain_threshold=0.1, offline=False, refresh=False,
                           as_of=None):
    if not 0 <= before <= 30 or not 0 <= after <= 30 or not np.isfinite(rain_threshold) or rain_threshold <= 0:
        raise ValueError("Ventana entre 0 y 30 días por lado y umbral de lluvia positivo.")
    if max_locations < 1:
        raise ValueError("Se necesita al menos una ubicación candidata.")
    years = sources.historical_years(event["event_date"], start, end, as_of)
    config = preparation_config(event, locations_file, max_locations, years, before, after, rain_threshold)
    saved = saved_preparation(event, config, root) if not refresh else None
    if saved:
        (root / "data/gold/events" / event["event_date"] / FAILURE_MARKER).unlink(missing_ok=True)
        return saved
    client = client or sources.session()
    locations, geometry = prepare_locations(event, client, root, locations_file, max_locations, offline, refresh)
    hourly, samples, quality = climate_window(event, locations, client, years, before, after, rain_threshold, root, offline, refresh)
    forecast, forecast_status = operational_forecast(event, locations, client, before, after, root, offline)
    family = event["event_family"]
    ranking = rank_locations(locations, samples, family)
    ranking = apply_forecast_to_ranking(ranking, forecast, forecast_status, family)
    silver = root / "data/silver/events" / event["event_date"]
    gold = root / "data/gold/events" / event["event_date"]
    silver.mkdir(parents=True, exist_ok=True)
    gold.mkdir(parents=True, exist_ok=True)
    sources.save_csv(hourly, silver / "climate_hourly.csv")
    sources.save_csv(samples, gold / "climate_event_samples.csv")
    sources.save_csv(planning_dataset(event, locations, samples), gold / "planning_ml_dataset.csv")
    sources.save_csv(ranking, gold / "location_ranking.csv")
    destinations = ranking.loc[~ranking.candidate_type.eq("referencia astronómica")].copy()
    destinations["destination_rank"] = np.arange(1, len(destinations) + 1)
    sources.save_csv(destinations, gold / "destination_ranking.csv")
    sources.save_csv(ranking.loc[ranking.candidate_type.eq("referencia astronómica")], gold / "astronomical_references.csv")
    summary = samples.groupby(["name", "day_offset"]).agg(valid_cloud_years=("clear_sky_proxy", "count"),
        valid_rain_years=("rain_proxy", "count"), clear_probability=("clear_sky_proxy", "mean"),
        rain_probability=("rain_proxy", "mean"), mean_temperature_c=("temperature_2m", "mean"),
        mean_wind_speed_kmh=("wind_speed_10m", "mean"), mean_cloud_cover_pct=("cloud_cover", "mean")).reset_index()
    summary["cloud_probability"] = 1 - summary.clear_probability
    sources.save_csv(summary, gold / "climate_daily_summary.csv")
    daily_records = []
    for (name, reference_year, offset), group in hourly.groupby(["name", "reference_year", "day_offset"]):
        daily_records.append({"name": name, "reference_year": reference_year, "day_offset": offset,
            "event_id": event["event_id"], "event_date": event["event_date"],
            "mean_temperature_c": group.temperature_2m.mean(), "min_temperature_c": group.temperature_2m.min(),
            "max_temperature_c": group.temperature_2m.max(), "mean_cloud_cover_pct": group.cloud_cover.mean(),
            "max_wind_speed_kmh": group.wind_speed_10m.max(),
            "rain_mm": group.rain_next_hour_mm.sum(min_count=24),
            "precipitation_mm": group.precipitation_next_hour_mm.sum(min_count=24),
            "valid_rain_hours": int(group.rain_next_hour_mm.notna().sum()), "valid_temperature_hours": int(group.temperature_2m.notna().sum())})
    sources.save_csv(pd.DataFrame(daily_records), gold / "climate_daily_conditions.csv")
    # Escribir también una tabla vacía si no hay previsión: evita reutilizar una salida antigua.
    if forecast.empty:
        forecast = pd.DataFrame(columns=["event_id", "event_date", "name", "time_utc", "precipitation_probability", "cloud_cover"])
    sources.save_csv(forecast, gold / "operational_forecast.csv")
    sources.save_json(gold / "forecast_status.json", forecast_status)
    report = {"event_id": event["event_id"], "event_date": event["event_date"], "event_family": event["event_family"],
        "geometry": geometry, "climate": quality, "forecast": forecast_status,
        "ranking_weights": SCORE_WEIGHTS[family],
        "ranking_rule": "phase priority, coverage >=15 years, then weighted score; best only among shortlisted candidates",
        "location_interpretation": "Astronomical references may be at sea or inaccessible; destination ranking is separate.",
        "ranking_basis": ranking.ranking_basis.iloc[0],
        "rain_threshold_mm": rain_threshold, "cloud_threshold_pct": 20,
        "ml_dataset": {"file": "planning_ml_dataset.csv", "columns": ML_COLUMNS,
            "min_prior_years_for_evaluation": MIN_PRIOR_YEARS_FOR_EVALUATION,
            "excluded_from_predictors": "weather variables observed in the same reference year (event hour and days -2..+2)",
            "validation": "temporal split by reference_year; baseline = prior empirical frequency"},
        "preparation_config": config, "reused_preparation": False,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_sha256": {filename: hashlib.sha256((gold / filename).read_bytes()).hexdigest()
                          for filename in SNAPSHOT_FILES},
        "code_sha256": sources.code_sha256()}
    sources.save_json(gold / "pipeline_quality_report.json", report)
    (gold / FAILURE_MARKER).unlink(missing_ok=True)
    return ranking, report


def record_failure(event, error, root=None):
    """Una adquisición fallida no borra la Gold anterior, pero impide presentarla como vigente."""
    root = root or sources.ROOT
    sources.save_json(root / "data/gold/events" / event["event_date"] / FAILURE_MARKER, {
        "event_id": event["event_id"], "event_date": event["event_date"],
        "failed_at_utc": datetime.now(timezone.utc).isoformat(), "error": str(error)})


def verify_outputs(root=sources.ROOT):
    """Comprueba que cada informe describe los archivos entregados y el código actual.

    Las respuestas climáticas no versionadas cuentan como no incluidas, no como error."""
    code = sources.code_sha256()
    results = []
    for report_path in sorted((root / "data/gold/events").glob("*/pipeline_quality_report.json")):
        folder = report_path.parent
        report = json.loads(report_path.read_text(encoding="utf-8"))
        problems = []
        for filename, expected in report.get("output_sha256", {}).items():
            if not (folder / filename).is_file():
                problems.append(f"falta {filename}")
            elif sources.file_sha256(folder / filename) != expected:
                problems.append(f"{filename} no coincide con el hash del informe")
        if report.get("code_sha256") != code:
            problems.append("el informe se generó con otra versión del código")
        inputs, included = [], 0
        manifest = root / "data/bronze/events" / folder.name / "climate_manifest.json"
        if manifest.is_file():
            inputs = json.loads(manifest.read_text(encoding="utf-8")).get("inputs", [])
            for item in inputs:
                path = root / item["file"].replace("\\", "/")
                if path.is_file():
                    included += 1
                    if sources.file_sha256(path) != item["sha256"]:
                        problems.append(f"{item['file']} no coincide con el manifiesto")
        results.append({"event_date": folder.name, "problems": problems,
                        "climate_inputs": len(inputs), "climate_inputs_included": included})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date")
    parser.add_argument("--family", choices=["solar", "lunar"])
    parser.add_argument("--all-future", action="store_true", help="Preparar la ventana futura limitada (igual que sin opciones).")
    parser.add_argument("--future-years", type=int, help="Horizonte de eclipses futuros, 3 años por defecto (1–10).")
    parser.add_argument("--catalog-only", action="store_true", help="Obtener el catálogo limitado sin adquirir clima.")
    parser.add_argument("--export-only", action="store_true", help="Actualizar el selector desde Gold sin consultar fuentes.")
    parser.add_argument("--limit", type=int, help="Limitar eventos procesados para una muestra de validación.")
    parser.add_argument("--as-of", help="Inicio ISO de la ventana; por defecto hoy UTC.")
    parser.add_argument("--locations", type=Path)
    parser.add_argument("--max-locations", type=int, default=12)
    parser.add_argument("--history-start", type=int)
    parser.add_argument("--history-end", type=int)
    parser.add_argument("--days-before", type=int, default=2)
    parser.add_argument("--days-after", type=int, default=2)
    parser.add_argument("--rain-threshold", type=float, default=0.1)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--verify", action="store_true", help="Comprobar hashes de las salidas Gold y versión del código, sin red.")
    args = parser.parse_args(argv)
    if args.verify:
        results = verify_outputs()
        for result in results:
            state = "; ".join(result["problems"]) or "coincide"
            print(f"{result['event_date']}: {state} (respuestas climáticas incluidas: "
                  f"{result['climate_inputs_included']}/{result['climate_inputs']})")
        return 1 if not results or any(result["problems"] for result in results) else 0
    if args.refresh and args.offline:
        parser.error("--offline y --refresh son incompatibles.")
    if args.event_date and args.all_future:
        parser.error("Selecciona una fecha o --all-future.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit debe ser positivo.")
    if args.locations and not args.event_date:
        parser.error("Un CSV regional de ubicaciones requiere seleccionar un evento concreto.")
    if args.catalog_only and args.event_date:
        parser.error("--catalog-only no prepara eventos individuales.")
    if args.export_only and (args.refresh or args.catalog_only or args.event_date or args.all_future):
        parser.error("--export-only se utiliza sin opciones de adquisición.")
    if args.max_locations < 1 or not 0 <= args.days_before <= 30 or not 0 <= args.days_after <= 30:
        parser.error("Elige al menos una ubicación y entre 0 y 30 días por lado.")
    if not np.isfinite(args.rain_threshold) or args.rain_threshold <= 0:
        parser.error("--rain-threshold debe ser positivo y finito.")
    try:
        try:
            from . import eclipse_product as product
        except ImportError:
            import eclipse_product as product
        future_years = args.future_years if args.future_years is not None else 3
        if args.export_only:
            options = ["--future-years", str(future_years)]
            if args.as_of:
                options.extend(["--as-of", args.as_of])
            return product.main(options)
        begin, end = planning_window(args.as_of, future_years)
        client = sources.session()
        planning = load_planning_catalog(client, args.refresh, args.offline, begin, future_years, root=sources.ROOT)

        def export():
            product.export_current_window(begin, future_years, client, offline=args.offline, root=sources.ROOT)
        future = planning
        if args.family:
            future = future.loc[future.event_family.eq(args.family)]
        print(f"Ventana de planificación: {begin} hasta {end} (fin excluido); {len(planning)} eclipses.", flush=True)
        if args.catalog_only:
            print(future[["event_id", "event_date", "event_family", "eclipse_type"]].head(args.limit or 20).to_string(index=False))
            export()
            print("Catálogo: data/gold/planning_eclipse_catalog.csv; selector: app/index.html")
            return 0
        events = ([select_event(planning, args.event_date, args.family)] if args.event_date
                  else future.head(args.limit or len(future)).to_dict("records"))
        results = []
        quota_exhausted = None
        report_path = sources.ROOT / "data/gold/batch_acquisition_report.json"
        batch_info = {"planning_start": begin, "planning_end_exclusive": end,
            "future_years": future_years,
            "planning_events": len(planning), "future_catalog_events": len(future), "requested_events": len(events)}
        sources.save_json(report_path, {**batch_info, "processed_events": 0, "batch_finished": not events, "results": []})
        for event in events:
            print(f"Procesando {event['event_id']} {event['event_date']} ({event['event_family']}, {event['eclipse_type']})", flush=True)
            try:
                ranking, report = acquire_complete_event(event, client, root=sources.ROOT, locations_file=args.locations,
                    max_locations=args.max_locations, start=args.history_start, end=args.history_end,
                    before=args.days_before, after=args.days_after, rain_threshold=args.rain_threshold,
                    offline=args.offline, refresh=args.refresh, as_of=begin)
                failures = report["climate"]["failures"]
                destinations = ranking.loc[~ranking.candidate_type.eq("referencia astronómica")]
                results.append({"event_id": event["event_id"], "event_date": event["event_date"],
                    "status": "partial" if failures or report["climate"]["missing_key_values"] else "complete", "climate_failures": failures,
                    "reused_preparation": report.get("reused_preparation", False),
                    "forecast_status": report["forecast"]["status"], "best_evaluated_location": ranking.iloc[0]["name"],
                    "best_evaluated_destination": destinations.iloc[0]["name"] if not destinations.empty else None})
            except sources.RateLimitExceeded as error:
                record_failure(event, error)
                results.append({"event_id": event["event_id"], "event_date": event["event_date"], "status": "failed", "error": str(error)})
                quota_exhausted = str(error)
            except (ValueError, KeyError, OSError, requests.RequestException) as error:
                record_failure(event, error)
                results.append({"event_id": event["event_id"], "event_date": event["event_date"], "status": "failed", "error": str(error)})
            sources.save_json(report_path, {**batch_info,
                "processed_events": len(results), "batch_finished": len(results) == len(events), "results": results})
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
            export()
            if quota_exhausted:
                # Seguir solo añadiría fallos; lo descargado queda en Bronze para reanudar.
                print(f"Adquisición detenida: {quota_exhausted}", flush=True)
                break
        if not events:
            export()
        print("Selector preparado: abre app/index.html. Consultar un eclipse no realiza descargas.", flush=True)
        return 1 if quota_exhausted or any(result["status"] != "complete" for result in results) else 0
    except (ValueError, KeyError, OSError, requests.RequestException) as error:
        parser.exit(1, f"Error de adquisición: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
