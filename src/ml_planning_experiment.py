"""Entrega 3: ¿mejora un modelo a la climatología histórica para planificar un eclipse?

Parte de los planning_ml_dataset.csv del Gold y estima, para cada evento × ubicación, la
probabilidad de cielo despejado y de lluvia a la hora del máximo. Valida por años completos:
cada año se predice entrenando solo con años anteriores. 2017–2022 sirven para elegir modelo y
2023–2026 se reservan para confirmar la elección.

Uso:  python src/ml_planning_experiment.py          (escribe data/gold/ml/)
      python src/ml_planning_experiment.py --check  (recalcula y compara con lo guardado)

El nombre no empieza por eclipse_ a propósito: code_sha256 identifica el código que genera el
Gold y este script solo lo lee.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

try:
    from . import eclipse_sources as sources
    from .eclipse_pipeline import MIN_PRIOR_YEARS_FOR_EVALUATION
except ImportError:
    import eclipse_sources as sources
    from eclipse_pipeline import MIN_PRIOR_YEARS_FOR_EVALUATION

OUTPUT = Path("data/gold/ml")
SEED = 20260
SELECTION_YEARS = list(range(2017, 2023))
HOLDOUT_YEARS = list(range(2023, 2027))
TARGETS = {"clear_sky_proxy": "baseline_clear_probability", "rain_proxy": "baseline_rain_probability"}
# prior_years y la celda ERA5 están en el Gold, pero se dejan fuera: prior_years equivale al año
# (en la consulta vale 30, fuera del rango de entrenamiento) y la celda duplica las coordenadas.
CLIMATE = ["prior_clear_frequency", "prior_rain_frequency", "prior_window_clear_frequency",
           "prior_mean_cloud_cover_pct", "prior_mean_cloud_cover_low_pct"]
GEOMETRY = ["latitude", "longitude_sin", "longitude_cos", "elevation_m", "day_sin", "day_cos",
            "hour_sin", "hour_cos", "body_altitude_deg"]
FULL_FEATURES = ["prior_logit", *CLIMATE, *GEOMETRY]
# De más simple a más complejo. global_prevalence es solo una referencia y no se puede elegir.
MODELS = ["global_prevalence", "climatology_prior", "calibrated_climatology", "logistic_full", "gradient_boosting"]
ELIGIBLE = MODELS[1:]
# Regla fijada antes de ver resultados: un modelo sustituye a la climatología si mejora su Brier
# al menos un 1 % y el intervalo por años excluye el empate; entre los que cumplen, el más
# simple a menos de 0,5 puntos de la mejor mejora.
MIN_SKILL = 0.01
PARSIMONY_MARGIN = 0.005
BOOTSTRAP_RESAMPLES = 5000
MIN_CANDIDATES_FOR_RANKING = 6
BANDS = {"baja": (0.0, 0.25), "media": (0.25, 0.5), "alta": (0.5, 1.0001)}
OUTPUT_FILES = ("metrics_summary.csv", "metrics_by_year.csv", "ranking_value.csv", "calibration.csv",
                "error_segments.csv", "worst_locations.csv", "out_of_time_predictions.csv", "query_predictions.csv")


def load_dataset(root=sources.ROOT):
    """Une los Gold por evento y separa filas evaluables (al menos 10 años previos) y consultas."""
    paths = sorted((root / "data/gold/events").glob("*/planning_ml_dataset.csv"))
    if not paths:
        raise FileNotFoundError("No hay planning_ml_dataset.csv; ejecutar antes el pipeline.")
    data = add_features(pd.concat([pd.read_csv(path) for path in paths], ignore_index=True))
    if data.sample_id.duplicated().any():
        raise ValueError("sample_id duplicado en el Gold.")
    history = data.loc[data.row_role.eq("history") & data.prior_years.ge(MIN_PRIOR_YEARS_FOR_EVALUATION)].copy()
    query = data.loc[data.row_role.eq("query")].copy()
    inputs = {path.relative_to(root).as_posix(): sources.file_sha256(path) for path in paths}
    return history.reset_index(drop=True), query.reset_index(drop=True), inputs


def add_features(data):
    """Codifica como ciclos las magnitudes circulares (día, hora, longitud)."""
    day, hour, longitude = (2 * np.pi * data.day_of_year / 365.25, 2 * np.pi * data.local_solar_hour / 24,
                            np.radians(data.longitude))
    return data.assign(day_sin=np.sin(day), day_cos=np.cos(day), hour_sin=np.sin(hour), hour_cos=np.cos(hour),
                       longitude_sin=np.sin(longitude), longitude_cos=np.cos(longitude))


def with_prior_logit(data, target):
    """Logit de la frecuencia previa con suavizado de Jeffreys, para que 0 y 1 no sean infinitos."""
    positives = data[TARGETS[target]] * data.prior_years
    smoothed = (positives + 0.5) / (data.prior_years + 1)
    return data.assign(prior_logit=np.log(smoothed / (1 - smoothed)))


def fit_predict(model, train, test, target):
    """Entrena con train y devuelve la probabilidad de la clase positiva en test."""
    if model == "global_prevalence":
        return np.full(len(test), train[target].mean())
    if model == "climatology_prior":
        return test[TARGETS[target]].to_numpy()
    train, test = with_prior_logit(train, target), with_prior_logit(test, target)
    if model == "calibrated_climatology":
        columns, estimator = ["prior_logit"], make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
    elif model == "logistic_full":
        columns, estimator = FULL_FEATURES, make_pipeline(StandardScaler(), LogisticRegression(max_iter=5000))
    elif model == "gradient_boosting":
        columns, estimator = FULL_FEATURES, HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150,
            min_samples_leaf=40, l2_regularization=1.0, early_stopping=False, random_state=SEED)
    else:
        raise ValueError(f"Modelo desconocido: {model}")
    estimator.fit(train[columns], train[target].astype(int))
    return estimator.predict_proba(test[columns])[:, 1]


def walk_forward(history, years, phase):
    """Predicciones fuera de entrenamiento: cada año usa solo años estrictamente anteriores."""
    rows = []
    for year in years:
        train, test = history.loc[history.reference_year.lt(year)], history.loc[history.reference_year.eq(year)]
        if train.empty or test.empty:
            raise ValueError(f"Sin datos para validar {year}.")
        for target in TARGETS:
            base = test[["sample_id", "event_id", "event_family", "name", "candidate_type", "latitude", "reference_year"]]
            base = base.assign(phase=phase, target=target, y=test[target].astype(int), climatology=test[TARGETS[target]])
            rows.extend(base.assign(model=model, probability=fit_predict(model, train, test, target)) for model in MODELS)
    return pd.concat(rows, ignore_index=True)


def calibration_error(y, probability, bins=10):
    """Diferencia media, ponderada, entre lo previsto y lo observado por tramos de probabilidad."""
    frame = pd.DataFrame({"y": y, "p": probability})
    groups = frame.groupby(np.minimum((frame.p * bins).astype(int), bins - 1))
    return float((groups.p.mean() - groups.y.mean()).abs().mul(groups.size()).sum() / len(frame))


def score(y, probability):
    return {"rows": len(y), "positive_rate": float(np.mean(y)), "brier": brier_score_loss(y, probability),
            "log_loss": log_loss(y, np.clip(probability, 1e-3, 1 - 1e-3), labels=[0, 1]),
            "roc_auc": roc_auc_score(y, probability), "calibration_error": calibration_error(y, probability)}


def bootstrap_interval(differences, seed=SEED):
    """Intervalo al 95 % remuestreando años: el año es la unidad independiente, no la fila."""
    values = np.asarray(differences, dtype=float)
    picks = np.random.default_rng(seed).integers(0, len(values), (BOOTSTRAP_RESAMPLES, len(values)))
    return tuple(float(value) for value in np.percentile(values[picks].mean(axis=1), [2.5, 97.5]))


def summarize(predictions):
    """Métricas por fase, objetivo y modelo, y diferencia pareada frente a la climatología."""
    by_year = (predictions.groupby(["phase", "target", "model", "reference_year"])[["y", "probability"]]
               .apply(lambda group: pd.Series(score(group.y, group.probability))).reset_index())
    rows = []
    for (phase, target, model), group in predictions.groupby(["phase", "target", "model"]):
        metrics = score(group.y, group.probability)
        reference = by_year.query("phase == @phase and target == @target and model == 'climatology_prior'").set_index("reference_year").brier
        yearly = by_year.query("phase == @phase and target == @target and model == @model").set_index("reference_year").brier
        difference = (yearly - reference).sort_index()
        baseline = predictions.query("phase == @phase and target == @target and model == 'climatology_prior'")
        base_brier = brier_score_loss(baseline.y, baseline.probability)
        low, high = bootstrap_interval(difference)
        rows.append({"phase": phase, "target": target, "model": model, **metrics,
            "skill_vs_climatology": 1 - metrics["brier"] / base_brier, "brier_diff_ci_low": low, "brier_diff_ci_high": high,
            "years_better_than_climatology": int(difference.lt(0).sum()), "years": len(difference)})
    return pd.DataFrame(rows), by_year


def select_model(summary, target):
    """Aplica la regla de selección sobre los años de selección; la reserva no interviene."""
    rows = summary.query("phase == 'selection' and target == @target").set_index("model")
    candidates = [model for model in ELIGIBLE[1:]
                  if rows.loc[model, "skill_vs_climatology"] >= MIN_SKILL and rows.loc[model, "brier_diff_ci_high"] < 0]
    if not candidates:
        return "climatology_prior"
    best = max(rows.loc[model, "skill_vs_climatology"] for model in candidates)
    return next(model for model in candidates if rows.loc[model, "skill_vs_climatology"] >= best - PARSIMONY_MARGIN)


def top_choice_outcome(group):
    """Resultado de elegir el destino con mayor probabilidad; los empates se promedian."""
    return group.loc[group.probability.eq(group.probability.max()), "y"].mean()


def ranking_value(predictions):
    """Decisión del producto: ¿estaba despejado el destino que el modelo habría puesto primero?"""
    clear = predictions.loc[predictions.target.eq("clear_sky_proxy")]
    keys = ["phase", "model", "event_id", "reference_year"]
    sizes = clear.groupby(keys).sample_id.transform("size")
    clear = clear.loc[sizes.ge(MIN_CANDIDATES_FOR_RANKING)]
    groups = clear.groupby(keys)
    table = pd.DataFrame({"top_choice_clear": groups[["probability", "y"]].apply(top_choice_outcome), "random_choice_clear": groups.y.mean(),
                          "any_candidate_clear": groups.y.max()}).reset_index()
    return (table.groupby(["phase", "model"])[["top_choice_clear", "random_choice_clear", "any_candidate_clear"]].mean()
            .join(table.groupby(["phase", "model"]).size().rename("event_years")).reset_index())


def calibration_table(predictions):
    """Previsto frente a observado por tramos de una décima en la reserva."""
    holdout = predictions.loc[predictions.phase.eq("holdout")].copy()
    holdout["bin"] = np.minimum((holdout.probability * 10).astype(int), 9) / 10
    return (holdout.groupby(["target", "model", "bin"])
            .agg(rows=("y", "size"), mean_predicted=("probability", "mean"), observed_rate=("y", "mean")).reset_index())


def error_segments(predictions):
    """Dónde falla cada modelo en la reserva: familia, tipo, latitud, climatología y año."""
    holdout = predictions.loc[predictions.phase.eq("holdout")].copy()
    holdout["error"] = (holdout.probability - holdout.y) ** 2
    holdout["latitude_band"] = pd.cut(holdout.latitude.abs(), [-0.1, 23.5, 50, 90.1], labels=["tropical", "media", "alta"]).astype(str)
    holdout["climatology_band"] = pd.cut(holdout.climatology, [-0.001, 0.1, 0.3, 0.6, 1.0], labels=["0-10 %", "10-30 %", "30-60 %", "60-100 %"]).astype(str)
    holdout["all_rows"] = "todas"
    # Predicciones muy seguras: lo observado debería acercarse a lo previsto.
    holdout["confidence"] = np.select([holdout.probability.ge(0.7), holdout.probability.le(0.1)], ["p>=0.7", "p<=0.1"], "resto")
    rows = []
    for segment in ["all_rows", "event_family", "candidate_type", "latitude_band", "climatology_band", "reference_year", "confidence"]:
        rows.append(holdout.groupby(["target", "model", segment]).agg(rows=("y", "size"), positive_rate=("y", "mean"),
                    mean_predicted=("probability", "mean"), brier=("error", "mean")).reset_index()
                    .rename(columns={segment: "value"}).assign(segment=segment, value=lambda frame: frame.value.astype(str)))
    return pd.concat(rows, ignore_index=True)[["target", "model", "segment", "value", "rows", "positive_rate", "mean_predicted", "brier"]]


def worst_locations(predictions, models):
    """Ubicaciones con mayor error en la reserva, con al menos un evento completo de años."""
    holdout = predictions.loc[predictions.phase.eq("holdout")].copy()
    holdout["error"] = (holdout.probability - holdout.y) ** 2
    rows = []
    for target, model in models.items():
        group = holdout.query("target == @target and model == @model")
        table = group.groupby("name").agg(rows=("y", "size"), positive_rate=("y", "mean"), mean_predicted=("probability", "mean"),
                                          brier=("error", "mean")).reset_index()
        rows.append(table.loc[table.rows.ge(len(HOLDOUT_YEARS))].nlargest(5, "brier").assign(target=target, model=model))
    return pd.concat(rows, ignore_index=True)[["target", "model", "name", "rows", "positive_rate", "mean_predicted", "brier"]]


def band(probability):
    return next(name for name, (low, high) in BANDS.items() if low <= probability < high)


def band_reliability(predictions, models):
    """Frecuencia observada en la reserva de cada categoría que vería el usuario."""
    holdout = predictions.loc[predictions.phase.eq("holdout")]
    result = {}
    for target, model in models.items():
        group = holdout.query("target == @target and model == @model")
        labels = group.probability.map(band)
        result[target] = {name: {"rows": int(labels.eq(name).sum()), "mean_predicted": group.probability[labels.eq(name)].mean(),
                                 "observed_rate": group.y[labels.eq(name)].mean()} for name in BANDS if labels.eq(name).any()}
    return result


def query_predictions(history, query, models):
    """Salida para el frontal: la solución elegida, entrenada con todo el histórico evaluable."""
    output = query[["event_id", "event_date", "event_family", "name", "country_code", "candidate_type", "reference_year", "prior_years",
                    "baseline_clear_probability", "baseline_rain_probability"]].copy()
    for target, column in [("clear_sky_proxy", "clear_sky"), ("rain_proxy", "rain")]:
        output[f"{column}_probability"] = np.clip(fit_predict(models[target], history, query, target), 0, 1)
        output[f"{column}_band"] = output[f"{column}_probability"].map(band)
        output[f"{column}_model"] = models[target]
    return output


def run(root, out):
    """Ejecuta el experimento completo y escribe resultados deterministas en out."""
    history, query, inputs = load_dataset(root)
    if history[[*FULL_FEATURES[1:], *TARGETS.values()]].isna().any().any():
        raise ValueError("Hay predictores nulos en las filas evaluables.")
    predictions = pd.concat([walk_forward(history, SELECTION_YEARS, "selection"),
                             walk_forward(history, HOLDOUT_YEARS, "holdout")], ignore_index=True)
    summary, by_year = summarize(predictions)
    models = {target: select_model(summary, target) for target in TARGETS}
    wide = predictions.pivot(index=["sample_id", "target", "phase", "reference_year", "y"], columns="model", values="probability").reset_index()
    wide.columns.name = None
    tables = {"metrics_summary.csv": summary, "metrics_by_year.csv": by_year, "ranking_value.csv": ranking_value(predictions),
              "calibration.csv": calibration_table(predictions), "error_segments.csv": error_segments(predictions),
              "worst_locations.csv": worst_locations(predictions, models), "out_of_time_predictions.csv": wide,
              "query_predictions.csv": query_predictions(history, query, models)}
    for filename, table in tables.items():
        sources.save_csv(table.round(5), out / filename)
    sources.save_json(out / "experiment_report.json", {
        "gold_inputs_sha256": inputs, "script_sha256": hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        "scikit_learn_version": sklearn.__version__, "seed": SEED, "evaluable_rows": len(history), "query_rows": len(query),
        "selection_years": SELECTION_YEARS, "holdout_years": HOLDOUT_YEARS, "models": MODELS,
        "rule": {"min_skill": MIN_SKILL, "parsimony_margin": PARSIMONY_MARGIN, "bootstrap_resamples": BOOTSTRAP_RESAMPLES},
        "selected_models": models, "band_reliability": band_reliability(predictions, models), "output_files": list(OUTPUT_FILES)})
    return models


def differences(saved, fresh):
    """Compara dos carpetas de resultados con tolerancia para diferencias numéricas entre equipos."""
    problems = []
    for filename in OUTPUT_FILES:
        try:
            pd.testing.assert_frame_equal(pd.read_csv(saved / filename), pd.read_csv(fresh / filename), check_exact=False, atol=1e-3, rtol=1e-3)
        except (AssertionError, FileNotFoundError) as error:
            problems.append(f"{filename}: {str(error).splitlines()[0]}")
    old, new = [json.loads((folder / "experiment_report.json").read_text(encoding="utf-8")) for folder in (saved, fresh)]
    problems.extend(f"experiment_report.json: {key}" for key in ("gold_inputs_sha256", "script_sha256", "selected_models") if old.get(key) != new.get(key))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experimento de ML de la entrega 3.")
    parser.add_argument("--check", action="store_true", help="recalcula en una carpeta temporal y compara con data/gold/ml")
    args = parser.parse_args(argv)
    if args.check:
        with tempfile.TemporaryDirectory() as temp:
            run(sources.ROOT, Path(temp))
            problems = differences(sources.ROOT / OUTPUT, Path(temp))
        print("\n".join(problems) if problems else "Resultados reproducidos: coinciden con data/gold/ml.")
        return 1 if problems else 0
    models = run(sources.ROOT, sources.ROOT / OUTPUT)
    print(f"Modelos elegidos: {models}. Resultados en {OUTPUT.as_posix()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
