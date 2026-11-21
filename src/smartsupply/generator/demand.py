"""Modèle de demande latente.

La demande d'un produit dans un entrepôt, pour un jour donné, est le produit de :
    demande de base  x  part de l'entrepôt  x  tendance  x  jour de semaine
    x  saisonnalité (par catégorie)  x  effet promotion
puis on tire une quantité aléatoire autour de cette moyenne.

Ce module ne touche pas à la base : fonctions pures, faciles à tester.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np

# Lundi ... Dimanche : pic le samedi, creux le dimanche.
WEEKDAY_FACTORS = (1.00, 1.00, 1.00, 1.05, 1.15, 1.25, 0.55)

# (amplitude, jour de l'année du pic). amplitude 0.35 => +/-35 % autour de la moyenne.
CATEGORY_SEASONALITY = {
    "Boissons":       (0.35, 196),  # été (mi-juillet)
    "Épicerie":       (0.15, 355),  # fêtes de fin d'année
    "Hygiène":        (0.05, 1),    # quasi stable
    "Entretien":      (0.10, 90),   # printemps
    "Papeterie":      (0.40, 245),  # rentrée scolaire
    "Bricolage":      (0.25, 120),  # printemps
    "Électroménager": (0.20, 330),  # Black Friday / Noël
    "Textile":        (0.25, 15),   # soldes d'hiver
}

# +1 % de remise => +3 % de demande (20 % de remise => +60 %).
PROMO_ELASTICITY = 3.0

# Sur-dispersion : la demande réelle varie plus qu'une loi de Poisson.
# On tire d'abord un multiplicateur Gamma(k, 1/k) de moyenne 1, puis Poisson.
# Plus k est petit, plus la demande est irrégulière.
OVERDISPERSION_K = 5.0


@dataclass(frozen=True)
class ProductProfile:
    """Caractéristiques d'un produit utiles à la simulation."""
    product_id: int
    category: str
    supplier_id: int
    unit_price: Decimal
    unit_cost: Decimal
    lead_time_days: int
    reliability: float
    base_daily_demand: float   # unités / jour, tous entrepôts confondus
    trend_per_year: float      # +0.05 = +5 % par an


def build_profile(
    product_id: int,
    category: str,
    supplier_id: int,
    unit_price: Decimal,
    unit_cost: Decimal,
    lead_time_days: int,
    reliability: float,
    seed: int,
) -> ProductProfile:
    """Paramètres cachés d'un produit, tirés avec une graine propre au produit.

    Graine [seed, 1, product_id] : le profil d'un produit ne dépend pas des autres,
    donc ajouter des produits ne change pas ceux qui existent déjà.
    """
    rng = np.random.default_rng([seed, 1, product_id])
    return ProductProfile(
        product_id=product_id,
        category=category,
        supplier_id=supplier_id,
        unit_price=unit_price,
        unit_cost=unit_cost,
        lead_time_days=lead_time_days,
        reliability=reliability,
        # Log-normale : beaucoup de produits lents, quelques produits très rapides.
        base_daily_demand=float(rng.lognormal(mean=math.log(8), sigma=0.9)),
        trend_per_year=float(rng.normal(0.05, 0.10)),
    )


def planned_daily_demand(profile: ProductProfile, warehouse_share: float) -> float:
    """Estimation « catalogue » de la demande moyenne (sans saison, promo ni tendance).

    Utilisée seulement au démarrage, tant que l'acheteur n'a pas 7 jours d'historique ;
    ensuite il se base sur la demande observée (voir simulator.estimated_daily_demand).
    """
    return profile.base_daily_demand * warehouse_share


def expected_demand(
    profile: ProductProfile,
    warehouse_share: float,
    day: date,
    discount_pct: float,
    history_start: date,
) -> float:
    """Demande moyenne attendue (lambda) pour un produit, un entrepôt, un jour."""
    years = (day - history_start).days / 365.25
    trend = max(0.2, 1.0 + profile.trend_per_year * years)
    weekday = WEEKDAY_FACTORS[day.weekday()]
    amplitude, peak = CATEGORY_SEASONALITY.get(profile.category, (0.0, 1))
    day_of_year = day.timetuple().tm_yday
    season = 1.0 + amplitude * math.cos(2 * math.pi * (day_of_year - peak) / 365.25)
    promo = 1.0 + PROMO_ELASTICITY * discount_pct
    return (
        profile.base_daily_demand * warehouse_share * trend * weekday * season * promo
    )


def sample_demand(rng: np.random.Generator, lam: float) -> int:
    """Quantité demandée tirée autour de lambda (Gamma-Poisson = binomiale négative)."""
    if lam <= 0:
        return 0
    multiplier = rng.gamma(OVERDISPERSION_K, 1.0 / OVERDISPERSION_K)
    return int(rng.poisson(lam * multiplier))
