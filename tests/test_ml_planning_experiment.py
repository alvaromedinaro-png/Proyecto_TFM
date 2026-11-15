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


def ranked(rows):
    """Filas de ranking_frame mínimas: un evento solar, un año, varias ciudades."""
    base = {"phase": "holdout", "event_id": "E", "reference_year": 2024, "rain": 0, "utility": 0.0}
    return pd.DataFrame([{**base, "name": str(index), **row} for index, row in enumerate(rows)])


class FullRankingTests(unittest.TestCase):
    def predictions(self, family="solar"):
        rows = []
        for name, clear, rain, y_clear, y_rain in [("A", 0.8, 0.1, 1, 0), ("B", 0.2, 0.5, 0, 1)]:
            for target, probability, y in (("clear_sky_proxy", clear, y_clear), ("rain_proxy", rain, y_rain)):
                rows.append({"sample_id": f"E|{name}|2024", "phase": "holdout", "event_id": "E", "event_family": family, "name": name,
                             "reference_year": 2024, "target": target, "model": "climatology_prior", "probability": probability, "y": y})
        return pd.DataFrame(rows)

    def geometry(self):
        return pd.DataFrame({"event_id": ["E", "E"], "name": ["A", "B"], "phase_priority": [2, 2],
                             "astronomy_score": [0.5, 1.0], "duration_score": [0.2, 1.0], "altitude_score": [0.3, 0.9]})

    def test_full_score_is_the_product_formula(self):
        models = {target: "climatology_prior" for target in ml.TARGETS}
        with patch.object(ml, "MIN_CANDIDATES_FOR_RANKING", 2):
            frame = ml.ranking_frame(self.predictions(), self.geometry(), models).set_index("name")
        weights = pipeline.SCORE_WEIGHTS["solar"]
        row = self.geometry().set_index("name").loc["A"]
        self.assertAlmostEqual(frame.loc["A", "completo"], pipeline.weighted_score(weights, 0.8, 0.9, row))
        self.assertAlmostEqual(frame.loc["A", "utility"], pipeline.weighted_score(weights, 1, 1, row))
        self.assertAlmostEqual(frame.loc["B", "utility"], pipeline.weighted_score(weights, 0, 0, self.geometry().set_index("name").loc["B"]))
        self.assertAlmostEqual(frame.loc["A", "completo"], frame.loc["A", "solo_geometria"] + frame.loc["A", "solo_tiempo"])

    def test_small_events_are_left_out(self):
        models = {target: "climatology_prior" for target in ml.TARGETS}
        self.assertTrue(ml.ranking_frame(self.predictions(), self.geometry(), models).empty)

    def test_choice_respects_phase_priority_and_averages_ties(self):
        group = ranked([{"phase_priority": 1, "completo": 0.9, "clear": 1, "utility": 0.9},
                        {"phase_priority": 2, "completo": 0.4, "clear": 1, "utility": 0.6},
                        {"phase_priority": 2, "completo": 0.4, "clear": 0, "utility": 0.2}])
        outcome = ml.choice_outcome(group, "completo")
        self.assertEqual(outcome["clear"], 0.5)
        self.assertAlmostEqual(outcome["best_utility"], 0.6)

    def test_pairs_compare_only_destinations_with_the_same_phase(self):
        frame = ranked([{"phase_priority": 2, "clear": 1}, {"phase_priority": 2, "clear": 0}, {"phase_priority": 1, "clear": 0}])
        frame[ml.STRATEGIES[1:]] = 0.0
        self.assertEqual(len(ml.pair_table(frame)), 1)

    def test_pairwise_accuracy_of_random_perfect_and_wrong_strategies(self):
        rows = [{"phase_priority": 1, "clear": clear, "utility": float(clear), "solo_geometria": -clear, "solo_tiempo": clear,
                 "completo": 0.0} for clear in (1, 0, 1, 0)]
        result = ml.pairwise_choice(ranked(rows)).query("phase == 'holdout' and outcome == 'despejado'").set_index("strategy").accuracy
        self.assertEqual((result["azar"], result["solo_tiempo"], result["solo_geometria"], result["completo"]), (0.5, 1.0, 0.0, 0.5))

    def test_pairs_with_the_same_weather_do_not_count_for_utility(self):
        rows = [{"phase_priority": 1, "clear": 1, "utility": 0.9, "solo_geometria": 0.5, "solo_tiempo": 0.4, "completo": 0.9},
                {"phase_priority": 1, "clear": 1, "utility": 0.7, "solo_geometria": 0.3, "solo_tiempo": 0.4, "completo": 0.7}]
        result = ml.pairwise_choice(ranked(rows)).query("phase == 'holdout' and outcome == 'utilidad'")
        self.assertTrue(result.pairs.eq(0).all())


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
