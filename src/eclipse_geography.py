"""Países y cartografía local de Natural Earth, sin geocodificación en red.

Los nombres de país publicados con las ciudades NASA tienen prioridad sobre
polígonos de escala 1:110m, que pueden omitir islas pequeñas y enclaves.
"""
import csv
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import unicodedata

try:
    from . import eclipse_sources as sources
except ImportError:
    import eclipse_sources as sources


RAW_GEOGRAPHY = Path("data/bronze/geography/ne_110m_admin_0_countries.json")
CANADIAN_PROVINCES = set("AB BC MB NB NF NL NS NT NU ON PE QC SK YT".split())
US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())
EXTRA_COUNTRIES = {
    "AW": "Aruba", "BH": "Baréin", "BB": "Barbados", "BM": "Bermudas", "BN": "Brunéi",
    "CV": "Cabo Verde", "CW": "Curazao", "FK": "Islas Malvinas", "FO": "Islas Feroe",
    "GI": "Gibraltar", "GP": "Guadalupe", "GD": "Granada", "GU": "Guam", "HK": "Hong Kong",
    "MO": "Macao", "MT": "Malta", "MQ": "Martinica", "MU": "Mauricio", "MP": "Islas Marianas del Norte",
    "NC": "Nueva Caledonia", "PF": "Polinesia Francesa", "PN": "Islas Pitcairn", "PR": "Puerto Rico",
    "SH": "Santa Elena, Ascensión y Tristán de Acuña", "SG": "Singapur", "ST": "Santo Tomé y Príncipe",
    "LC": "Santa Lucía", "VC": "San Vicente y las Granadinas", "WS": "Samoa",
    "VG": "Islas Vírgenes Británicas", "VI": "Islas Vírgenes de Estados Unidos",
    "TW": "Taiwán", "GB": "Reino Unido", "CI": "Costa de Marfil", "CZ": "Chequia",
    "MK": "Macedonia del Norte", "BA": "Bosnia y Herzegovina", "XK": "Kosovo"}
ALIASES = {
    "ASCENSION IS.": "SH", "AZORES": "PT", "CANARY ISLANDS": "ES", "KALININGRAD": "RU",
    "BOSNIA & HERZEGOVINA": "BA", "CAPE VERDE": "CV", "CZECH REPUBLIC": "CZ",
    "DEM. REP. OF THE CONGO": "CD", "IVORY COAST": "CI", "MACEDONIA": "MK",
    "SAO TOME & PRINCIPE": "ST", "UK": "GB", "USA": "US", "ST. LUCIA": "LC",
    "ST. VINCENT & GRENADINES": "VC", "TRINIDAD & TOBAGO": "TT", "U.S. VIRGIN IS.": "VI",
    "UK VIRGIN IS.": "VG", "NORTHERN MARIANA IS.": "MP", "FIJI ISLANDS": "FJ",
    "WESTERN SAMOA": "WS", "SERBIA AND MONTENEGRO": "RS",
    "BAHRAIN": "BH", "BARBADOS": "BB", "BERMUDA": "BM", "BRUNEI": "BN", "GRENADA": "GD",
    "GIBRALTAR": "GI", "GUADELOUPE": "GP", "GUAM": "GU", "HONG KONG": "HK",
    "MACAU": "MO", "MALTA": "MT", "MARTINIQUE": "MQ", "NEW CALEDONIA": "NC",
    "FRENCH POLYNESIA": "PF", "PITCAIRN": "PN", "PUERTO RICO": "PR", "SINGAPORE": "SG",
    "FALKLAND ISLANDS": "FK", "ARUBA": "AW", "CURACAO": "CW", "WESTERN SAHARA": "EH"}


def _normalized(value):
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().upper())


def _data_path(root):
    local = Path(root) / RAW_GEOGRAPHY
    # La cartografía es un recurso común, también al exportar datasets en otro directorio.
    return local if local.is_file() else sources.ROOT / RAW_GEOGRAPHY


@lru_cache(maxsize=8)
def _country_data(filename):
    names, aliases, features, polygons = dict(EXTRA_COUNTRIES), {}, [], []
    path = Path(filename)
    original = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"features": []}
    for feature in original["features"]:
        properties = feature["properties"]
        iso = properties.get("ISO_A2_EH", properties.get("ISO_A2"))
        if not re.fullmatch(r"[A-Z]{2}", str(iso)):
            iso = "NE-" + properties["ADM0_A3"]
        label = EXTRA_COUNTRIES.get(iso, properties.get("NAME_ES") or properties["NAME"])
        names[iso] = label
        for key in ("NAME", "NAME_EN", "NAME_ES", "NAME_LONG", "ADMIN", "FORMAL_EN", "ISO_A3", "ISO_A3_EH"):
            if properties.get(key) and properties[key] != "-99":
                aliases[_normalized(properties[key])] = iso
        geometry = feature["geometry"]
        features.append({"type": "Feature", "properties": {"country_code": iso, "country_name": label}, "geometry": geometry})
        groups = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for rings in groups:
            outer = rings[0]
            bounds = (min(p[0] for p in outer), min(p[1] for p in outer), max(p[0] for p in outer), max(p[1] for p in outer))
            polygons.append((iso, rings, bounds))
    for iso, label in names.items():
        aliases[_normalized(iso)] = iso
        aliases[_normalized(label)] = iso
    aliases.update({_normalized(key): value for key, value in ALIASES.items()})
    return names, aliases, features, polygons


def _inside_ring(longitude, latitude, ring):
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > latitude) != (y2 > latitude):
            crossing = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing:
                inside = not inside
        previous = current
    return inside


def _polygon_country(latitude, longitude, polygons):
    for iso, rings, (west, south, east, north) in polygons:
        if west <= longitude <= east and south <= latitude <= north:
            if _inside_ring(longitude, latitude, rings[0]) and not any(_inside_ring(longitude, latitude, hole) for hole in rings[1:]):
                return iso
    return None


@lru_cache(maxsize=16)
def _csv_countries(filename, modified):
    with Path(filename).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def enrich_country(row, root=sources.ROOT):
    """Resuelve país/territorio con procedencia explícita; no consulta la red."""
    names, aliases, _, polygons = _country_data(str(_data_path(root)))
    try:
        latitude, longitude = float(row["latitude"]), float(row["longitude"])
    except (KeyError, ValueError, TypeError):
        latitude = longitude = math.nan
    def result(iso, method):
        return {**row, "country_code": iso or "UNKNOWN", "country_name": names.get(iso, "País sin identificar"), "country_source": method}

    for key in ("country_code", "country_name", "country"):
        value = row.get(key)
        if isinstance(value, str) and value.strip() and value != "UNKNOWN":
            iso = aliases.get(_normalized(value))
            if iso:
                return result(iso, "provided_" + key)

    # Un CSV aportado puede declarar enclaves que el mapa a pequeña escala omite.
    source = row.get("location_source")
    if isinstance(source, str) and source.lower().endswith(".csv") and not source.startswith("http"):
        candidate_file = Path(source)
        if not candidate_file.is_absolute():
            candidate_file = Path(root) / candidate_file
        try:
            resolved = candidate_file.resolve()
            if resolved.is_relative_to(Path(root).resolve()) and resolved.is_file():
                for candidate in _csv_countries(str(resolved), resolved.stat().st_mtime_ns):
                    if (candidate.get("name") == row.get("name") and abs(float(candidate["latitude"]) - latitude) <= 1e-5
                            and abs(float(candidate["longitude"]) - longitude) <= 1e-5):
                        for key in ("country_code", "country_name", "country"):
                            iso = aliases.get(_normalized(candidate.get(key, "")))
                            if iso:
                                return result(iso, "source_csv")
        except (OSError, ValueError, KeyError):
            pass

    name = str(row.get("name", ""))
    suffix = name.rsplit(",", 1)[-1].strip()
    if isinstance(source, str) and source.endswith("city-na.js"):
        if suffix in CANADIAN_PROVINCES:
            return result("CA", "nasa_city_country")
        if suffix in US_STATES:
            return result("US", "nasa_city_country")
    if suffix == "NETHERLANDS ANTILLES":
        if "Aruba" in name:
            return result("AW", "nasa_city_country")
        if "Curacao" in name:
            return result("CW", "nasa_city_country")
    # Un sufijo libre como 'CA' solo se interpreta con el contexto regional NASA.
    if len(suffix) > 2 or suffix in {"UK", "USA"}:
        iso = aliases.get(_normalized(suffix))
        if iso:
            return result(iso, "nasa_city_country" if str(source).startswith("https://eclipse.gsfc.nasa.gov/JSEX/") else "destination_name")
    if math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
        iso = _polygon_country(latitude, longitude, polygons)
        if iso:
            return result(iso, "approximate_polygon")
    return result(None, "unresolved")


def export_world_map(root=sources.ROOT):
    """Empaqueta el mapa de referencia para abrirlo con file:// sin API ni servidor."""
    _, _, features, _ = _country_data(str(_data_path(root)))
    if not features:
        raise ValueError("Falta la cartografía local de Natural Earth.")
    payload = {"type": "FeatureCollection", "features": features,
        "attribution": {"name": "Natural Earth", "url": "https://www.naturalearthdata.com/", "license": "Public domain", "scale": "1:110m"}}
    directory = Path(root) / "app"
    directory.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    (directory / "world-map.js").write_text("window.ECLIPSE_WORLD_MAP = " + serialized + ";\n", encoding="utf-8")
    return payload
