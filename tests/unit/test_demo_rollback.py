"""Démonstration du retour arrière : pannes simulées et rapport (sans base)."""
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from smartsupply.features.build import TARGET
from smartsupply.models.demo_rollback import PANNES, Result, SabotagedModel, check_name, render_report
from smartsupply.models.guard import assess
from smartsupply.models.nightly import dump_model, load_model


class Good:
    """Bon modèle factice : prédit 7 x la moyenne 7 jours (proche de la demande réelle ci-dessous)."""
    def predict(self, df):
        return 7 * df["demand_rolling_mean_7"].to_numpy(dtype=float)


rng = np.random.default_rng(1)
m7 = rng.uniform(5, 20, 200)
RECENT = pd.DataFrame({"product_id": np.arange(200) % 50 + 1, "demand_rolling_mean_7": m7,
                       "demand_rolling_mean_28": m7 * rng.uniform(0.6, 1.4, 200),
                       TARGET: np.rint(7 * m7 * rng.uniform(0.95, 1.05, 200)).astype(int)})
LATEST = RECENT.iloc[:50].reset_index(drop=True)


def test_le_bon_modele_passe_le_garde_fou():
    assert assess(Good(), LATEST, RECENT, recorded_wape=0.05).ok


@pytest.mark.parametrize("panne, expected", [
    ("x4", "demande totale prévue"),
    ("subtile", "moins bon que la méthode naïve"),
    ("negative", "prédictions négatives"),
])
def test_chaque_panne_est_attrapee_par_son_controle(panne, expected):
    check = assess(SabotagedModel(Good(), panne), LATEST, RECENT, recorded_wape=0.05)
    assert not check.ok
    assert expected in check.reason


def test_la_panne_subtile_reste_plausible_a_l_oeil():
    """Elle passe les contrôles de la journée : seule l'erreur sur les jours récents la trahit."""
    check = assess(SabotagedModel(Good(), "subtile"), LATEST, pd.DataFrame(), recorded_wape=0.05)
    assert check.ok, check.reason


def test_panne_negative_un_produit_sur_cinq():
    pred = SabotagedModel(Good(), "negative").predict(LATEST)
    negative = LATEST["product_id"].to_numpy()[pred < 0]
    assert len(negative) == 10 and (negative % 5 == 0).all()


def test_le_modele_sabote_se_sauvegarde_comme_un_vrai():
    model = load_model(dump_model(SabotagedModel(Good(), "x4")))
    np.testing.assert_allclose(model.predict(LATEST), 4 * Good().predict(LATEST))


def test_panne_inconnue_refusee():
    with pytest.raises(ValueError):
        SabotagedModel(Good(), "autre")


@pytest.mark.parametrize("reason, expected", [
    ("demande totale prévue à 4.00 x la méthode naïve", "demande totale comparée à la méthode naïve"),
    ("moins bon que la méthode naïve sur les 28 derniers jours (60% > 19%)", "erreur sur les 28 derniers jours connus"),
    ("prédictions négatives (10 produit(s))", "valeurs négatives"),
    (None, "aucun"),
])
def test_nom_du_controle(reason, expected):
    assert check_name(reason) == expected


def _result(panne, caught=True, restored_equal=True):
    original = pd.Series([70, 30, 50], index=pd.Index([1, 2, 3], name="product_id"), dtype="int64")
    restored = original if restored_equal else original + 1
    guard = ({"status": "retour arrière automatique : DEMO_X4 -> GOOD", "reason": "demande totale prévue à 4.00 x"}
             if caught else {"status": "ok"})
    return Result(panne, PANNES[panne], f"DEMO_{panne.upper()}", "GOOD", date(2026, 11, 21), "publiée", guard, 0.4,
                  "GOOD" if caught else f"DEMO_{panne.upper()}", "liste des modèles", original, original * 4,
                  restored, [(0.0, "modèle saboté mis en service"), (1.2, "garde-fou : retour arrière")])


def test_rapport_reussi():
    report = render_report([_result("x4")], {1: "SKU-00001 Produit"}, when=datetime(2026, 11, 22, 13, 0))
    assert "# Démonstration : retour arrière automatique du modèle" in report
    assert "données au 21/11/2026" in report
    assert "| oui | oui → GOOD | identique à l’origine | 0.4 s |" in report
    assert "| **Total (3 produits)** | 150 | 600 | 150 |" in report
    assert "| SKU-00001 Produit | 70 | 280 | 70 |" in report          # produits triés par demande prévue
    assert "automatique : demande totale prévue" in report


def test_rapport_signale_un_echec():
    r = _result("x4", caught=False, restored_equal=False)
    report = render_report([r])
    assert "**non**" in report and "**différente**" in report
    assert not r.caught and not r.identical
