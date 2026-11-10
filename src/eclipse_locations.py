"""Candidatos terrestres NASA y circunstancias locales aproximadas de eclipses."""
import csv
from datetime import datetime, timedelta, timezone
import json
import re

from bs4 import BeautifulSoup
import numpy as np
import pandas as pd

try:
    from . import eclipse_sources as sources
    from .eclipse_geography import enrich_country
except ImportError:
    import eclipse_sources as sources
    from eclipse_geography import enrich_country

CITY_BASE = "https://eclipse.gsfc.nasa.gov/JSEX/"
# Cambiarla invalida las preparaciones guardadas con otra selección de candidatos.
SHORTLIST_POLICY = "references_quarter_country_leaders_v2"


def load_cities(client, root=sources.ROOT, offline=False, refresh=False):
    records = []
    for region in ["na", "sa", "eu", "af", "as", "au"]:
        url = CITY_BASE + f"city-{region}.js"
        raw = root / "data/bronze/nasa" / f"city-{region}.js"
        body = sources.fetch(url, raw, client, refresh=refresh, offline=offline)
        for line in body.decode("utf-8", errors="replace").splitlines():
            if not line.lstrip().startswith('"'):
                continue
            values = next(csv.reader([line.strip().rstrip(",")]))
            if len(values) != 9 or values[0].startswith("-"):
                continue
            numbers = [float(value) for value in values[1:]]
            records.append({"name": values[0], "latitude": sum(numbers[index] / scale for index, scale in [(0, 1), (1, 60), (2, 3600)]),
                "longitude": -sum(numbers[index] / scale for index, scale in [(3, 1), (4, 60), (5, 3600)]),
                "elevation_m": numbers[6], "candidate_type": "ciudad NASA", "location_source": url})
    cities = pd.DataFrame(records).drop_duplicates(["name", "latitude", "longitude"]).reset_index(drop=True)
    if cities.empty or not cities.latitude.between(-90, 90).all() or not cities.longitude.between(-180, 180).all():
        raise ValueError("La lista de ciudades NASA está vacía o tiene coordenadas inválidas.")
    cities = pd.DataFrame([enrich_country(row, root) for row in cities.to_dict("records")])
    folder = root / "data/silver"
    folder.mkdir(parents=True, exist_ok=True)
    sources.save_csv(cities, folder / "nasa_cities.csv")
    return cities


def parse_besselian(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text()
    t0 = re.search(r"(\d+\.\d+)\s+TDT\s*\(=t0\)", text)
    if not t0:
        raise ValueError("No se encuentra t0 en los elementos besselianos NASA.")
    rows = {}
    for block in soup.find_all("pre"):
        pending = None
        for line in block.get_text().splitlines():
            match = re.match(r"^\s*([0-3])\s+([-+]?\d+\.\d+.*)$", line)
            if match:
                values = [float(value) for value in re.findall(r"[-+]?\d+\.\d+", match[2])]
                row = int(match[1])
                rows[row] = values
                pending = row if len(values) < 6 else None
            elif pending is not None and re.fullmatch(r"\s*(?:[-+]?\d+\.\d+\s*){1,6}", line):
                rows[pending].extend(float(value) for value in re.findall(r"[-+]?\d+\.\d+", line))
                pending = None
    if set(rows) != {0, 1, 2, 3} or any(len(values) != 6 for values in rows.values()):
        raise ValueError("Tabla de coeficientes besselianos incompleta; no se sustituyen valores ausentes por cero.")
    f1 = re.search(r"tan\s+f1\s*=\s*([\d.]+)", text)
    f2 = re.search(r"tan\s+f2\s*=\s*([\d.]+)", text)
    if not f1 or not f2:
        raise ValueError("Faltan los ángulos de los conos de sombra.")
    return {"t0_td_hours": float(t0[1]), "coefficients": [rows[index] for index in range(4)],
            "tan_f1": float(f1[1]), "tan_f2": float(f2[1])}


def solar_state(elements, event, latitude, longitude, elevation, t):
    coeff = np.array(elements["coefficients"])
    t = np.asarray(t)
    x, y, declination, l1, l2, mu = [np.polynomial.polynomial.polyval(t, coeff[:, column]) for column in range(6)]
    phi = np.radians(latitude)
    reduced = np.arctan(0.99664719 * np.tan(phi))
    rho_sin = 0.99664719 * np.sin(reduced) + elevation / 6378140 * np.sin(phi)
    rho_cos = np.cos(reduced) + elevation / 6378140 * np.cos(phi)
    d = np.radians(declination)
    h = np.radians(mu + longitude) - event["delta_t_s"] * (2 * np.pi / 86164.0905)
    observer_x = rho_cos * np.sin(h)
    observer_y = rho_sin * np.cos(d) - rho_cos * np.cos(h) * np.sin(d)
    observer_z = rho_sin * np.sin(d) + rho_cos * np.cos(h) * np.cos(d)
    separation = np.hypot(x - observer_x, y - observer_y)
    outer = l1 - observer_z * elements["tan_f1"]
    inner = l2 - observer_z * elements["tan_f2"]
    magnitude = (outer - separation) / (outer + inner)
    altitude = np.degrees(np.arcsin(np.clip(np.sin(phi) * np.sin(d) + np.cos(phi) * np.cos(d) * np.cos(h), -1, 1)))
    return separation, outer, inner, magnitude, altitude


def disk_obscuration(separation, outer, inner):
    sun, moon = (outer + inner) / 2, (outer - inner) / 2
    if sun <= 0 or moon <= 0 or separation >= sun + moon:
        return 0.0
    if separation <= abs(sun - moon):
        return min(1.0, (moon / sun) ** 2)
    a = np.arccos(np.clip((separation**2 + sun**2 - moon**2) / (2 * separation * sun), -1, 1))
    b = np.arccos(np.clip((separation**2 + moon**2 - sun**2) / (2 * separation * moon), -1, 1))
    triangle = max(0.0, (-separation + sun + moon) * (separation + sun - moon) * (separation - sun + moon) * (separation + sun + moon))
    return float(np.clip((sun**2 * a + moon**2 * b - np.sqrt(triangle) / 2) / (np.pi * sun**2), 0, 1))


def solar_circumstances(elements, event, latitude, longitude, elevation=0):
    # Buscar máximo visible en la ventana de validez local de los polinomios (±3 h).
    grid = np.linspace(-3, 3, 241)
    state = solar_state(elements, event, latitude, longitude, elevation, grid)
    visible_magnitude = np.where(state[4] > 0, state[3], -np.inf)
    index = int(np.argmax(visible_magnitude))
    if visible_magnitude[index] <= 0:
        return None
    low, high = grid[max(0, index - 1)], grid[min(len(grid) - 1, index + 1)]
    for _ in range(30):
        left, right = low + (high - low) / 3, high - (high - low) / 3
        ls = solar_state(elements, event, latitude, longitude, elevation, left)
        rs = solar_state(elements, event, latitude, longitude, elevation, right)
        lv, rv = (ls[3] if ls[4] > 0 else -np.inf), (rs[3] if rs[4] > 0 else -np.inf)
        if lv < rv:
            low = left
        else:
            high = right
    t = (low + high) / 2
    m, outer, inner, magnitude, altitude = [float(value) for value in solar_state(elements, event, latitude, longitude, elevation, t)]
    if magnitude <= 0 or altitude <= 0:
        return None
    central = m < abs(inner)
    phase = ("total" if inner < 0 else "annular") if central else "partial"
    contacts = []
    if central:
        for direction in [-1, 1]:
            inside, outside = t, t
            for _ in range(360):
                outside += direction / 600  # pasos de seis segundos
                c = solar_state(elements, event, latitude, longitude, elevation, outside)
                if float(c[0] - abs(c[2])) >= 0 or float(c[4]) <= 0:
                    break
                inside = outside
            else:
                raise ValueError("No se encuentra contacto de fase central dentro de la ventana de cálculo.")
            for _ in range(30):
                midpoint = (inside + outside) / 2
                c = solar_state(elements, event, latitude, longitude, elevation, midpoint)
                if float(c[0] - abs(c[2])) < 0 and float(c[4]) > 0:
                    inside = midpoint
                else:
                    outside = midpoint
            contacts.append((inside + outside) / 2)
    duration = abs(contacts[1] - contacts[0]) * 3600 if contacts else 0.0
    external_contacts, duration_complete = [], True
    for direction in [-1, 1]:
        inside, outside = t, t
        for _ in range(360):
            outside += direction / 60
            if not -3 <= outside <= 3:
                duration_complete = False
                break
            c = solar_state(elements, event, latitude, longitude, elevation, outside)
            if float(c[0] - c[1]) >= 0 or float(c[4]) <= 0:
                break
            inside = outside
        else:
            duration_complete = False
        if not duration_complete:
            break
        for _ in range(30):
            midpoint = (inside + outside) / 2
            c = solar_state(elements, event, latitude, longitude, elevation, midpoint)
            if float(c[0] - c[1]) < 0 and float(c[4]) > 0:
                inside = midpoint
            else:
                outside = midpoint
        external_contacts.append((inside + outside) / 2)
    visible_duration = abs(external_contacts[1] - external_contacts[0]) * 3600 if duration_complete else np.nan
    instant = datetime.fromisoformat(event["event_date"]).replace(tzinfo=timezone.utc) + timedelta(
        hours=elements["t0_td_hours"] + t, seconds=-event["delta_t_s"])
    return {"visible": True, "local_phase": phase, "local_magnitude": magnitude,
        "obscuration_fraction": disk_obscuration(m, outer, inner), "body_altitude_deg": altitude,
        "event_time_utc_approx": instant.isoformat(), "central_duration_s": duration,
        "visible_eclipse_duration_s": visible_duration, "duration_complete": duration_complete,
        "phase_duration_s": duration if central else visible_duration,
        "geometry_method": "besselian_polynomial_approximation"}


def lunar_circumstances(event, latitude, longitude):
    # El catálogo publica el punto sublunar en el máximo; coseno de distancia angular = seno de altura.
    phi, zenith = np.radians(latitude), np.radians(event["greatest_latitude"])
    cosine = np.sin(phi) * np.sin(zenith) + np.cos(phi) * np.cos(zenith) * np.cos(np.radians(longitude - event["greatest_longitude"]))
    altitude = float(np.degrees(np.arcsin(np.clip(cosine, -1, 1))))
    if altitude <= 0:
        return None
    duration_field = {"total": "total_duration_s", "partial": "partial_duration_s", "penumbral": "penumbral_duration_s"}[event["eclipse_type"]]
    phase_duration = event.get(duration_field, np.nan)
    return {"visible": True, "local_phase": event["eclipse_type"], "local_magnitude": event["eclipse_magnitude"],
        "obscuration_fraction": np.nan, "body_altitude_deg": altitude,
        "event_time_utc_approx": event["greatest_eclipse_ut_approx"],
        "central_duration_s": event.get("total_duration_s", 0) if pd.notna(event.get("total_duration_s")) else 0,
        "phase_duration_s": phase_duration, "published_phase_duration_s": phase_duration,
        "visible_eclipse_duration_s": np.nan, "duration_complete": False,
        "geometry_method": "sublunar_point_altitude_at_maximum"}


def prepare_locations(event, client, root=sources.ROOT, locations_file=None, max_locations=12,
                      offline=False, refresh=False):
    if max_locations < 1:
        raise ValueError("Se necesita al menos una ubicación candidata.")
    if locations_file:
        candidates = pd.read_csv(locations_file)
        if not {"name", "latitude", "longitude"}.issubset(candidates.columns):
            raise ValueError("Las ubicaciones requieren name,latitude,longitude.")
        candidates = candidates.copy()
        candidates["candidate_type"] = "destino aportado"
        candidates["location_source"] = str(locations_file)
    else:
        candidates = load_cities(client, root, offline, refresh)
    if candidates["name"].isna().any() or candidates["name"].duplicated().any():
        raise ValueError("Nombres de ubicaciones ausentes o repetidos.")
    for coordinate_name, bound in [("latitude", 90), ("longitude", 180)]:
        candidates[coordinate_name] = pd.to_numeric(candidates[coordinate_name], errors="coerce")
        if not candidates[coordinate_name].between(-bound, bound).all():
            raise ValueError("Coordenadas de candidatos inválidas.")
    if "elevation_m" not in candidates:
        candidates["elevation_m"] = 0.0
        candidates["elevation_is_assumed"] = True
    else:
        candidates["elevation_m"] = pd.to_numeric(candidates.elevation_m, errors="coerce")
        candidates["elevation_is_assumed"] = candidates.elevation_m.isna()
        candidates["elevation_m"] = candidates.elevation_m.fillna(0)
        if not np.isfinite(candidates.elevation_m).all():
            raise ValueError("Elevación de candidatos no finita.")
    elements = None
    reference_status = "not_requested_for_user_locations"
    if not locations_file:
        reference_records = []
        if event["event_family"] == "solar" and isinstance(event.get("path_url"), str):
            try:
                raw = root / "data/bronze/events" / event["event_date"] / "nasa_path.html"
                path = sources.parse_path(sources.fetch(event["path_url"], raw, client, refresh=refresh, offline=offline))
                indices = np.unique(np.linspace(0, len(path) - 1, 8, dtype=int))
                reference_records = [{"name": f"Referencia central NASA {index + 1}", "latitude": row.latitude,
                    "longitude": row.longitude, "elevation_m": 0, "elevation_is_assumed": True, "candidate_type": "referencia astronómica",
                    "location_source": event["path_url"]} for index, row in enumerate(path.iloc[indices].itertuples())]
                reference_status = "central_path_available"
            except (ValueError, KeyError, sources.requests.RequestException) as error:
                reference_status = f"central_path_unavailable: {error}"
        else:
            reference_records = [{"name": "Referencia NASA del máximo", "latitude": event["greatest_latitude"],
                "longitude": event["greatest_longitude"], "elevation_m": 0, "elevation_is_assumed": True, "candidate_type": "referencia astronómica",
                "location_source": event["source_url"]}]
            reference_status = "global_maximum_reference"
        if reference_records:
            candidates = pd.concat([candidates, pd.DataFrame(reference_records)], ignore_index=True)
    if event["event_family"] == "solar":
        url = event.get("besselian_url")
        if not isinstance(url, str):
            raise ValueError("NASA no proporciona elementos para este evento.")
        path = root / "data/bronze/events" / event["event_date"] / "besselian.html"
        body = sources.fetch(url, path, client, refresh=refresh, offline=offline)
        elements = parse_besselian(body)
        sources.save_json(path.with_suffix(".parsed.json"), elements)
    records = []
    for row in candidates.to_dict("records"):
        if event["event_family"] == "solar":
            circumstances = solar_circumstances(elements, event, row["latitude"], row["longitude"], row.get("elevation_m", 0))
        else:
            circumstances = lunar_circumstances(event, row["latitude"], row["longitude"])
        if circumstances:
            records.append({**row, **circumstances, "event_id": event["event_id"], "event_date": event["event_date"]})
    if not records:
        raise ValueError("Ningún candidato tiene el eclipse sobre el horizonte; aporta ubicaciones adecuadas.")
    evaluated = pd.DataFrame(records)
    evaluated["phase_priority"] = evaluated.local_phase.map({"total": 3, "annular": 2, "partial": 1, "penumbral": 0})
    order = (["body_altitude_deg", "local_magnitude"] if event["event_family"] == "lunar"
             else ["phase_priority", "central_duration_s", "local_magnitude", "body_altitude_deg"])
    evaluated = evaluated.sort_values(order, ascending=False)
    folder = root / "data/silver/events" / event["event_date"]
    folder.mkdir(parents=True, exist_ok=True)
    sources.save_csv(evaluated, folder / "all_visible_candidates.csv")
    if not locations_file and max_locations > 1:
        references = evaluated.loc[evaluated.candidate_type.eq("referencia astronómica")]
        cities = evaluated.loc[~evaluated.candidate_type.eq("referencia astronómica")]
        # Las referencias no llegan al selector: ocupan como máximo una cuarta parte.
        n_references = min(len(references), max(1, max_locations // 4))
        city_slots = max_locations - n_references
        # La mejor ciudad de cada país primero, para que la climatología cubra más países.
        leaders = cities.drop_duplicates("country_code") if "country_code" in cities else cities.iloc[0:0]
        chosen = pd.concat([leaders.head(city_slots), cities]).drop_duplicates("name").head(city_slots)
        chosen = cities.loc[cities.name.isin(chosen.name)]
        shortlisted = pd.concat([references.head(n_references), chosen])
        # Rellenar plazas si solo hay una clase de candidato visible.
        remaining = evaluated.loc[~evaluated.name.isin(shortlisted.name)]
        shortlist = pd.concat([shortlisted, remaining.head(max_locations - len(shortlisted))]).reset_index(drop=True)
    else:
        shortlist = evaluated.head(max_locations).reset_index(drop=True)
    sources.save_csv(shortlist, folder / "locations.csv")
    return shortlist, {"evaluated_candidates": len(candidates), "visible_candidates": len(evaluated),
        "climate_shortlist": len(shortlist), "source": "NASA city lists or user CSV",
        "reference_status": reference_status,
        "method": "local maximum above geometric horizon; terrain and refraction not included"}
