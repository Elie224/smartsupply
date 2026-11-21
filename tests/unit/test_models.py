"""Tests des métriques, baselines et modèles (sans base de données)."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from smartsupply.features.build import FEATURES, HORIZON_DAYS, TARGET
from smartsupply.models.baselines import BASELINES, predict_baseline
from smartsupply.models.estimators import MODELS, add_derived
from smartsupply.models.metrics import bias, mae, wape
from smartsupply.models.train import rolling_origin_folds


def test_metriques_sur_un_exemple_calcule_a_la_main():
    y, p = [10, 20, 30], [12, 18, 30]
    assert mae(y, p) == pytest.approx(4 / 3)
    assert wape(y, p) == pytest.approx(4 / 60)
    assert bias(y, p) == pytest.approx(0.0)
    assert bias([10], [8]) == pytest.approx(-0.2)    # sous-estimation = biais négatif


def synthetic(n_days=200, n_products=6, seed=0) -> pd.DataFrame:
    """Petit dataset avec la même structure que le vrai, et une cible liée aux features."""
    rng = np.random.default_rng(seed)
    rows = []
    for p in range(1, n_products + 1):
        level = rng.uniform(2, 30)
        for d in range(n_days):
            m28 = level * (1 + 0.2 * np.sin(d / 30))
            promo = int(rng.random() < 0.1)
            row = {f: 0.0 for f in FEATURES}
            row.update(
                as_of_date=date(2024, 1, 28) + timedelta(days=d), product_id=p,
                category=["Boissons", "Hygiène"][p % 2], supplier_id=1,
                demand_rolling_mean_28=m28, demand_rolling_mean_7=m28 * rng.uniform(0.8, 1.2),
                demand_rolling_std_28=m28 * 0.3, promo_days_next_7=7 * promo,
                promotion_active=False, is_weekend=False, stock_as_of=50, weekday=d % 7,
            )
            row[TARGET] = int(rng.poisson(7 * m28 * (1 + 0.5 * promo)))
            rows.append(row)
    return pd.DataFrame(rows)


@pytest.mark.parametrize("name", list(BASELINES))
def test_baselines_positives(name):
    assert (predict_baseline(name, synthetic()) >= 0).all()


@pytest.mark.parametrize("name", list(MODELS))
def test_modeles_entrainent_et_predisent_des_quantites_positives(name):
    df = synthetic()
    train, test = df[df["as_of_date"] < date(2024, 6, 1)], df[df["as_of_date"] >= date(2024, 6, 15)]
    pred = MODELS[name]().fit(train).predict(test)
    assert len(pred) == len(test) and (pred >= 0).all()
    # un modèle entraîné doit faire mieux qu'une prévision constante
    assert wape(test[TARGET], pred) < wape(test[TARGET], np.full(len(test), train[TARGET].mean()))


def test_lightgbm_capte_l_effet_promotion():
    df = synthetic(n_days=400)
    model = MODELS["lightgbm"]().fit(df)
    base = df.iloc[[100]].copy()
    sans, avec = base.assign(promo_days_next_7=0), base.assign(promo_days_next_7=7)
    assert model.predict(avec)[0] > model.predict(sans)[0]


def test_variables_derivees_sans_division_par_zero():
    df = synthetic().assign(demand_rolling_mean_28=0.0)
    out = add_derived(df)
    assert np.isfinite(out[["mean7_over_mean28", "cv_28", "stock_cover_days"]].to_numpy()).all()


def test_validation_glissante_sans_chevauchement_de_cible():
    df = synthetic(n_days=300)
    for train, evaluation in rolling_origin_folds(df):
        assert train["as_of_date"].max() + timedelta(days=HORIZON_DAYS) < evaluation["as_of_date"].min()
