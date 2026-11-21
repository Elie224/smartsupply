"""Entraînement et évaluation des modèles de prévision (phase 6).

    python -m smartsupply.models.train                 # entraîne, compare, choisit sur la VALIDATION
    python -m smartsupply.models.train --final-test    # + évaluation finale sur le TEST (à faire une seule fois)

Démarche (README section 8) :
  1. Baselines sans apprentissage (naïve 7 j, naïve 28 j, pondérée).
  2. Régression linéaire (Ridge) : modèle simple de référence supplémentaire.
  3. LightGBM : modèle candidat principal.
  4. Validation glissante (rolling-origin) sur la période d'entraînement : le score est-il stable ?
  5. Choix du modèle sur la VALIDATION uniquement ; le TEST est réservé à la fin.
  6. Rapport par segment : catégorie, volume du produit, promotion, ruptures récentes.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from smartsupply.db.connection import PROJECT_ROOT
from smartsupply.features.build import HORIZON_DAYS, TARGET
from smartsupply.features.split import DEFAULT_PLAN, chronological_split
from smartsupply.models.baselines import BASELINES, predict_baseline
from smartsupply.models.estimators import LGBM_PARAMS, MODELS
from smartsupply.models.metrics import scores, segment_report

MODELS_DIR = PROJECT_ROOT / "data" / "models"


# -----------------------------------------------------------------------------
# Données
# -----------------------------------------------------------------------------
def latest_dataset() -> Path:
    files = sorted((PROJECT_ROOT / "data" / "datasets").glob("features_*.parquet"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError("Aucun dataset : lance `python -m smartsupply.features.build --export`.")
    return files[-1]


def load_dataset(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.date
    return df


def fit_predict(name: str, train: pd.DataFrame, other: pd.DataFrame) -> np.ndarray:
    return MODELS[name]().fit(train).predict(other)


# -----------------------------------------------------------------------------
# Validation glissante (rolling-origin) sur la période d'entraînement
# -----------------------------------------------------------------------------
def rolling_origin_folds(train: pd.DataFrame, n_folds: int = 3, fold_days: int = 42) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Origines successives : on entraîne sur le passé, on évalue sur les 6 semaines suivantes.

    Entre fin d'entraînement et début d'évaluation : HORIZON_DAYS + 1 jours de trou,
    pour que les cibles d'entraînement ne chevauchent pas la période évaluée.
    """
    last = train["as_of_date"].max()
    folds = []
    for k in range(n_folds, 0, -1):
        eval_end = last - timedelta(days=(k - 1) * fold_days)
        eval_start = eval_end - timedelta(days=fold_days - 1)
        train_end = eval_start - timedelta(days=HORIZON_DAYS + 1)
        tr = train[train["as_of_date"] <= train_end]
        ev = train[(train["as_of_date"] >= eval_start) & (train["as_of_date"] <= eval_end)]
        folds.append((tr, ev))
    return folds


def rolling_origin_scores(train: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for i, (tr, ev) in enumerate(rolling_origin_folds(train), start=1):
        y = ev[TARGET].astype(float)
        candidates = {"naive_28j": predict_baseline("naive_28j", ev)}
        for name in ("ridge", "lightgbm"):
            candidates[name] = fit_predict(name, tr, ev)
        for name, pred in candidates.items():
            rows.append({"fold": i, "evaluation": f"{ev['as_of_date'].min()} -> {ev['as_of_date'].max()}",
                         "modele": name, **scores(y, pred)})
    return pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# Segments
# -----------------------------------------------------------------------------
def add_segments(df: pd.DataFrame, train: pd.DataFrame) -> pd.DataFrame:
    """Volume du produit calculé sur le TRAIN uniquement (pas d'information de la validation)."""
    volume = train.groupby("product_id")[TARGET].mean().astype(float)
    tiers = pd.qcut(volume, 3, labels=["lent", "moyen", "rapide"])
    out = df.copy()
    out["volume"] = out["product_id"].map(tiers).astype(str)
    out["promo_a_venir"] = np.where(out["promo_days_next_7"] > 0, "oui", "non")
    out["rupture_recente"] = np.where(out["lost_sales_rolling_7"] > 0, "oui", "non")
    return out


# -----------------------------------------------------------------------------
# Programme principal
# -----------------------------------------------------------------------------
@dataclass
class TrainingResult:
    run_id: str
    best_model: str
    validation: pd.DataFrame
    rolling: pd.DataFrame
    segments: dict[str, pd.DataFrame]
    test: pd.DataFrame | None
    model_path: Path


def run(final_test: bool = False, dataset: Path | None = None) -> TrainingResult:
    path = dataset or latest_dataset()
    df = load_dataset(path)
    parts = chronological_split(df, DEFAULT_PLAN)
    train, val, test = parts["train"], parts["validation"], parts["test"]

    # 1-3) Tous les candidats, entraînés sur TRAIN, évalués sur VALIDATION
    preds = {name: predict_baseline(name, val).to_numpy() for name in BASELINES}
    for name in ("ridge", "lightgbm"):
        preds[name] = fit_predict(name, train, val)
    y_val = val[TARGET].astype(float)
    validation = pd.DataFrame({name: scores(y_val, p) for name, p in preds.items()}).T.sort_values("wape")

    # 4) Stabilité dans le temps
    rolling = rolling_origin_scores(train)

    # 5) Choix sur la validation uniquement
    best = validation.index[0]
    best_baseline = validation.loc[list(BASELINES)].sort_values("wape").index[0]

    # 6) Segments : meilleur modèle vs meilleure baseline
    seg_df = add_segments(val, train).assign(pred_model=preds[best], pred_baseline=preds[best_baseline])
    segments = {}
    for by in ("category", "volume", "promo_a_venir", "rupture_recente"):
        m = segment_report(seg_df, TARGET, "pred_model", by).add_prefix(f"{best}_")
        b = segment_report(seg_df, TARGET, "pred_baseline", by)[["wape"]].rename(columns={"wape": f"{best_baseline}_wape"})
        segments[by] = m.join(b)

    # Modèle final : réentraîné sur TRAIN + VALIDATION (plus de données récentes),
    # avec les mêmes réglages. Le TEST n'a jamais servi à choisir quoi que ce soit.
    final_train = pd.concat([train, val], ignore_index=True)
    test_scores = None
    final_model = None if best in BASELINES else MODELS[best]().fit(final_train)
    if final_test:
        y_test = test[TARGET].astype(float)
        test_preds = {name: predict_baseline(name, test).to_numpy() for name in BASELINES}
        if final_model is not None:
            test_preds[best] = final_model.predict(test)
        test_scores = pd.DataFrame({n: scores(y_test, p) for n, p in test_preds.items()}).T.sort_values("wape")

    # Sauvegarde (MLflow viendra en phase 7)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = MODELS_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / "model.joblib"
    joblib.dump(
        {"model": final_model, "model_name": best,
         "features": getattr(final_model, "columns", None), "target": TARGET, "params": LGBM_PARAMS if best == "lightgbm" else None,
         "dataset": path.name, "trained_until": str(final_train["as_of_date"].max())},
        model_path,
    )
    report = {
        "run_id": run_id, "dataset": path.name, "best_model": best, "best_baseline": best_baseline,
        "plan": {k: [str(a), str(b)] for k, (a, b) in vars(DEFAULT_PLAN).items()},
        "validation": validation.round(4).to_dict(orient="index"),
        "rolling_origin": rolling.round(4).to_dict(orient="records"),
        "segments": {k: v.round(4).reset_index().to_dict(orient="records") for k, v in segments.items()},
        "test": None if test_scores is None else test_scores.round(4).to_dict(orient="index"),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if best == "lightgbm":
        final_model.feature_importance().to_csv(out / "feature_importance.csv", header=["importance"])

    return TrainingResult(run_id, best, validation, rolling, segments, test_scores, model_path)


def _pct(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if col.endswith("wape") or col.endswith("bias"):
            out[col] = (100 * out[col]).round(1).astype(str) + " %"
        elif col.endswith("mae"):
            out[col] = out[col].round(2)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Entraîne et évalue les modèles de prévision.")
    parser.add_argument("--final-test", action="store_true",
                        help="évalue aussi sur le jeu de TEST (à ne faire qu'une fois, à la fin)")
    args = parser.parse_args()
    result = run(final_test=args.final_test)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    print("\n=== VALIDATION : tous les candidats ===")
    print(_pct(result.validation))
    print("\n=== Validation glissante sur la période d'entraînement (WAPE) ===")
    piv = result.rolling.pivot(index="evaluation", columns="modele", values="wape")
    print((100 * piv).round(1).astype(str) + " %")
    for by, table in result.segments.items():
        print(f"\n=== Segments : {by} ===")
        print(_pct(table))
    if result.test is not None:
        print("\n=== TEST FINAL (jamais utilisé avant) ===")
        print(_pct(result.test))
    print(f"\nMeilleur modèle sur la validation : {result.best_model}")
    print(f"Modèle sauvegardé : {result.model_path}")


if __name__ == "__main__":
    main()
