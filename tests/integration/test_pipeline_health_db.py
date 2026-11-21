"""Vue analytics.v_pipeline_health sur la vraie base.

Les lignes ajoutées au journal sont dans une transaction ANNULÉE à la fin :
la base n'est pas modifiée.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine

HEALTH = "SELECT health_status, health_message FROM analytics.v_pipeline_health"


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            view = conn.execute(text("SELECT to_regclass('analytics.v_pipeline_health')")).scalar()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if view is None:
        pytest.skip("Vue absente : exécute database/schema.sql puis database/analytics_views.sql")
    return engine


def add_run(conn, status, message):
    conn.execute(text("INSERT INTO ops.pipeline_runs (started_at, finished_at, status, message) "
                      "VALUES (now(), clock_timestamp() + interval '1 minute', :s, :m)"),
                 {"s": status, "m": message})


def test_une_seule_ligne_et_un_etat_connu(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(HEALTH)).all()
    assert len(rows) == 1
    assert rows[0].health_status in {"ok", "en retard", "échec", "aucun passage"}


def test_dernier_passage_en_echec_donne_une_alerte(engine):
    with engine.connect() as conn, conn.begin() as tx:
        add_run(conn, "échec", "test : base injoignable")
        status, message = conn.execute(text(HEALTH)).one()
        tx.rollback()
    assert status == "échec"
    assert "test : base injoignable" in message


def test_une_reussite_apres_un_echec_efface_l_alerte_echec(engine):
    with engine.connect() as conn, conn.begin() as tx:
        add_run(conn, "échec", "test")
        conn.execute(text("INSERT INTO ops.pipeline_runs (started_at, finished_at, status, message) "
                          "VALUES (now(), clock_timestamp() + interval '2 minutes', 'ok', 'test')"))
        status, _ = conn.execute(text(HEALTH)).one()
        tx.rollback()
    assert status != "échec"
