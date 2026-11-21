"""Tests du dataset ML, sans base de données.

Le test le plus important : test_aucune_fuite_du_futur.
Les variables calculées à la date D doivent être EXACTEMENT les mêmes
  (a) avec toutes les données, y compris celles d'après D ;
  (b) avec seulement les données connues à la fin du jour D.
Si une variable utilisait le futur, (a) et (b) seraient différents.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from smartsupply.features.build import FEATURES, TARGET, build_features
from smartsupply.features.extract import FeatureInputs, truncate_inputs
from smartsupply.features.split import DEFAULT_PLAN, SplitPlan, check_plan, chronological_split

START = date(2024, 1, 1)
N_DAYS = 120


def synthetic_inputs(seed=0) -> FeatureInputs:
    rng = np.random.default_rng(seed)
    days = [START + timedelta(days=i) for i in range(N_DAYS)]
    products = pd.DataFrame(
        {"product_id": [1, 2, 3], "category": ["Boissons", "Hygiène", "Textile"],
         "supplier_id": [1, 1, 2], "unit_price": [10.0, 5.0, 20.0], "lead_time_days": [5, 5, 10]}
    )
    demand = pd.DataFrame(
        [(p, d, int(rng.poisson(5)), int(rng.poisson(0.5))) for p in (1, 2, 3) for d in days],
        columns=["product_id", "day", "requested_qty", "lost_sales_qty"],
    )
    movements = pd.DataFrame(
        [(p, d, int(rng.integers(-5, 6)) or 1) for p in (1, 2, 3) for d in days],
        columns=["product_id", "day", "quantity_delta"],
    )
    purchases = []
    for _ in range(40):
        ordered = START + timedelta(days=int(rng.integers(0, N_DAYS - 5)))
        expected = ordered + timedelta(days=int(rng.integers(3, 15)))
        received = expected + timedelta(days=int(rng.integers(-1, 6)))
        purchases.append((int(rng.integers(1, 4)), ordered, expected, received, int(rng.integers(10, 50))))
    purchases = pd.DataFrame(purchases, columns=["product_id", "ordered_at", "expected_at", "received_at", "qty"])
    promotions = []
    for i in range(8):
        announced = START + timedelta(days=int(rng.integers(0, N_DAYS - 20)))
        starts = announced + timedelta(days=int(rng.integers(3, 11)))
        promotions.append((int(rng.integers(1, 4)), announced, starts, starts + timedelta(days=7), 0.2))
    promotions = pd.DataFrame(promotions, columns=["product_id", "announced_at", "starts_at", "ends_at", "discount_pct"])
    return FeatureInputs(products, demand, movements, purchases, promotions, START, days[-1])


@pytest.mark.parametrize("offset", [30, 45, 60, 77, 100])
def test_aucune_fuite_du_futur(offset):
    inputs = synthetic_inputs()
    as_of = START + timedelta(days=offset)
    full = build_features(inputs)
    known = build_features(truncate_inputs(inputs, as_of))
    a = full[full["as_of_date"] == as_of].set_index("product_id")[FEATURES]
    b = known[known["as_of_date"] == as_of].set_index("product_id")[FEATURES]
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_aucune_fuite_avec_plus_d_un_an_d_historique():
    # Les variables "an dernier" ne sont remplies qu'après un an : on teste aussi ce cas.
    inputs = synthetic_inputs_long()
    as_of = START + timedelta(days=420)
    full = build_features(inputs)
    known = build_features(truncate_inputs(inputs, as_of))
    a = full[full["as_of_date"] == as_of].set_index("product_id")[FEATURES]
    b = known[known["as_of_date"] == as_of].set_index("product_id")[FEATURES]
    assert a["demand_next7_last_year"].notna().all()
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def synthetic_inputs_long():
    global N_DAYS
    old, N_DAYS = N_DAYS, 500
    try:
        return synthetic_inputs(seed=1)
    finally:
        N_DAYS = old


def test_cible_somme_des_7_jours_suivants():
    inputs = synthetic_inputs()
    df = build_features(inputs)
    as_of = START + timedelta(days=50)
    row = df[(df["as_of_date"] == as_of) & (df["product_id"] == 2)].iloc[0]
    d = inputs.demand
    expected = d[(d["product_id"] == 2) & (d["day"] > as_of) & (d["day"] <= as_of + timedelta(days=7))]["requested_qty"].sum()
    assert row[TARGET] == expected


def test_cible_inconnue_pour_les_7_derniers_jours():
    df = build_features(synthetic_inputs())
    last = df["as_of_date"].max()
    recent = df[df["as_of_date"] > last - timedelta(days=7)]
    assert recent[TARGET].isna().all()
    assert df[df["as_of_date"] <= last - timedelta(days=7)][TARGET].notna().all()


def test_pas_de_ligne_sans_28_jours_d_historique():
    df = build_features(synthetic_inputs())
    assert df["as_of_date"].min() == START + timedelta(days=27)
    from smartsupply.features.build import NULLABLE_FEATURES
    required = [f for f in FEATURES if f not in NULLABLE_FEATURES]
    assert not df[required].isna().any().any()


def test_promotion_annoncee_visible_avant_son_debut():
    inputs = synthetic_inputs()
    promo = inputs.promotions.iloc[0]
    df = build_features(inputs)
    veille = df[(df["as_of_date"] == promo.starts_at - timedelta(days=1)) & (df["product_id"] == promo.product_id)]
    if not veille.empty and veille.iloc[0]["as_of_date"] >= promo.announced_at:
        assert veille.iloc[0]["promo_days_next_7"] > 0


def test_decoupage_chronologique_sans_chevauchement():
    check_plan(DEFAULT_PLAN)
    with pytest.raises(ValueError):
        check_plan(SplitPlan((date(2024, 1, 1), date(2024, 5, 1)),
                             (date(2024, 5, 5), date(2024, 6, 1)),
                             (date(2024, 7, 1), date(2024, 8, 1))))


def test_split_respecte_les_dates():
    df = build_features(synthetic_inputs())
    plan = SplitPlan((date(2024, 1, 1), date(2024, 2, 15)),
                     (date(2024, 3, 1), date(2024, 3, 20)),
                     (date(2024, 4, 1), date(2024, 4, 20)))
    parts = chronological_split(df, plan)
    assert parts["train"]["as_of_date"].max() + timedelta(days=7) < parts["validation"]["as_of_date"].min()
    assert parts["validation"]["as_of_date"].max() + timedelta(days=7) < parts["test"]["as_of_date"].min()
