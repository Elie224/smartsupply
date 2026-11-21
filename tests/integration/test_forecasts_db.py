"""Prévisions et registre des modèles dans la vraie base (lecture seule).

Ignorés tant que le cycle nocturne n'a jamais tourné :
    python -m smartsupply.models.nightly
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('ml.forecasts')")).scalar()
            n = conn.execute(text("SELECT count(*) FROM ml.forecasts")).scalar() if exists else 0
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if exists is None:
        pytest.skip("Table ml.forecasts absente : exécute database/schema.sql")
    if n == 0:
        pytest.skip("Aucune prévision : lance `python -m smartsupply.models.nightly`")
    return engine


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_un_seul_modele_en_service_et_il_est_stocke(engine):
    assert scalar(engine, "SELECT count(*) FROM analytics.v_model_runs WHERE is_current") == 1
    assert scalar(engine, "SELECT bool_and(model_blob IS NOT NULL) FROM ml.model_runs WHERE promoted")


def test_derniere_prevision_couvre_tous_les_produits(engine):
    assert scalar(
        engine,
        "SELECT (SELECT count(*) FROM ml.forecasts WHERE as_of_date = (SELECT max(as_of_date) FROM ml.forecasts))"
        "     = (SELECT count(*) FROM raw.products)",
    )


def test_prevision_faite_avec_un_modele_entraine_avant(engine):
    # le modèle n'a pas pu voir la période qu'il prévoit
    assert scalar(
        engine,
        "SELECT count(*) FROM ml.forecasts f JOIN ml.model_runs r USING (run_id)"
        " WHERE r.data_until + 7 > f.as_of_date",
    ) == 0


def test_aucune_prevision_depuis_une_journee_non_terminee(engine):
    # une prévision part de la dernière journée terminée : jamais d'une date à venir
    assert scalar(engine, "SELECT max(as_of_date) < (now() AT TIME ZONE 'UTC')::date FROM ml.forecasts")
