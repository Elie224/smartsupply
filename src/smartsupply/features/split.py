"""Découpage CHRONOLOGIQUE train / validation / test (README section 7.4).

Jamais de découpage aléatoire : les lignes d'une même période se ressemblent,
un mélange ferait "tricher" le modèle.

Un "trou" sépare chaque ensemble : la cible d'une ligne du train (D+1..D+7)
ne doit pas chevaucher les dates de la validation. On vérifie :
    dernière date train + 7 jours  <  première date validation
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from smartsupply.features.build import HORIZON_DAYS, TARGET


@dataclass(frozen=True)
class SplitPlan:
    train: tuple[date, date]
    validation: tuple[date, date]
    test: tuple[date, date]


# Historique simulé : du 01/01/2024 à hier (calendrier réel, ~35 mois en nov. 2026).
# Assez d'historique pour que chaque saison soit vue au moins deux fois à l'entraînement.
DEFAULT_PLAN = SplitPlan(
    train=(date(2024, 1, 1), date(2026, 3, 31)),        # ~27 mois
    validation=(date(2026, 5, 1), date(2026, 7, 31)),   # 3 mois (avril = séparation)
    test=(date(2026, 9, 1), date(2026, 11, 30)),        # sept. -> dernière cible connue (août = séparation)
)


def check_plan(plan: SplitPlan) -> None:
    gap = timedelta(days=HORIZON_DAYS)
    if not plan.train[1] + gap < plan.validation[0]:
        raise ValueError("Chevauchement train / validation : cible de train dans la validation.")
    if not plan.validation[1] + gap < plan.test[0]:
        raise ValueError("Chevauchement validation / test.")


def chronological_split(df: pd.DataFrame, plan: SplitPlan = DEFAULT_PLAN) -> dict[str, pd.DataFrame]:
    check_plan(plan)
    labeled = df[df[TARGET].notna()]
    dates = pd.to_datetime(labeled["as_of_date"]).dt.date
    return {
        name: labeled[(dates >= start) & (dates <= end)].reset_index(drop=True)
        for name, (start, end) in [
            ("train", plan.train), ("validation", plan.validation), ("test", plan.test)
        ]
    }
