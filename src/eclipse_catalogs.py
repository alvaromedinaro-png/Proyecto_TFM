"""Descubrimiento de los catálogos solar y lunar publicados por NASA hasta el año 3000."""
from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import numpy as np
import pandas as pd

try:
    from . import eclipse_sources as sources
except ImportError:
    import eclipse_sources as sources

INDEXES = {"solar": "https://eclipse.gsfc.nasa.gov/SEcat5/catalog.html",
           "lunar": "https://eclipse.gsfc.nasa.gov/LEcat5/LEcatalog.html"}


def discover_centuries(html, source_url):
    links = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(source_url, anchor["href"])
        match = re.search(r"/(SE|LE)(\d{4})-(\d{4})\.html$", url)
        if match and int(match[2]) >= 2001:
            links[int(match[2])] = url
    if not links:
        raise ValueError(f"El índice NASA no contiene catálogos futuros reconocibles: {source_url}")
    return [links[year] for year in sorted(links)]


def parse_lunar_catalog(html, source_url):
    records = []
    for block in BeautifulSoup(html, "html.parser").find_all("pre"):
        for line in block.decode_contents().splitlines():
            row = BeautifulSoup(line, "html.parser")
            tokens = row.get_text().split()
            if not tokens or not re.fullmatch(r"\d{5}", tokens[0]):
                continue
            if len(tokens) != 18:
                raise ValueError(f"Fila lunar no reconocida: {tokens}")
            date = f"{int(tokens[1]):04d}-{sources.MONTHS[tokens[2]]:02d}-{int(tokens[3]):02d}"
            delta_t = float(tokens[5])
            ut = datetime.fromisoformat(f"{date}T{tokens[4]}").replace(tzinfo=timezone.utc) - timedelta(seconds=delta_t)
            anchor = row.find("a", href=True)
            records.append({"event_id": f"NASA-LE-{tokens[0]}", "event_date": date,
                "catalog_number": tokens[0], "event_family": "lunar",
                "eclipse_type": {"T": "total", "P": "partial", "N": "penumbral"}[tokens[8][0]],
                "type_code": tokens[8], "greatest_eclipse_td": tokens[4], "delta_t_s": delta_t,
                "greatest_eclipse_ut_approx": ut.isoformat(), "saros_number": int(tokens[7]),
                "gamma": float(tokens[10]), "penumbral_magnitude": float(tokens[11]),
                "eclipse_magnitude": float(tokens[12]),
                "penumbral_duration_s": float(tokens[13]) * 60 if tokens[13] != "-" else np.nan,
                "partial_duration_s": float(tokens[14]) * 60 if tokens[14] != "-" else np.nan,
                "total_duration_s": float(tokens[15]) * 60 if tokens[15] != "-" else np.nan,
                "greatest_latitude": sources.coordinate(tokens[16]),
                "greatest_longitude": sources.coordinate(tokens[17]),
                "source_url": source_url, "map_url": urljoin(source_url, anchor["href"]),
                "geometry_supported": True, "geometry_method": "lunar_altitude_at_global_maximum"})
    catalog = pd.DataFrame(records)
    if catalog.empty or catalog.event_id.duplicated().any():
        raise ValueError("Catálogo lunar vacío o duplicado.")
    return catalog


def planning_window(as_of=None, future_years=3):
    """Ventana de años naturales [inicio, fin), ajustando el 29 de febrero."""
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError("La fecha de corte debe tener formato YYYY-MM-DD.")
    start = datetime.fromisoformat(as_of).date()
    if not isinstance(future_years, int) or isinstance(future_years, bool) or not 1 <= future_years <= 10:
        raise ValueError("El horizonte de planificación debe ser de 1 a 10 años.")
    try:
        end = start.replace(year=start.year + future_years)
    except ValueError:
        # El único día que puede no existir al sumar años naturales es el 29/02.
        if start.month != 2 or start.day != 29:
            raise
        end = start.replace(year=start.year + future_years, day=28)
    return start.isoformat(), end.isoformat()


def _collect_catalogs(client=None, refresh=False, offline=False, root=sources.ROOT, begin=None, end=None):
    """Descubre los siglos necesarios; un fallo impide declarar completo el catálogo."""
    # Sin red solo se leen copias verificadas, así que no hace falta sesión HTTP.
    client = client or (None if offline else sources.session())
    frames, provenance = [], []
    for family, index_url in INDEXES.items():
        index_file = root / "data/bronze/nasa" / f"{family}_index.html"
        index_body = sources.fetch(index_url, index_file, client, refresh=refresh, offline=offline)
        urls = discover_centuries(index_body, index_url)
        for url in urls:
            if begin and end:
                century = re.search(r"(\d{4})-(\d{4})\.html$", url)
                last_day = datetime.fromisoformat(end).date() - timedelta(days=1)
                if int(century[2]) < int(begin[:4]) or int(century[1]) > last_day.year:
                    continue
            filename = url.rsplit("/", 1)[-1]
            raw = root / "data/bronze/nasa" / filename
            body = sources.fetch(url, raw, client, refresh=refresh, offline=offline)
            catalog = sources.parse_catalog(body, url) if family == "solar" else parse_lunar_catalog(body, url)
            if family == "solar":
                catalog["event_family"] = "solar"
                catalog["geometry_supported"] = catalog.besselian_url.notna()
                catalog["geometry_method"] = "besselian_local_circumstances"
            frames.append(catalog)
            provenance.append({"family": family, "url": url, "file": raw.relative_to(root).as_posix(),
                               "sha256": hashlib.sha256(body).hexdigest(), "rows": len(catalog)})
    if not frames:
        raise ValueError("NASA no publica catálogos para la ventana de planificación solicitada.")
    all_events = pd.concat(frames, ignore_index=True).sort_values(["event_date", "event_family"]).reset_index(drop=True)
    if all_events.event_id.duplicated().any():
        raise ValueError("Identificadores repetidos entre siglos NASA.")
    return all_events, provenance


def load_all_catalogs(client=None, refresh=False, offline=False, as_of=None, root=sources.ROOT):
    """Catálogo extendido de investigación; el producto utiliza load_planning_catalog."""
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    datetime.fromisoformat(as_of)
    all_events, provenance = _collect_catalogs(client, refresh, offline, root)
    future = all_events.loc[all_events.event_date.ge(as_of)].copy()
    for layer in ["silver", "gold"]:
        folder = root / "data" / layer
        folder.mkdir(parents=True, exist_ok=True)
        sources.save_csv(all_events, folder / "all_eclipse_catalog.csv")
        sources.save_csv(future, folder / "future_eclipse_catalog.csv")
    sources.save_json(root / "data/gold/future_eclipse_catalog.metadata.json", {
        "as_of": as_of, "coverage_end": all_events.event_date.max(), "rows": len(future),
        "family_counts": future.event_family.value_counts().to_dict(), "century_sources": provenance,
        "catalog_complete": True, "indexes": INDEXES,
        "attribution": "Eclipse Predictions by Fred Espenak, NASA/GSFC"})
    return all_events, future


def load_planning_catalog(client=None, refresh=False, offline=False, as_of=None, future_years=3, root=sources.ROOT):
    """Adquiere solo los siglos que cubren el horizonte limitado del selector."""
    begin, end = planning_window(as_of, future_years)
    catalog, provenance = _collect_catalogs(client, refresh, offline, root, begin, end)
    events = catalog.loc[catalog.event_date.ge(begin) & catalog.event_date.lt(end)].copy().reset_index(drop=True)
    for layer in ["silver", "gold"]:
        folder = root / "data" / layer
        folder.mkdir(parents=True, exist_ok=True)
        sources.save_csv(events, folder / "planning_eclipse_catalog.csv")
    sources.save_json(root / "data/gold/planning_eclipse_catalog.metadata.json", {
        "planning_start": begin, "planning_end_exclusive": end, "future_years": future_years,
        "rows": len(events), "family_counts": events.event_family.value_counts().to_dict(),
        "century_sources": provenance, "catalog_complete": True, "indexes": INDEXES,
        "attribution": "Eclipse Predictions by Fred Espenak, NASA/GSFC"})
    return events


def catalog_between(begin, end, client=None, refresh=False, offline=False, root=sources.ROOT):
    """Eventos NASA en [begin, end) sin reescribir el catálogo de planificación guardado."""
    catalog, _ = _collect_catalogs(client, refresh, offline, root, begin, end)
    return catalog.loc[catalog.event_date.ge(begin) & catalog.event_date.lt(end)].copy().reset_index(drop=True)


def select_event(catalog, date, family=None):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("La fecha debe tener formato YYYY-MM-DD.")
    datetime.fromisoformat(date)
    matches = catalog.loc[catalog.event_date.eq(date)]
    if family:
        matches = matches.loc[matches.event_family.eq(family)]
    if len(matches) != 1:
        raise ValueError(f"La fecha {date} no identifica un único eclipse del catálogo NASA; indica familia si es necesario.")
    return matches.iloc[0].to_dict()
