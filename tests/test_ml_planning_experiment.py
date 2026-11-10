"""Garantías del experimento de ML: sin fugas temporales y regla de selección estable."""
import json
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import eclipse_pipeline as pipeline
from src import eclipse_sources as sources
from src import ml_planning_experiment as ml


def history_frame(years=(2015, 2016, 2017, 2018)):
    rows = [{"sample_id": f"E|{name}|{year}", "event_id": "E", "event_family": "solar", "name": name,
             "candidate_type": "ciudad NASA", "latitude": 40.0, "reference_year": year, "clear_sky_proxy": index % 2,
             "rain_proxy": (index + 1) % 2, "baseline_clear_probability": 0.4, "baseline_rain_probability": 0.2, "prior_years": 10}
            for year in years for index, name in enumerate("ABCD")]
    return pd.DataFrame(rows)


class TemporalValidationTests(unittest.TestCase):
    def test_each_year_is_predicted_only_from_earlier_years(self):
        seen = []

        def fake_fit(model, train, test, target):
            seen.append((train.reference_year.max(), set(test.reference_year)))
            return np.full(len(test), 0.5)

        with patch.object(ml, "fit_predict", fake_fit):
            result = ml.walk_forward(history_frame(), [2017, 2018], "selection")
        for latest_train, test_years in seen:
            self.assertLess(latest_train, min(test_years))
        self.assertEqual(set(result.reference_year), {2017, 2018})

    def test_selection_and_holdout_years_do_not_overlap(self):
        self.assertFalse(set(ml.SELECTION_YEARS) & set(ml.HOLDOUT_YEARS))
        self.assertLess(max(ml.SELECTION_YEARS), min(ml.HOLDOUT_YEARS))


class PredictorTests(unittest.TestCase):
    def test_features_exclude_labels_and_same_hour_observations(self):
        forbidden = {"reference_year", "prior_years", *pipeline.ML_COLUMNS["labels"], "cloud_cover", "cloud_cover_low", "rain", "precipitation"}
        self.assertFalse(forbidden & set(ml.FULL_FEATURES))

    def test_prior_columns_are_gold_predictors(self):
        self.assertLessEqual(set(ml.CLIMATE), set(pipeline.ML_COLUMNS["predictors"]))

    def test_prior_logit_is_finite_at_zero_and_one(self):
        data = pd.DataFrame({"baseline_clear_probability": [0.0, 1.0], "prior_years": [10, 30]})
        self.assertTrue(np.isfinite(ml.with_prior_logit(data, "clear_sky_proxy").prior_logit).all())

    def test_climatology_returns_gold_baseline_untouched(self):
        frame = history_frame()
        result = ml.fit_predict("climatology_prior", frame, frame, "clear_sky_proxy")
        self.assertTrue((result == frame.baseline_clear_probability.to_numpy()).all())


class SelectionRuleTests(unittest.TestCase):
    def summary(self, calibrated, logistic):
        rows = [("climatology_prior", 0.0, 0.0), ("calibrated_climatology", *calibrated), ("logistic_full", *logistic), ("gradient_boosting", -0.01, 0.001)]
        return pd.DataFrame([{"phase": "selection", "target": "clear_sky_proxy", "model": model,
                              "skill_vs_climatology": skill, "brier_diff_ci_high": high} for model, skill, high in rows])

    def test_keeps_climatology_when_interval_includes_no_improvement(self):
        self.assertEqual(ml.select_model(self.summary((0.05, 0.001), (0.05, 0.001)), "clear_sky_proxy"), "climatology_prior")

    def test_keeps_climatology_when_improvement_is_too_small(self):
        self.assertEqual(ml.select_model(self.summary((0.005, -0.001), (0.009, -0.001)), "clear_sky_proxy"), "climatology_prior")

    def test_prefers_simplest_model_within_margin(self):
        self.assertEqual(ml.select_model(self.summary((0.020, -0.001), (0.022, -0.001)), "clear_sky_proxy"), "calibrated_climatology")

    def test_complex_model_needs_a_clear_advantage(self):
        self.assertEqual(ml.select_model(self.summary((0.020, -0.001), (0.040, -0.001)), "clear_sky_proxy"), "logistic_full")


class ProductMetricTests(unittest.TestCase):
    def test_top_choice_averages_ties(self):
        group = pd.DataFrame({"probability": [0.8, 0.8, 0.1], "y": [1, 0, 1]})
        self.assertEqual(ml.top_choice_outcome(group), 0.5)

    def test_bands_cover_the_whole_probability_range(self):
        self.assertEqual([ml.band(value) for value in (0.0, 0.2499, 0.25, 0.5, 1.0)], ["baja", "baja", "media", "alta", "alta"])

    def test_bootstrap_interval_contains_the_mean_of_constant_differences(self):
        self.assertEqual(ml.bootstrap_interval([0.01] * 6), (0.01, 0.01))


@unittest.skipUnless((sources.ROOT / ml.OUTPUT / "experiment_report.json").is_file(), "Experimento no generado.")
class SavedResultsTests(unittest.TestCase):
    def test_reported_selection_follows_from_saved_metrics(self):
        report = json.loads((sources.ROOT / ml.OUTPUT / "experiment_report.json").read_text(encoding="utf-8"))
        summary = pd.read_csv(sources.ROOT / ml.OUTPUT / "metrics_summary.csv")
        self.assertEqual({target: ml.select_model(summary, target) for target in ml.TARGETS}, report["selected_models"])

    def test_saved_results_describe_the_delivered_gold(self):
        report = json.loads((sources.ROOT / ml.OUTPUT / "experiment_report.json").read_text(encoding="utf-8"))
        for relative, digest in report["gold_inputs_sha256"].items():
            self.assertEqual(sources.file_sha256(sources.ROOT / relative), digest, relative)


if __name__ == "__main__":
    unittest.main()
