"""Exportación local del catálogo y sus destinos para el selector de eclipses.

No descarga datos ni vuelve a calcular geometría o clima. Los porcentajes
climáticos son frecuencias históricas a la hora del evento; la ocultación solar
es la fracción del disco solar cubierta. La duración lunar sigue siendo global.
"""
import argparse
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import unicodedata

import pandas as pd

try:
    from . import eclipse_sources as sources
    from .eclipse_catalogs import catalog_between, planning_window
    from .eclipse_geography import enrich_country, export_world_map
except ImportError:
    import eclipse_sources as sources
    from eclipse_catalogs import catalog_between, planning_window
    from eclipse_geography import enrich_country, export_world_map


SCHEMA_VERSION = 1
# Umbral único del selector: la interfaz lo lee de la exportación.
SOLAR_OBSCURATION_THRESHOLD_PCT = 70
MIN_RANKING_YEARS = 15
# Eclipses posteriores a la ventana que se exportan como pendientes: así el selector
# puede mantener «hoy + 3 años» aunque se abra tiempo después de la exportación.
CATALOG_MARGIN_YEARS = 2
FAILURE_MARKER = "preparation_failure.json"
TYPE_LABELS = {"total": "total", "partial": "parcial", "annular": "anular",
               "hybrid": "híbrido", "penumbral": "penumbral"}


def _iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Fecha ISO no válida: {value!r}.")
    return date.fromisoformat(value)


def _number(value):
    if value is None or pd.isna(value):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _percentage(value):
    result = _number(value)
    if result is None:
        return None
    if not 0 <= result <= 1:
        raise ValueError("Una frecuencia climática o una ocultación está fuera de [0, 1].")
    return result * 100


def _count(value):
    result = _number(value)
    if result is None:
        return None
    if result < 0 or not result.is_integer():
        raise ValueError("Un recuento de años válidos no es un entero positivo o cero.")
    return int(result)


def _boolean(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        if value.lower() in {"true", "1"}:
            return True
        if value.lower() in {"false", "0"}:
            return False
        raise ValueError(f"Valor booleano no reconocido: {value!r}.")
    return bool(value)


def _is_reference(value):
    if value is None or pd.isna(value):
        return False
    normalized = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    return "referencia" in normalized or "reference" in normalized


def _json_clean(value):
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _saved_forecast_status(report):
    """Una exportación tardía nunca vuelve a presentar como vigente la previsión guardada."""
    forecast = _json_clean(dict(report.get("forecast", {"status": "not_prepared"})))
    acquired = forecast.get("acquired_utc")
    if forecast.get("status") in {"available", "partially_available"}:
        if not acquired:
            forecast["status"] = "stale"
            forecast["reason"] = "No acquisition timestamp is available for this saved forecast."
        else:
            acquired_time = datetime.fromisoformat(str(acquired).replace("Z", "+00:00"))
            if acquired_time.tzinfo is None:
                raise ValueError("La previsión guardada no indica zona horaria de adquisición.")
            age = max(0.0, (datetime.now(timezone.utc) - acquired_time).total_seconds() / 3600)
            forecast["cache_age_hours"] = age
            if age > 6:
                forecast["status"] = "stale"
                forecast["reason"] = "Saved forecast is older than six hours."
    return forecast


def _check_identity(mapping, event, required=False):
    for key in ("event_id", "event_date", "event_family"):
        if key not in mapping:
            if required and key != "event_family":
                raise ValueError(f"Falta {key}; no se puede verificar la identidad del evento.")
            continue
        values = mapping[key] if isinstance(mapping, pd.DataFrame) else [mapping[key]]
        if any(pd.isna(value) or str(value) != event[key] for value in values):
            raise ValueError(f"{key} no coincide con {event['event_id']}; no se mezclan sus datos.")


def _daily_sample(row):
    offset = _number(row.get("day_offset"))
    if offset is None or not offset.is_integer():
        raise ValueError("El desplazamiento de día climático no es un entero.")
    return {"day_offset": int(offset),
        "mean_temperature_c": _number(row.get("mean_temperature_c")),
        "mean_cloud_cover_pct": _number(row.get("mean_cloud_cover_pct")),
        "clear_probability_pct": _percentage(row.get("clear_probability")),
        "rain_probability_pct": _percentage(row.get("rain_probability")),
        "valid_cloud_years": _count(row.get("valid_cloud_years")),
        "valid_rain_years": _count(row.get("valid_rain_years"))}


def _location(row, daily, family, rank, root=sources.ROOT):
    latitude, longitude = _number(row.get("latitude")), _number(row.get("longitude"))
    if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Las coordenadas de un destino no son válidas.")
    name = str(row["name"])
    group = daily.loc[daily.name.eq(name)].sort_values("day_offset") if not daily.empty else pd.DataFrame()
    if not group.empty and group.day_offset.duplicated().any():
        raise ValueError(f"Hay días climáticos duplicados para {name}.")
    day_zero = group.loc[group.day_offset.eq(0)] if not group.empty else pd.DataFrame()
    climate = day_zero.iloc[0].to_dict() if not day_zero.empty else {}
    phase = str(row.get("local_phase", ""))
    if family == "lunar":
        duration = _number(row.get("published_phase_duration_s", row.get("phase_duration_s")))
        scope, obscuration = "global_lunar_phase", None
    elif phase in {"total", "annular"}:
        duration = _number(row.get("central_duration_s", row.get("phase_duration_s")))
        scope, obscuration = "local_central", _percentage(row.get("obscuration_fraction"))
    else:
        duration = _number(row.get("visible_eclipse_duration_s", row.get("phase_duration_s")))
        scope, obscuration = "local_visible", _percentage(row.get("obscuration_fraction"))
    if duration is not None and duration < 0:
        raise ValueError("La duración de un eclipse no puede ser negativa.")
    peak = row.get("event_time_utc_approx")
    if peak is None or pd.isna(peak):
        peak = None
    else:
        peak = str(peak)
        timestamp = datetime.fromisoformat(peak.replace("Z", "+00:00"))
        if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("La hora del máximo debe incluir la zona UTC.")
    cloud_years, rain_years = _count(row.get("valid_cloud_years")), _count(row.get("valid_rain_years"))
    validated = _boolean(row.get("ranking_validated_coverage"))
    enriched = enrich_country(row, root=root)
    has_climate = (cloud_years or 0) > 0 and (rain_years or 0) > 0 and \
        _number(row.get("clear_probability")) is not None and _number(row.get("rain_probability")) is not None
    return {"rank": rank, "name": name, "latitude": latitude, "longitude": longitude,
        "country_code": enriched.get("country_code") or "UNKNOWN",
        "country_name": enriched.get("country_name") or "País sin identificar",
        "country_source": enriched.get("country_source"),
        "climate_status": "ready" if has_climate and not group.empty else "partial" if has_climate else "not_prepared",
        "local_phase": phase, "peak_time_utc": peak, "obscuration_pct": obscuration,
        "phase_duration_s": duration, "duration_scope": scope,
        # False: la fase sale de la ventana de validez de los elementos NASA y no se extrapola.
        "duration_complete": _boolean(row.get("duration_complete")),
        "body_altitude_deg": _number(row.get("body_altitude_deg")),
        "clear_probability_pct": _percentage(row.get("clear_probability")),
        "rain_probability_pct": _percentage(row.get("rain_probability")),
        "cloud_probability_pct": _percentage(row.get("cloud_probability")),
        "valid_cloud_years": cloud_years, "valid_rain_years": rain_years,
        "ranking_provisional": validated is False or (cloud_years or 0) < 15 or (rain_years or 0) < 15,
        "mean_cloud_cover_pct": _number(climate.get("mean_cloud_cover_pct")),
        "mean_temperature_c": _number(climate.get("mean_temperature_c")),
        "mean_wind_speed_kmh": _number(climate.get("mean_wind_speed_kmh")),
        "climate_by_day": [_daily_sample(record) for record in group.to_dict("records")]}


def _prepared_event_output(event, root):
    event_date = _iso_date(event["event_date"])
    base = {key: event[key] for key in ("event_id", "event_date", "event_family", "eclipse_type")}
    base.update({"label": f"{event_date.strftime('%d-%m-%Y')} · Eclipse {event['event_family']} {TYPE_LABELS[event['eclipse_type']]}",
                 "status": "pending", "locations": [], "forecast_status": {"status": "not_prepared"}})
    directory = root / "data/gold/events" / event["event_date"]
    ranking_path = directory / "destination_ranking.csv"
    if not ranking_path.exists():
        return base
    errors = []
    try:
        ranking = pd.read_csv(ranking_path)
        _check_identity(ranking, event, required=True)
        report_path = directory / "pipeline_quality_report.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            _check_identity(report, event, required=True)
        else:
            report = {}
            errors.append("Falta el informe de calidad del evento.")
        if report and not isinstance(report.get("preparation_config"), dict):
            # Informes de versiones anteriores o de pruebas: no acreditan la configuración usada.
            base["error"] = ("La preparación guardada procede de una versión anterior del pipeline; "
                             "vuelve a preparar el evento.")
            return base
        failure_path = directory / FAILURE_MARKER
        if failure_path.exists():
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            if str(failure.get("failed_at_utc", "")) >= str(report.get("prepared_at_utc", "")):
                errors.append(f"La última actualización falló ({failure.get('failed_at_utc', 'fecha desconocida')}); "
                              "se muestran los resultados de la preparación anterior.")
        if "candidate_type" in ranking:
            ranking = ranking.loc[~ranking.candidate_type.map(_is_reference)].copy()
        if ranking.empty:
            base["error"] = "No hay destinos terrestres preparados para este evento."
            return base
        if "name" not in ranking or ranking.name.isna().any() or ranking.name.duplicated().any():
            raise ValueError("Los nombres de destinos están ausentes o duplicados.")
        sort_key = "destination_rank" if "destination_rank" in ranking else "rank"
        if sort_key in ranking:
            ranking = ranking.sort_values(sort_key, kind="stable", na_position="last")
        forecast_status = _saved_forecast_status(report)
        recorded_basis = report.get("ranking_basis")
        if recorded_basis is None and "ranking_basis" in ranking:
            bases = ranking.ranking_basis.dropna().astype(str).unique()
            recorded_basis = bases[0] if len(bases) == 1 else None
        base["ranking_basis"] = recorded_basis
        forecast_ranking = recorded_basis == "operational_forecast_proxy"
        if "ranking_basis" in ranking:
            forecast_ranking = forecast_ranking or ranking.ranking_basis.eq("operational_forecast_proxy").any()
        if forecast_ranking and forecast_status.get("status") not in {"available", "partially_available"}:
            historical_keys = ["phase_priority", "ranking_validated_coverage", "score"]
            if all(key in ranking for key in historical_keys):
                ranking = ranking.sort_values(historical_keys, ascending=False, kind="stable", na_position="last")
                base["ranking_basis"] = "historical_climatology"
            else:
                base["ranking_basis_warning"] = "El orden guardado dependía de una previsión que ya no está vigente; faltan métricas para recuperar el orden histórico."
        daily_path = directory / "climate_daily_summary.csv"
        if daily_path.exists():
            daily = pd.read_csv(daily_path)
            _check_identity(daily, event)
            if not {"name", "day_offset"}.issubset(daily.columns):
                raise ValueError("La tabla climática no identifica sus destinos y días.")
        else:
            daily = pd.DataFrame()
            errors.append("Falta el resumen climático de los días próximos al eclipse.")
        base["locations"] = [_location(row, daily, event["event_family"], rank, root)
            for rank, row in enumerate(ranking.to_dict("records"), 1)]
        climate = report.get("climate", {})
        if climate.get("failures") or climate.get("missing_key_values"):
            errors.append("La adquisición climática contiene años o valores ausentes.")
        requested_years = len(climate.get("years", []))
        if requested_years < MIN_RANKING_YEARS:
            errors.append(f"La climatología usa {requested_years} años; se necesitan al menos {MIN_RANKING_YEARS} "
                          "para una clasificación validada.")
        expected_days = set(range(-int(climate.get("days_before", 0)), int(climate.get("days_after", 0)) + 1))
        for location in base["locations"]:
            required = ("peak_time_utc", "phase_duration_s", "body_altitude_deg", "clear_probability_pct",
                "rain_probability_pct", "mean_cloud_cover_pct", "mean_temperature_c", "mean_wind_speed_kmh")
            # Una duración no calculable por la geometría no es un dato climático ausente.
            uncomputable = location["phase_duration_s"] is None and location["duration_complete"] is False
            incomplete = any(location[key] is None for key in required if not (key == "phase_duration_s" and uncomputable))
            if event["event_family"] == "solar" and location["obscuration_pct"] is None:
                incomplete = True
            if not expected_days.issubset({day["day_offset"] for day in location["climate_by_day"]}):
                incomplete = True
            for day in location["climate_by_day"]:
                if any(day[key] is None for key in ("mean_temperature_c", "mean_cloud_cover_pct", "clear_probability_pct", "rain_probability_pct")):
                    incomplete = True
                if (day["valid_cloud_years"] or 0) < max(1, requested_years) or (day["valid_rain_years"] or 0) < max(1, requested_years):
                    incomplete = True
            if (location["valid_cloud_years"] or 0) < max(1, requested_years) or (location["valid_rain_years"] or 0) < max(1, requested_years):
                incomplete = True
            if incomplete:
                location["climate_status"] = "partial"
                errors.append(f"Datos astronómicos o climáticos incompletos en {location['name']}.")
        base["forecast_status"] = forecast_status
        base["forecast_acquired_utc"] = base["forecast_status"].get("acquired_utc")
        base["rain_threshold_mm"] = _number(report.get("rain_threshold_mm", 0.1))
        base["cloud_threshold_pct"] = _number(report.get("cloud_threshold_pct", 20))
        base["climate_reference_years"] = climate.get("years", [])
        base["status"] = "partial" if errors else "ready"
        if errors:
            base["error"] = " ".join(errors)
        return base
    except (ValueError, KeyError, TypeError, OSError, pd.errors.ParserError) as error:
        base.update({"status": "pending", "locations": [], "error": str(error)})
        return base


def _same_location(left, right):
    """El nombre por sí solo no identifica una ciudad ni sus datos meteorológicos."""
    return left["name"] == right["name"] and \
        abs(left["latitude"] - right["latitude"]) <= 1e-5 and \
        abs(left["longitude"] - right["longitude"]) <= 1e-5


def _has_climate_observations(location):
    return (location.get("valid_cloud_years") or 0) > 0 and \
        (location.get("valid_rain_years") or 0) > 0 and \
        location.get("clear_probability_pct") is not None and \
        location.get("rain_probability_pct") is not None


def _country_catalog(locations, family):
    """Cada porcentaje es el máximo de los puntos evaluados, no del territorio."""
    grouped = {}
    for location in locations:
        code = location["country_code"]
        country = grouped.setdefault(code, {"country_code": code,
            "country_name": location["country_name"], "max_obscuration_pct": None,
            "max_body_altitude_deg": None, "max_phase_duration_s": None,
            "location_count": 0, "climate_location_count": 0})
        country["location_count"] += 1
        country["climate_location_count"] += int(_has_climate_observations(location))
        for key, field in (("max_obscuration_pct", "obscuration_pct"),
                           ("max_body_altitude_deg", "body_altitude_deg"),
                           ("max_phase_duration_s", "phase_duration_s")):
            value = location[field]
            if value is not None and (country[key] is None or value > country[key]):
                country[key] = value
    visibility_key = "max_obscuration_pct" if family == "solar" else "max_body_altitude_deg"
    return sorted(grouped.values(), key=lambda country: (
        -(country[visibility_key] if country[visibility_key] is not None else -math.inf),
        -(country["max_phase_duration_s"] if country["max_phase_duration_s"] is not None else -math.inf),
        country["country_name"]))


def _event_output(event, root):
    """Combina la cobertura astronómica con los destinos que ya tienen clima."""
    base = _prepared_event_output(event, root)
    prepared = base["locations"]
    locations = list(prepared)
    warnings = []
    catalog_status = "not_prepared"
    candidate_path = root / "data/silver/events" / event["event_date"] / "all_visible_candidates.csv"
    if candidate_path.exists():
        try:
            candidates = pd.read_csv(candidate_path)
            _check_identity(candidates, event, required=True)
            if not {"name", "latitude", "longitude"}.issubset(candidates.columns) or candidates.name.isna().any():
                raise ValueError("El catálogo de zonas no identifica nombres y coordenadas válidos.")
            if "candidate_type" in candidates:
                candidates = candidates.loc[~candidates.candidate_type.map(_is_reference)]
            if "visible" in candidates:
                candidates = candidates.loc[candidates.visible.map(_boolean).eq(True)]
            climate_fields = ("clear_probability", "rain_probability", "cloud_probability",
                "valid_cloud_years", "valid_rain_years", "ranking_validated_coverage")
            astronomy = [_location({**row, **dict.fromkeys(climate_fields)}, pd.DataFrame(),
                event["event_family"], 0, root) for row in candidates.to_dict("records")]
            # Validar toda la tabla antes de incorporar filas evita mezclas parciales
            # si un CSV está dañado. Gold sigue disponible cuando falla Silver.
            for location in astronomy:
                if not any(_same_location(location, known) for known in locations):
                    locations.append(location)
            catalog_status = "available"
        except (ValueError, KeyError, TypeError, OSError, pd.errors.ParserError) as error:
            catalog_status = "invalid"
            warnings.append(f"No se incorporó el catálogo astronómico de zonas: {error}")
    for rank, location in enumerate(locations, 1):
        location["rank"] = rank
    base["locations"] = locations
    base["countries"] = _country_catalog(locations, event["event_family"])
    base["astronomy_ready"] = bool(locations)
    climate_count = sum(_has_climate_observations(location) for location in locations)
    base["country_catalog_coverage"] = {"catalog_status": catalog_status,
        "location_count": len(locations), "climate_location_count": climate_count,
        "not_prepared_location_count": sum(location["climate_status"] == "not_prepared" for location in locations)}
    if locations and not prepared:
        base.setdefault("error", "Las ubicaciones astronómicas están disponibles; su climatología está pendiente de preparación.")
    if warnings:
        base["warnings"] = warnings
    return base


def build_planner_data(events: pd.DataFrame, planning_start: str, planning_end_exclusive: str,
                       root: Path = sources.ROOT, catalog_end_exclusive: str | None = None) -> dict:
    """Genera el snapshot sin red con los eventos de [inicio, fin del catálogo)."""
    catalog_end_exclusive = catalog_end_exclusive or planning_end_exclusive
    start, planning_end = _iso_date(planning_start), _iso_date(planning_end_exclusive)
    end = _iso_date(catalog_end_exclusive)
    if planning_end <= start or end < planning_end:
        raise ValueError("El final exclusivo de planificación debe ser posterior al inicio y no superar el del catálogo.")
    required = {"event_id", "event_date", "event_family", "eclipse_type"}
    if not required.issubset(events.columns):
        raise ValueError(f"El catálogo carece de columnas obligatorias: {sorted(required - set(events.columns))}.")
    filtered = []
    for event in events.to_dict("records"):
        event_date = _iso_date(event["event_date"])
        if not start <= event_date < end:
            continue
        family = event["event_family"]
        expected_prefix = {"solar": "SE", "lunar": "LE"}.get(family)
        if expected_prefix is None or not re.fullmatch(rf"NASA-{expected_prefix}-\d{{5}}", str(event["event_id"])):
            raise ValueError("La familia y el identificador NASA del evento no son compatibles.")
        allowed_types = {"total", "partial", "penumbral"} if family == "lunar" else {"total", "partial", "annular", "hybrid"}
        if event["eclipse_type"] not in allowed_types:
            raise ValueError(f"Tipo de eclipse no reconocido para {family}: {event['eclipse_type']}.")
        filtered.append(event)
    if len({event["event_id"] for event in filtered}) != len(filtered):
        raise ValueError("El catálogo de planificación contiene identificadores duplicados.")
    filtered.sort(key=lambda event: (event["event_date"], event["event_family"], event["event_id"]))
    output = [_event_output(event, Path(root)) for event in filtered]
    return {"schema_version": SCHEMA_VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "planning_start": planning_start, "planning_end_exclusive": planning_end_exclusive,
        "catalog_end_exclusive": catalog_end_exclusive,
        "solar_obscuration_threshold_pct": SOLAR_OBSCURATION_THRESHOLD_PCT,
        "events": output, "counts": {"events": len(output),
            **{status: sum(event["status"] == status for event in output) for status in ("ready", "partial", "pending")}},
        "attribution": {
            "astronomy": {"name": "NASA GSFC Eclipse Web Site", "url": "https://eclipse.gsfc.nasa.gov/"},
            "climate": {"name": "Open-Meteo · ERA5", "url": "https://open-meteo.com/en/docs/historical-weather-api", "license": "CC BY 4.0"},
            "climate_probabilities": "Frecuencias históricas a la hora aproximada del máximo local; cielo despejado <=20 % de nubosidad; lluvia >=0,1 mm por hora salvo umbral configurado en el informe de cada evento.",
            "climate_by_day": "Estadísticas históricas a la hora del evento en cada día relativo; no son medias de las 24 horas.",
            "solar_obscuration": "Porcentaje del disco solar ocultado, sin ajuste meteorológico.",
            "country_visibility": "Máximo del porcentaje de ocultación solar entre los puntos terrestres evaluados de cada país; no es el porcentaje del territorio ni garantiza el máximo de todo el país. Para eclipses lunares se conserva la altura lunar, sin inventar un porcentaje de ocultación solar.",
            "country_climate_coverage": "El catálogo de zonas incluye puntos con geometría astronómica cuya climatología aún no se ha preparado. La cobertura se indica por zona y país; no se copian datos meteorológicos de otra ubicación.",
            "lunar_duration": "Duración global publicada de la fase lunar; no garantiza que la fase entera sea visible desde el destino.",
            "ranking": "Orden de los destinos evaluados; referencias astronómicas excluidas. Cobertura inferior a 15 años marcada como provisional."}}


def export_planner_data(events: pd.DataFrame, planning_start: str, planning_end_exclusive: str,
                        root: Path = sources.ROOT, catalog_end_exclusive: str | None = None) -> dict:
    """Escribe JSON y JavaScript equivalentes para servir el selector sin API externa."""
    payload = build_planner_data(events, planning_start, planning_end_exclusive, root, catalog_end_exclusive)
    try:
        world_map = export_world_map(root)
        payload["map_status"] = {"status": "available", "feature_count": len(world_map["features"])}
    except (ValueError, KeyError, TypeError, OSError) as error:
        # La cartografía mejora el selector, pero sus datos deben seguir siendo
        # utilizables cuando el recurso local no está disponible.
        payload["map_status"] = {"status": "unavailable", "reason": str(error)}
    serialized = json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2)
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    directory = Path(root) / "data/gold"
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in (("planner_data.json", serialized + "\n"),
                              ("planner_data.js", "window.ECLIPSE_PLANNER_DATA = " + serialized + ";\n")):
        destination = directory / filename
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    return payload


def export_current_window(start=None, future_years=3, client=None, offline=True, refresh=False,
                          root: Path = sources.ROOT) -> dict:
    """Exporta la ventana [hoy, hoy + años) y el margen posterior desde los catálogos NASA de Bronze."""
    planning_start, planning_end = planning_window(start, future_years)
    _, catalog_end = planning_window(planning_end, CATALOG_MARGIN_YEARS)
    events = catalog_between(planning_start, catalog_end, client, refresh, offline, Path(root))
    return export_planner_data(events, planning_start, planning_end, root, catalog_end)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="Inicio ISO de la ventana; por defecto hoy UTC.")
    parser.add_argument("--future-years", type=int, default=3, help="Horizonte de la ventana, 3 años por defecto (1–10).")
    args = parser.parse_args(argv)
    try:
        payload = export_current_window(args.as_of, args.future_years, root=sources.ROOT)
        counts = payload["counts"]
        print(f"Ventana {payload['planning_start']} hasta {payload['planning_end_exclusive']} (fin excluido); "
              f"se exportan {counts['events']} eclipses hasta {payload['catalog_end_exclusive']}: "
              f"{counts['ready']} preparados, {counts['partial']} parciales, {counts['pending']} pendientes.")
        print("data/gold/planner_data.json y data/gold/planner_data.js")
        return 0
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(1, f"Error de exportación: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
