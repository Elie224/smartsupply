"""Métriques de prévision (README section 8, étape 3).

  MAE   : erreur moyenne absolue, en unités.
  WAPE  : somme des erreurs absolues / somme du réel. Erreur relative globale,
          robuste aux produits à faible volume (contrairement au MAPE).
  Biais : somme(prévu - réel) / somme(réel). Négatif = on SOUS-estime
          (le plus dangereux pour les ruptures).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def mae(y_true, y_pred) -> float:
    return float(np.mean(np.abs(np.asarray(y_pred, float) - np.asarray(y_true, float))))


def wape(y_true, y_pred) -> float:
    y_true, y_pred = np.asarray(y_true, float), np.asarray(y_pred, float)
    total = y_true.sum()
    return float(np.abs(y_pred - y_true).sum() / total) if total else float("nan")


def bias(y_true, y_pred) -> float:
    y_true, y_pred = np.asarray(y_true, float), np.asarray(y_pred, float)
    total = y_true.sum()
    return float((y_pred - y_true).sum() / total) if total else float("nan")


def scores(y_true, y_pred) -> dict[str, float]:
    return {"mae": mae(y_true, y_pred), "wape": wape(y_true, y_pred), "bias": bias(y_true, y_pred)}


def segment_report(df: pd.DataFrame, y_true: str, y_pred: str, by: str) -> pd.DataFrame:
    """Scores par segment (catégorie, volume, promotion...)."""
    rows = []
    for segment, part in df.groupby(by, observed=True):
        rows.append({by: segment, "n": len(part), **scores(part[y_true], part[y_pred])})
    return pd.DataFrame(rows).set_index(by)
