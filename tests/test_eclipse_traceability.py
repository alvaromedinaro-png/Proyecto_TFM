"""Hashes de las salidas, fin de línea estable y ejemplo reconstruible sin red."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src import eclipse_example as example
from src import eclipse_pipeline as pipeline
from src import eclipse_sources as sources


class LineEndingTests(unittest.TestCase):
    def test_csv_and_json_are_written_with_lf(self):
        with tempfile.TemporaryDirectory() as temp:
            csv_path, json_path = Path(temp) / "a.csv", Path(temp) / "a.json"
            sources.save_csv(pd.DataFrame({"x": [1, 2]}), csv_path)
            sources.save_json(json_path, {"x": [1, 2]})
            for path in [csv_path, json_path]:
                self.assertNotIn(b"\r\n", path.read_bytes())

    def test_code_hash_ignores_checkout_line_endings(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "eclipse_a.py").write_bytes(b"x = 1\ny = 2\n")
            lf = sources.code_sha256(folder)
            (folder / "eclipse_a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
            self.assertEqual(sources.code_sha256(folder), lf)


class VerifyOutputsTests(unittest.TestCase):
    def write_event(self, root):
        gold = root / "data/gold/events/2027-08-02"
        gold.mkdir(parents=True)
        (gold / "location_ranking.csv").write_bytes(b"name\nA\n")
        sources.save_json(gold / "pipeline_quality_report.json", {"code_sha256": sources.code_sha256(),
            "output_sha256": {"location_ranking.csv": hashlib.sha256(b"name\nA\n").hexdigest()}})
        return gold

    def test_matching_outputs_pass_and_altered_files_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gold = self.write_event(root)
            self.assertEqual(pipeline.verify_outputs(root)[0]["problems"], [])
            (gold / "location_ranking.csv").write_bytes(b"name\r\nA\r\n")
            self.assertIn("location_ranking.csv no coincide con el hash del informe",
                          pipeline.verify_outputs(root)[0]["problems"])

    def test_other_code_version_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gold = self.write_event(root)
            report = json.loads((gold / "pipeline_quality_report.json").read_text(encoding="utf-8"))
            sources.save_json(gold / "pipeline_quality_report.json", {**report, "code_sha256": {"eclipse_pipeline.py": "0"}})
            self.assertIn("el informe se generó con otra versión del código", pipeline.verify_outputs(root)[0]["problems"])


@unittest.skipUnless((sources.ROOT / example.EXAMPLE / "pipeline_quality_report.json").is_file(), "Ejemplo no generado.")
class ReproducibleExampleTests(unittest.TestCase):
    def test_example_rebuilds_from_included_sources_without_network(self):
        expected = json.loads((sources.ROOT / example.EXAMPLE / "pipeline_quality_report.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            gold, report = example.rebuild(Path(temp))
            self.assertEqual(example.compare(report, expected), [])
            for filename, digest in expected["output_sha256"].items():
                if filename not in example.DATE_DEPENDENT:
                    self.assertEqual(sources.file_sha256(sources.ROOT / example.EXAMPLE / filename), digest, filename)
        self.assertEqual(report["climate"]["years"], list(range(example.YEARS[0], example.YEARS[1] + 1)))


if __name__ == "__main__":
    unittest.main()
