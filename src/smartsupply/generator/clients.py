"""Comportement des clients : QUI commande, QUOI, et OÙ.

Chaque client Faker reçoit un profil caché, tiré avec une graine propre au client
(donc stable d'une exécution à l'autre) :
  * fréquence d'achat : B2B (tous les 2-5 jours), particulier régulier
    (tous les 4-10 jours) ou occasionnel (tous les 1-3 mois) ;
  * affinités : chaque client préfère certaines catégories ;
  * entrepôt de rattachement (proportionnel à la capacité des entrepôts) ;
  * taille des lignes : un pro commande plus d'unités qu'un particulier.

La sélection : pour chaque unité de demande d'un produit, on choisit le client
qui l'achète avec une probabilité proportionnelle à
    fréquence du client  x  affinité du client pour la catégorie du produit.
Les clients fréquents et intéressés par la catégorie sont donc choisis plus souvent ;
les occasionnels apparaissent rarement.

Cycle de vie (paramètres cachés, comme dans la réalité) :
  * arrivée : ~0,2 nouveau client par jour ;
  * montée en puissance : un nouveau client achète à 30 % de son rythme, puis
    atteint son rythme normal en RAMP_UP_DAYS jours ;
  * départ : chaque client a une durée de vie tirée au hasard (taux de départ
    FAIBLE, voir ANNUAL_CHURN). Un client parti ne commande plus, c'est tout :
    la base ne contient aucun indicateur « parti ». Côté métier, un client est
    considéré PERDU après 12 mois sans commande (vue analytics.v_clients).

La demande totale suit la base de clients : un départ fait baisser la demande de
ses catégories préférées, un nouveau client la fait monter (client_base_factor).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from smartsupply.generator.demand import CATEGORY_SEASONALITY

CATEGORIES = tuple(CATEGORY_SEASONALITY)
REGULAR_B2C_SHARE = 0.30       # 30 % des particuliers sont des clients réguliers
NEW_CLIENTS_PER_DAY = 0.2      # ~1 nouveau client tous les 5 jours : rare
NEW_CLIENT_B2B_SHARE = 0.30
RAMP_UP_DAYS = 60              # 30 % du rythme le 1er jour -> 100 % après 60 jours
RAMP_UP_START = 0.30
# Probabilité de partir dans l'année (faible). Moyenne pondérée : ~5 % par an.
ANNUAL_CHURN = {"B2B": 0.03, "B2C_regulier": 0.04, "B2C_occasionnel": 0.07}


@dataclass(frozen=True)
class ClientProfile:
    client_id: int
    segment: str                       # 'B2B' ou 'B2C'
    warehouse_id: int
    order_rate: float                  # commandes / jour en moyenne
    affinity: tuple[float, ...]        # une valeur par catégorie (somme = 1)
    line_size_p: float                 # paramètre géométrique : petit = grosses lignes
    start: date | None = None          # début d'activité simulée (None = toujours actif)
    is_new: bool = False               # inscrit pendant la simulation -> montée en puissance
    churn_at: date | None = None       # premier jour SANS commande (caché)

    def active(self, day: date) -> bool:
        return self.churn_at is None or day < self.churn_at

    def ramp(self, day: date) -> float:
        if not self.is_new or self.start is None:
            return 1.0
        progress = max(0, (day - self.start).days) / RAMP_UP_DAYS
        return min(1.0, RAMP_UP_START + (1 - RAMP_UP_START) * progress)

    def weight(self, category: str, day: date | None = None) -> float:
        """Poids d'achat dans la catégorie (0 si le client est parti)."""
        base = self.order_rate * self.affinity[CATEGORIES.index(category)]
        if day is None:
            return base
        return base * self.ramp(day) if self.active(day) else 0.0


def build_client_profile(
    client_id: int, segment: str, shares: dict[int, float], seed: int,
    created: date | None = None, history_start: date | None = None,
) -> ClientProfile:
    """Profil caché d'un client. Sans `created`, le client ne part jamais (tests)."""
    rng = np.random.default_rng([seed, 3, client_id])
    warehouses = sorted(shares)
    warehouse_id = int(rng.choice(warehouses, p=[shares[w] for w in warehouses]))

    if segment == "B2B":
        order_rate = float(rng.uniform(1 / 5, 1 / 2))
        line_size_p = 0.30             # ~3 unités par ligne
        kind = "B2B"
    elif rng.random() < REGULAR_B2C_SHARE:
        order_rate = float(rng.uniform(1 / 10, 1 / 4))
        line_size_p = 0.65             # ~1,5 unité par ligne
        kind = "B2C_regulier"
    else:
        order_rate = float(rng.uniform(1 / 90, 1 / 30))
        line_size_p = 0.65
        kind = "B2C_occasionnel"

    # Dirichlet(0.8) = préférences marquées, mélangé à 50 % d'uniforme
    # pour que chacun puisse quand même acheter de tout.
    preference = rng.dirichlet([0.8] * len(CATEGORIES))
    affinity = tuple(float(x) for x in 0.5 * preference + 0.5 / len(CATEGORIES))

    start = is_new = churn_at = None
    if created is not None:
        history_start = history_start or created
        start = max(created, history_start)
        is_new = created >= history_start
        # Durée de vie exponentielle : P(partir dans l'année) = ANNUAL_CHURN[kind].
        # Tirage séparé ([seed, 4, client_id]) : ne change pas les autres paramètres.
        mean_days = -365.0 / math.log(1.0 - ANNUAL_CHURN[kind])
        life = np.random.default_rng([seed, 4, client_id]).exponential(mean_days)
        churn_at = start + timedelta(days=int(life))
    return ClientProfile(client_id, segment, warehouse_id, order_rate, affinity, line_size_p,
                         start, bool(is_new), churn_at)


def client_base_factor(clients: list[ClientProfile], warehouse_id: int, category: str,
                       day: date, history_start: date) -> float:
    """Pouvoir d'achat des clients actifs / celui de la base de départ (1 = inchangé).

    Base de départ = clients inscrits avant le début de l'historique, tous actifs.
    """
    current = reference = 0.0
    for c in clients:
        if c.warehouse_id != warehouse_id:
            continue
        current += c.weight(category, day)
        if c.start is None or not c.is_new:
            reference += c.weight(category)
    return current / reference if reference else 1.0


class ClientSelector:
    """Tire rapidement un client pour (entrepôt, catégorie) selon les poids."""

    def __init__(self, clients: list[ClientProfile], day: date | None = None):
        self._table: dict[tuple[int, str], tuple[list[ClientProfile], np.ndarray]] = {}
        warehouses = sorted({c.warehouse_id for c in clients})
        for warehouse_id in warehouses:
            local = [c for c in clients if c.warehouse_id == warehouse_id]
            for category in CATEGORIES:
                weights = np.array([c.weight(category, day) for c in local])
                if weights.sum() <= 0:
                    continue          # plus aucun client actif pour cette catégorie
                self._table[(warehouse_id, category)] = (local, np.cumsum(weights) / weights.sum())

    def has(self, warehouse_id: int, category: str | None = None) -> bool:
        if category is not None:
            return (warehouse_id, category) in self._table
        return any(w == warehouse_id for w, _ in self._table)

    def pick(self, rng: np.random.Generator, warehouse_id: int, category: str) -> ClientProfile:
        local, cumulative = self._table[(warehouse_id, category)]
        index = int(np.searchsorted(cumulative, rng.random(), side="right"))
        return local[min(index, len(local) - 1)]
