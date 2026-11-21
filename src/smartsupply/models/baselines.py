"""Prévisions de référence, SANS apprentissage (README section 8, étape 1).

Toutes n'utilisent que des colonnes connues à la date D (features du dataset).
Un modèle n'a d'intérêt que s'il fait mieux que la meilleure d'entre elles.
"""
from __future__ import annotations

import pandas as pd

BASELINES = {
    # demande moyenne des 7 derniers jours x 7
    "naive_7j": lambda df: 7 * df["demand_rolling_mean_7"],
    # demande moyenne des 28 derniers jours x 7 (plus stable)
    "naive_28j": lambda df: 7 * df["demand_rolling_mean_28"],
    # moyenne pondérée des semaines récentes : 50 % dernière semaine, 50 % 4 dernières
    "ponderee_7j_28j": lambda df: 7 * (0.5 * df["demand_rolling_mean_7"] + 0.5 * df["demand_rolling_mean_28"]),
}


def predict_baseline(name: str, df: pd.DataFrame) -> pd.Series:
    return BASELINES[name](df).astype(float)
