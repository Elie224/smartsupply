"""Dataset ML construit depuis la vraie base. Nécessite PostgreSQL démarré."""
from datetime import timedelta

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.features.build import FEATURES, TARGET, build_features
from smartsupply.features.extract import load_inputs, truncate_inputs


@pytest.fixture(scope="module")
def data():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('ml.feature_snapshots')")).scalar()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if exists is None:
        pytest.skip("Table ml.feature_snapshots absente : exécute database/schema.sql")
    inputs = load_inputs(engine)
    return engine, inputs, build_features(inputs)


def test_une_ligne_par_produit_et_par_jour(data):
    _, _, df = data
    assert not df.duplicated(["as_of_date", "product_id"]).any()


def test_aucune_fuite_sur_les_vraies_donnees(data):
    _, inputs, full = data
    dates = sorted(full["as_of_date"].unique())
    for as_of in (dates[len(dates) // 3], dates[2 * len(dates) // 3]):
        known = build_features(truncate_inputs(inputs, as_of))
        a = full[full["as_of_date"] == as_of].set_index("product_id")[FEATURES]
        b = known[known["as_of_date"] == as_of].set_index("product_id")[FEATURES]
        pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_cible_egale_a_la_demande_sql(data):
    engine, _, df = data
    row = df[df[TARGET].notna()].iloc[len(df) // 2]
    with engine.connect() as conn:
        expected = conn.execute(
            text(
                """
                SELECT coalesce(sum(oi.requested_qty), 0)
                FROM raw.order_items oi JOIN raw.orders o USING (order_id)
                WHERE oi.product_id = :p
                  AND (o.ordered_at AT TIME ZONE 'UTC')::date BETWEEN :d1 AND :d7
                """
            ),
            {"p": int(row.product_id), "d1": row.as_of_date + timedelta(days=1),
             "d7": row.as_of_date + timedelta(days=7)},
        ).scalar_one()
    assert row[TARGET] == expected


def test_stock_egal_au_solde_actuel(data):
    engine, inputs, df = data
    last = df[df["as_of_date"] == inputs.last_day].set_index("product_id")["stock_as_of"]
    with engine.connect() as conn:
        balances = pd.read_sql(
            text("SELECT product_id, sum(on_hand_qty) AS qty FROM raw.inventory_balances GROUP BY 1"),
            conn,
        ).set_index("product_id")["qty"]
    assert (last.sort_index() == balances.reindex(last.index).sort_index()).all()
