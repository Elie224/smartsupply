"""Démonstration du retour arrière sur la vraie base : les trois pannes sont attrapées, le bon modèle
revient, la prévision du jour est identique, et la base est remise exactement dans son état d'origine."""
import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.models import demo_rollback
from smartsupply.models.nightly import current_champion, latest_dates


def _snapshot(engine):
    day, _ = latest_dates(engine)
    with engine.connect() as conn:
        runs = pd.read_sql(text("SELECT run_id, promoted, withdrawn_at, withdrawn_reason FROM ml.model_runs "
                                "ORDER BY run_id"), conn)
        forecasts = pd.read_sql(text("SELECT * FROM ml.forecasts WHERE as_of_date = :d ORDER BY product_id"),
                                conn, params={"d": day})
    return runs, forecasts


@pytest.fixture
def engine():
    engine = get_engine()
    try:
        champion = current_champion(engine)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if champion is None:
        pytest.skip("aucun modèle en service dans la base de test")
    return engine


@pytest.mark.parametrize("panne", list(demo_rollback.PANNES))
def test_demonstration(engine, panne):
    before = _snapshot(engine)
    champion = current_champion(engine).run_id

    result = demo_rollback.run_scenario(engine, panne, say=lambda _: None)

    assert result.caught, result.guard
    assert result.champion_after == champion
    assert result.identical
    assert f"{result.bad_run}" in result.state_after and "automatique" in result.state_after
    after = _snapshot(engine)
    pd.testing.assert_frame_equal(before[0], after[0])          # aucun modèle ajouté ni retiré
    pd.testing.assert_frame_equal(before[1], after[1])          # prévision du jour intacte
    assert demo_rollback.leftovers(engine) == []
