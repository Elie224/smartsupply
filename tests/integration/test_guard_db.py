"""Garde-fou sur la vraie base : un modèle cassé est mis en service, avec sa prévision du jour ;
le garde-fou doit le retirer, remettre le bon modèle et refaire la prévision.

La base est remise exactement dans son état d'origine à la fin."""
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.models import guard as guard_mod
from smartsupply.models.nightly import current_champion, dump_model, latest_dates

BROKEN = "TEST_GUARD_CASSE"


@pytest.fixture
def setup():
    engine = get_engine()
    try:
        champion = current_champion(engine)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if champion is None:
        pytest.skip("aucun modèle en service dans la base de test")
    last_day, _ = latest_dates(engine)
    with engine.connect() as conn:
        saved = pd.read_sql(text("SELECT * FROM ml.forecasts WHERE as_of_date = :d"), conn, params={"d": last_day})
    broken = DummyRegressor(strategy="constant", constant=1_000_000.0).fit([[0]], [1.0])   # prévisions absurdes
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ml.model_runs (run_id, trained_at, model_name, data_until, eval_start, eval_end,
                n_train_rows, wape, mae, bias, baseline_wape, promoted, decision, model_blob)
            VALUES (:id, '2099-01-01 02:30+00', 'test', '2099-01-01', '2099-01-01', '2099-01-01',
                    1, 0.1, 1, 0, 0.2, true, 'test', :b)"""), {"id": BROKEN, "b": dump_model(broken)})
        conn.execute(text("DELETE FROM ml.forecasts WHERE as_of_date = :d"), {"d": last_day})
        conn.execute(text("""
            INSERT INTO ml.forecasts (as_of_date, product_id, horizon_start, horizon_end, predicted_qty, run_id)
            SELECT :d, product_id, :d + 1, :d + 7, 1000000, :id FROM raw.products"""),
            {"d": last_day, "id": BROKEN})
    yield engine, champion.run_id, last_day
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ml.forecasts WHERE as_of_date = :d"), {"d": last_day})
        if not saved.empty:
            saved.to_sql("forecasts", conn, schema="ml", if_exists="append", index=False)
        conn.execute(text("DELETE FROM ml.model_runs WHERE run_id = :id"), {"id": BROKEN})


def test_retour_arriere_automatique_et_restauration(setup):
    engine, good, last_day = setup
    assert current_champion(engine).run_id == BROKEN

    result = guard_mod.guard(engine)

    assert result["status"] == f"retour arrière automatique : {BROKEN} -> {good}"
    assert "demande totale prévue" in result["reason"]
    assert current_champion(engine).run_id == good
    with engine.connect() as conn:
        reason = conn.execute(text("SELECT withdrawn_reason FROM ml.model_runs WHERE run_id = :id"),
                              {"id": BROKEN}).scalar()
        runs = dict(conn.execute(text("SELECT run_id, count(*) FROM ml.forecasts WHERE as_of_date = :d "
                                      "GROUP BY 1"), {"d": last_day}).all())
    assert reason.startswith("automatique : demande totale prévue")
    assert list(runs) == [good] and runs[good] == 50           # prévision refaite par le bon modèle
    assert guard_mod.guard(engine)["status"] == "ok"          # passage suivant : plus rien à faire


def test_sans_modele_de_secours_le_cycle_s_arrete(setup, monkeypatch):
    engine, _, _ = setup
    only_broken = [m for m in guard_mod.active_models(engine) if m["run_id"] == BROKEN]
    monkeypatch.setattr(guard_mod, "active_models", lambda e: only_broken)
    with pytest.raises(guard_mod.GuardError, match="aucun modèle de secours"):
        guard_mod.guard(engine)
    assert current_champion(engine).run_id == BROKEN          # rien n'a été retiré
