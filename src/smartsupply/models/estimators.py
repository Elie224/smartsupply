"""Modèles de prévision, avec une interface commune : fit(df) / predict(df).

LightGBMRatioModel — le modèle principal.
  Idée : les produits ont des volumes très différents (x50 entre le plus lent et le
  plus rapide), mais les effets saison / promotion sont RELATIFS (+30 % en promo...).
  On fait donc apprendre au modèle un RATIO :
        cible / (7 x demande moyenne des 91 derniers jours)
  puis on remultiplie. La moyenne sur 91 jours estime le niveau du produit avec
  beaucoup moins de bruit que celle sur 28 jours (testé : -1 à -2 points de WAPE). Chaque ligne est pondérée par ce volume de référence, pour que
  l'optimisation reste alignée sur l'erreur en UNITÉS (et donc sur le WAPE).
"""
from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from smartsupply.features.build import FEATURES, TARGET

CATEGORICAL = ["category"]
BOOLEAN = ["promotion_active", "is_weekend"]
NUMERIC = [f for f in FEATURES if f not in BOOLEAN] + BOOLEAN
# Variables utilisées par LightGBM. Les variables "an dernier" et les moyennes longues
# restent dans le dataset, mais ne l'améliorent pas en validation glissante : on ne les garde pas.
EXPERIMENTAL = {"demand_rolling_mean_91", "demand_rolling_mean_364", "demand_next7_last_year",
                "category_season_index", "category_trend_7_28"}
LGBM_NUMERIC = [f for f in NUMERIC if f not in EXPERIMENTAL]
DERIVED = ["mean7_over_mean28", "cv_28", "stock_cover_days", "incoming_cover_days"]

LGBM_PARAMS = {
    "objective": "l1",          # optimise l'erreur absolue, cohérent avec MAE / WAPE
    "learning_rate": 0.03,
    "n_estimators": 1000,
    "num_leaves": 31,
    "min_child_samples": 40,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": 42,
    "verbose": -1,
}


def reference_volume(df: pd.DataFrame) -> pd.Series:
    """Demande attendue « si rien ne change » : 7 x moyenne des 91 derniers jours
    (moyenne 28 jours si l'historique est trop court)."""
    level = df["demand_rolling_mean_91"].fillna(df["demand_rolling_mean_28"])
    return (7 * level).clip(lower=1.0)


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Variables relatives, toutes calculées à partir de colonnes connues à D."""
    out = df.copy()
    m28 = out["demand_rolling_mean_28"].clip(lower=0.1)
    out["mean7_over_mean28"] = out["demand_rolling_mean_7"] / m28       # accélération récente
    out["cv_28"] = out["demand_rolling_std_28"] / m28                   # irrégularité
    out["stock_cover_days"] = out["stock_as_of"] / m28                  # jours de stock
    out["incoming_cover_days"] = out["incoming_qty_known_as_of"] / m28  # jours de réassort attendus
    for col in BOOLEAN:
        out[col] = out[col].astype(int)
    out["category"] = out["category"].astype("category")
    return out


class LightGBMRatioModel:
    name = "lightgbm"

    def __init__(self, params: dict | None = None):
        self.params = {**LGBM_PARAMS, **(params or {})}
        self.columns = LGBM_NUMERIC + DERIVED + CATEGORICAL
        self.model = lgb.LGBMRegressor(**self.params)

    def fit(self, df: pd.DataFrame) -> "LightGBMRatioModel":
        data = add_derived(df)
        ref = reference_volume(data)
        self.model.fit(data[self.columns], data[TARGET].astype(float) / ref, sample_weight=ref)
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        data = add_derived(df)
        pred = self.model.predict(data[self.columns]) * reference_volume(data).to_numpy()
        return np.clip(pred, 0, None)

    def feature_importance(self) -> pd.Series:
        return pd.Series(self.model.feature_importances_, index=self.columns).sort_values(ascending=False)


class RidgeModel:
    """Régression linéaire régularisée : référence « modèle simple » (README étape 2)."""
    name = "ridge"

    def __init__(self, alpha: float = 1.0):
        self.columns = NUMERIC + CATEGORICAL
        pre = ColumnTransformer(
            [("num", Pipeline([("impute", SimpleImputer(strategy="median")),
                               ("scale", StandardScaler())]), NUMERIC),
             ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL)]
        )
        self.model = Pipeline([("prep", pre), ("model", Ridge(alpha=alpha))])

    def fit(self, df: pd.DataFrame) -> "RidgeModel":
        data = add_derived(df)
        self.model.fit(data[self.columns], data[TARGET].astype(float))
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return np.clip(self.model.predict(add_derived(df)[self.columns]), 0, None)


MODELS = {"ridge": RidgeModel, "lightgbm": LightGBMRatioModel}
