"""Garde-fou : quand un modèle est-il jugé défectueux ? (sans base)"""
import numpy as np
import pandas as pd
import pytest

from smartsupply.features.build import TARGET
from smartsupply.models.guard import assess


class Fake:
    """Modèle factice : prédit `factor` x la demande des 7 derniers jours, ou ce qu'on lui donne."""
    def __init__(self, factor=1.0, values=None, error=None):
        self.factor, self.values, self.error = factor, values, error

    def predict(self, df):
        if self.error:
            raise self.error
        if self.values is not None:
            return np.array(self.values, dtype=float)
        return self.factor * 7 * df["demand_rolling_mean_7"].to_numpy(dtype=float)


rng = np.random.default_rng(0)
LATEST = pd.DataFrame({"demand_rolling_mean_28": np.full(10, 10.0), "demand_rolling_mean_7": np.full(10, 11.0)})
# Jours récents : la demande réelle suit la moyenne 7 jours (le « bon » modèle) ; la méthode
# naïve 28 jours se trompe davantage.
m7 = rng.uniform(5, 20, 200)
RECENT = pd.DataFrame({"demand_rolling_mean_7": m7, "demand_rolling_mean_28": m7 * rng.uniform(0.6, 1.4, 200),
                       TARGET: np.rint(7 * m7 * rng.uniform(0.95, 1.05, 200)).astype(int)})


def test_un_bon_modele_passe():
    check = assess(Fake(), LATEST, RECENT, recorded_wape=0.05)
    assert check.ok, check.reason
    assert check.details["ratio_total"] == pytest.approx(1.1)


@pytest.mark.parametrize("model, expected", [
    (Fake(error=ValueError("colonnes")), "ne sait pas prédire"),
    (Fake(values=[np.nan] * 10), "manquantes ou infinies"),
    (Fake(values=[1.0] * 9), "manquantes ou infinies"),                 # un produit oublié
    (Fake(values=[-5.0] + [70.0] * 9), "négatives"),
    (Fake(factor=3.0), "demande totale prévue à 3.30 x"),
    (Fake(values=[400.0] * 3 + [10.0] * 7), "des produits prévus très loin"),
])
def test_modeles_defectueux_sur_le_dernier_jour(model, expected):
    check = assess(model, LATEST, RECENT, recorded_wape=0.05)
    assert not check.ok and expected in check.reason


def test_moins_bon_que_la_methode_naive_sur_les_jours_recents():
    class Bad(Fake):
        def predict(self, df):
            out = super().predict(df)
            return out if len(df) == len(LATEST) else out * rng.uniform(0.2, 1.8, len(out))
    check = assess(Bad(), LATEST, RECENT, recorded_wape=0.05)
    assert not check.ok and "moins bon que la méthode naïve" in check.reason


def test_erreur_recente_bien_pire_qu_a_l_entrainement():
    class Drifting(Fake):
        def predict(self, df):
            out = super().predict(df)
            return out if len(df) == len(LATEST) else out * 1.15
    check = assess(Drifting(), LATEST, RECENT, recorded_wape=0.01)
    assert not check.ok and "au-dessus de son erreur d'entraînement" in check.reason


@pytest.mark.parametrize("real, ok", [
    ((10, 0.25, 0.20), False),     # 10 jours, erreur 25 %, pire que la méthode naïve -> retour arrière
    ((10, 0.25, 0.30), True),      # erreur forte, mais la méthode naïve fait pire : période difficile
    ((10, 0.15, 0.10), True),      # sous le seuil de 18 %
    ((5, 0.40, 0.10), True),       # pas assez de jours pour juger
])
def test_vraies_previsions(real, ok):
    assert assess(Fake(), LATEST, RECENT, recorded_wape=0.05, real=real).ok is ok
