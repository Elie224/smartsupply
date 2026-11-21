"""Surveillance : PSI, niveaux de dérive, résumé, métriques MLflow (sans base)."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from smartsupply.models import monitoring
from smartsupply.models.tracking import monitoring_metrics

rng = np.random.default_rng(0)


def test_psi_nul_pour_la_meme_distribution():
    x = pd.Series(rng.normal(100, 10, 5000))
    assert monitoring.psi(x, x) == pytest.approx(0, abs=1e-9)
    assert monitoring.psi(x, pd.Series(rng.normal(100, 10, 5000))) < monitoring.WATCH   # simple bruit


def test_psi_grandit_avec_le_decalage():
    ref = pd.Series(rng.normal(100, 10, 5000))
    small = monitoring.psi(ref, pd.Series(rng.normal(103, 10, 5000)))
    big = monitoring.psi(ref, pd.Series(rng.normal(120, 10, 5000)))
    assert small < big and big > monitoring.STRONG


def test_psi_variable_constante_detecte_un_changement():
    zeros = pd.Series(np.zeros(1000))                     # ex. jamais de remise dans la référence
    assert monitoring.psi(zeros, zeros) == pytest.approx(0, abs=1e-9)
    assert monitoring.psi(zeros, pd.Series(np.r_[np.zeros(500), np.full(500, 0.2)])) > monitoring.STRONG


def test_psi_ignore_les_valeurs_manquantes_et_series_vides():
    x = pd.Series([1.0, 2.0, np.nan, 3.0] * 100)
    assert monitoring.psi(x, x) == pytest.approx(0, abs=1e-9)
    assert monitoring.psi(pd.Series(dtype=float), x) == 0.0


@pytest.mark.parametrize("value, expected", [(0.0, "stable"), (0.099, "stable"), (0.10, "à surveiller"),
                                             (0.25, "à surveiller"), (0.26, "forte")])
def test_niveaux(value, expected):
    assert monitoring.level(value) == expected


def test_fenetres_un_an_de_reference_28_jours_recents():
    ref_start, ref_end, cur_start = monitoring.windows(date(2026, 11, 20), date(2026, 11, 13))
    assert (ref_end - ref_start).days + 1 == 365 and ref_end == date(2026, 11, 13)
    assert (date(2026, 11, 20) - cur_start).days + 1 == 28


def test_calendrier_exclu_de_la_derive():
    assert not monitoring.CALENDAR & set(monitoring.DRIFT_FEATURES)
    assert "demand_rolling_mean_28" in monitoring.DRIFT_FEATURES


def test_resume():
    drift = pd.DataFrame({"feature": ["a", "b", "c"], "psi": [0.4, 0.15, 0.01]})
    drift["level"] = drift["psi"].map(monitoring.level)
    s = monitoring.summarize(drift)
    assert (s["n_strong"], s["n_watch"], s["top_feature"]) == (1, 1, "a")
    assert s["status"].startswith("dérive forte")
    calm = drift.assign(psi=[0.05, 0.02, 0.01], level="stable")
    assert monitoring.summarize(calm)["status"].startswith("stable")


def day(**kw):
    base = {"as_of_date": date(2026, 11, 20), "max_psi": 0.11, "n_watch": 1, "n_strong": 0,
            "drift_logged": False, "real_wape": None, "real_bias": None, "real_logged": False}
    return {**base, **kw}


def test_metriques_mlflow_seulement_ce_qui_manque():
    m = monitoring_metrics(day(), {"cv_28": 0.03})
    assert m["drift_max_psi"] == 0.11 and m["psi_cv_28"] == 0.03 and "real_wape" not in m
    m = monitoring_metrics(day(drift_logged=True, real_wape=0.12, real_bias=-0.01), {"cv_28": 0.03})
    assert set(m) == {"real_wape", "real_bias"}                    # dérive déjà envoyée
    assert monitoring_metrics(day(drift_logged=True, real_wape=0.1, real_bias=0, real_logged=True), {}) == {}
