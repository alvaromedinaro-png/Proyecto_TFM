"""Ejemplo pequeño reconstruible sin red: eclipse del 2 de agosto de 2027 con cinco años de histórico.

Copia en una carpeta temporal solo las fuentes Bronze versionadas, rehace Silver y Gold con el
código actual y compara los hashes con data/example/2027-08-02/pipeline_quality_report.json.
"""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import tempfile

try:
    from . import eclipse_sources as sources
    from .eclipse_catalogs import catalog_between, select_event
    from .eclipse_pipeline import SNAPSHOT_FILES, acquire_complete_event
except ImportError:
    import eclipse_sources as sources
    from eclipse_catalogs import catalog_between, select_event
    from eclipse_pipeline import SNAPSHOT_FILES, acquire_complete_event

EVENT_DATE = "2027-08-02"
YEARS = (2022, 2026)
EXAMPLE = Path("data/example") / EVENT_DATE
# El estado del pronóstico registra el día de la consulta; no se compara.
DATE_DEPENDENT = {"forecast_status.json"}


def example_sources(root=sources.ROOT):
    """Originales NASA, cartografía y las respuestas climáticas de los años del ejemplo."""
    event = Path("data/bronze/events") / EVENT_DATE
    manifest = json.loads((root / event / "climate_manifest.json").read_text(encoding="utf-8"))
    climate = [Path(item["file"].replace("\\", "/")) for item in manifest["inputs"]
               if YEARS[0] <= int(Path(item["file"].replace("\\", "/")).name.split("_")[1]) <= YEARS[1]]
    files = [path.relative_to(root) for folder in ("data/bronze/nasa", "data/bronze/geography")
             for path in sorted((root / folder).iterdir()) if path.is_file()]
    for raw in [event / "besselian.html", event / "nasa_path.html", *climate]:
        files.extend([raw, raw.with_name(raw.name + ".metadata.json")])
    return files


def rebuild(target, root=sources.ROOT):
    """Reconstruye el ejemplo en target, que solo contiene copias de las fuentes incluidas."""
    for relative in example_sources(root):
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target / relative)
    following = (date.fromisoformat(EVENT_DATE) + timedelta(days=1)).isoformat()
    event = select_event(catalog_between(EVENT_DATE, following, offline=True, root=target), EVENT_DATE)
    _, report = acquire_complete_event(event, root=target, start=YEARS[0], end=YEARS[1], offline=True)
    return target / "data/gold/events" / EVENT_DATE, report


def compare(report, expected):
    differences = [filename for filename in SNAPSHOT_FILES if filename not in DATE_DEPENDENT
                   and report["output_sha256"][filename] != expected["output_sha256"].get(filename)]
    if report["code_sha256"] != expected.get("code_sha256"):
        differences.append("code_sha256")
    return differences


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Guardar el resultado como referencia del ejemplo.")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as folder:
        gold, report = rebuild(Path(folder))
        destination = sources.ROOT / EXAMPLE
        if args.write:
            destination.mkdir(parents=True, exist_ok=True)
            for filename in [*SNAPSHOT_FILES, "pipeline_quality_report.json"]:
                shutil.copyfile(gold / filename, destination / filename)
            print(f"Ejemplo guardado en {EXAMPLE.as_posix()}")
            return 0
    expected = json.loads((destination / "pipeline_quality_report.json").read_text(encoding="utf-8"))
    differences = compare(report, expected)
    for filename in SNAPSHOT_FILES:
        state = ("no se compara (depende del día)" if filename in DATE_DEPENDENT
                 else "distinto" if filename in differences else "idéntico")
        print(f"{filename}: {state}")
    if "code_sha256" in differences:
        print("El ejemplo guardado se generó con otra versión del código.")
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
