"""Surveillance du modèle dans la vraie base (lecture seule).

Ignoré tant que la surveillance n'a jamais tourné : python -m smartsupply.models.monitoring
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.models.monitoring import DRIFT_FEATURES


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('ml.drift_metrics')")).scalar()
            n = conn.execute(text("SELECT count(*) FROM ml.drift_metrics")).scalar() if exists else 0
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if not n:
        pytest.skip("Surveillance jamais calculée : python -m smartsupply.models.monitoring")
    return engine


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_toutes_les_variables_chaque_jour(engine):
    assert scalar(engine, "SELECT min(n) FROM (SELECT count(*) n FROM ml.drift_metrics GROUP BY as_of_date) x") \
        == len(DRIFT_FEATURES)


def test_resume_coherent_avec_le_detail(engine):
    assert scalar(engine, """
        SELECT count(*) FROM ml.monitoring_daily m
        JOIN (SELECT as_of_date, max(psi) mx, count(*) FILTER (WHERE level = 'forte') ns
              FROM ml.drift_metrics GROUP BY as_of_date) d USING (as_of_date)
        WHERE abs(m.max_psi - d.mx) > 1e-9 OR m.n_strong <> d.ns""") == 0


def test_reference_avant_la_fin_d_entrainement(engine):
    # la référence ne doit jamais contenir de données que le modèle n'a pas vues
    assert scalar(engine, """
        SELECT count(*) FROM ml.drift_metrics d JOIN ml.model_runs r ON r.run_id = d.model_run_id
        WHERE d.reference_end > r.data_until""") == 0
