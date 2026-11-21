"""Tests du cycle nocturne : règle de mise en service, fenêtre d'évaluation, sérialisation."""
from datetime import timedelta

import numpy as np

from smartsupply.features.build import HORIZON_DAYS
from smartsupply.models.estimators import LightGBMRatioModel
from smartsupply.models.nightly import (
    BIAS_LIMIT, EVAL_DAYS, WAPE_TOLERANCE, backtest_window, decide, dump_model, load_model,
)

from test_models import synthetic

NAIVE = {"wape": 0.18, "bias": 0.0}


def test_premier_modele_mis_en_service_s_il_bat_la_methode_naive():
    assert decide({"wape": 0.14, "bias": 0.01}, NAIVE, None).promote


def test_refuse_s_il_ne_bat_pas_la_methode_naive():
    assert not decide({"wape": 0.19, "bias": 0.0}, NAIVE, None).promote


def test_refuse_si_le_biais_est_trop_fort():
    assert not decide({"wape": 0.14, "bias": -(BIAS_LIMIT + 0.01)}, NAIVE, None).promote


def test_refuse_s_il_est_nettement_moins_bon_que_le_modele_en_place():
    assert not decide({"wape": 0.13 + WAPE_TOLERANCE + 0.001, "bias": 0.0}, NAIVE, 0.13).promote


def test_accepte_une_petite_variation_due_au_bruit():
    assert decide({"wape": 0.13 + WAPE_TOLERANCE / 2, "bias": 0.0}, NAIVE, 0.13).promote


def test_fenetre_d_evaluation_sans_chevauchement():
    df = synthetic(n_days=200)
    train_end, eval_start, eval_end = backtest_window(df)
    assert eval_end == df["as_of_date"].max()
    assert (eval_end - eval_start).days + 1 == EVAL_DAYS
    # la cible de la dernière ligne d'entraînement (D+1..D+7) s'arrête avant l'évaluation
    assert train_end + timedelta(days=HORIZON_DAYS) < eval_start


def test_modele_identique_apres_passage_par_la_base():
    df = synthetic(n_days=120)
    model = LightGBMRatioModel({"n_estimators": 30}).fit(df)
    restored = load_model(dump_model(model))
    np.testing.assert_allclose(model.predict(df), restored.predict(df))
