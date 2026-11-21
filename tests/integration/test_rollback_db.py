"""Retour arrière sur la vraie base : le champion change partout, puis tout est remis.

Deux faux modèles (datés de 2099) sont ajoutés puis SUPPRIMÉS à la fin : la base
retrouve son état d'origine."""
import pytest
from sklearn.dummy import DummyRegressor
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.models import rollback
from smartsupply.models.nightly import current_champion, dump_model

FAKE = ("TEST_ROLLBACK_A", "TEST_ROLLBACK_B")


@pytest.fixture
def engine():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            has_col = conn.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'ml' "
                "AND table_name = 'model_runs' AND column_name = 'withdrawn_at'")).scalar()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if not has_col:
        pytest.skip("Colonne withdrawn_at absente : exécute database/schema.sql")
    blob = dump_model(DummyRegressor().fit([[0]], [1.0]))
    with engine.begin() as conn:
        for i, run_id in enumerate(FAKE):
            conn.execute(text("""
                INSERT INTO ml.model_runs (run_id, trained_at, model_name, data_until, eval_start, eval_end,
                    n_train_rows, wape, mae, bias, baseline_wape, promoted, decision, model_blob)
                VALUES (:id, :t, 'test', :d, :d, :d, 1, 0.1, 1, 0, 0.2, true, 'test', :b)"""),
                {"id": run_id, "t": f"2099-01-0{i + 1} 02:30+00", "d": f"2099-01-0{i + 1}", "b": blob})
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ml.model_runs WHERE run_id IN :ids").bindparams(
            __import__("sqlalchemy").bindparam("ids", expanding=True)), {"ids": list(FAKE)})


def champion_everywhere(engine):
    with engine.connect() as conn:
        view = conn.execute(text("SELECT run_id FROM analytics.v_model_runs WHERE is_current")).scalar()
    return current_champion(engine).run_id, view


def test_retrait_puis_annulation(engine):
    assert champion_everywhere(engine) == ("TEST_ROLLBACK_B", "TEST_ROLLBACK_B")

    target, out = rollback.plan(rollback.promoted_models(engine))
    assert target.run_id == "TEST_ROLLBACK_A" and [m.run_id for m in out] == ["TEST_ROLLBACK_B"]
    rollback.withdraw(engine, ["TEST_ROLLBACK_B"], "test automatique")
    assert champion_everywhere(engine) == ("TEST_ROLLBACK_A", "TEST_ROLLBACK_A")
    with engine.connect() as conn:
        reason = conn.execute(text("SELECT withdrawn_reason FROM analytics.v_model_runs "
                                   "WHERE run_id = 'TEST_ROLLBACK_B'")).scalar()
    assert reason == "test automatique"

    rollback.restore(engine, "TEST_ROLLBACK_B")
    assert champion_everywhere(engine) == ("TEST_ROLLBACK_B", "TEST_ROLLBACK_B")
    with pytest.raises(rollback.RollbackError, match="n'est pas un modèle retiré"):
        rollback.restore(engine, "TEST_ROLLBACK_B")
