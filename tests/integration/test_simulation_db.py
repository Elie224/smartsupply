"""Tests d'intégration du simulateur. Nécessitent PostgreSQL démarré.

Ils ne créent JAMAIS de date future : s'il reste une journée terminée à simuler,
elle est ajoutée (comme le ferait le pipeline), sinon le test correspondant est ignoré.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.generator.reference_data import generate_reference_data
from smartsupply.generator.runner import (
    NothingToSimulate,
    last_complete_day,
    next_day_to_simulate,
    simulate_next_day,
)


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable ({exc.orig}) : lance `docker compose up -d postgres`")
    generate_reference_data(engine=engine)
    return engine


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_jour_suivant_sans_doublon(engine):
    avant = next_day_to_simulate(engine)
    if avant > last_complete_day():
        pytest.skip("Historique à jour : la journée en cours n'est pas terminée.")
    summary = simulate_next_day(engine, mode="once")
    assert summary.day == avant
    assert next_day_to_simulate(engine) > avant


def test_aucune_date_dans_le_futur(engine):
    with engine.connect() as conn:
        last = conn.execute(text("SELECT max(simulated_date) FROM raw.simulation_runs")).scalar()
    assert last is None or last <= last_complete_day()


def test_refuse_de_simuler_une_journee_non_terminee(engine):
    if next_day_to_simulate(engine) <= last_complete_day():
        pytest.skip("Il reste des journées terminées à simuler.")
    with pytest.raises(NothingToSimulate):
        simulate_next_day(engine, mode="once")


def test_jours_consecutifs_et_uniques(engine):
    assert scalar(
        engine,
        "SELECT count(*) = count(DISTINCT simulated_date) "
        "AND count(*) = max(simulated_date) - min(simulated_date) + 1 "
        "FROM raw.simulation_runs",
    )


def test_soldes_egaux_au_journal(engine):
    ecarts = scalar(
        engine,
        """
        SELECT count(*) FROM raw.inventory_balances b
        JOIN (SELECT product_id, warehouse_id, sum(quantity_delta) AS total
              FROM raw.stock_movements GROUP BY 1, 2) m USING (product_id, warehouse_id)
        WHERE m.total <> b.on_hand_qty
        """,
    )
    assert ecarts == 0


def test_aucun_stock_negatif(engine):
    assert scalar(engine, "SELECT count(*) FROM raw.inventory_balances WHERE on_hand_qty < 0") == 0


def test_commandes_apres_inscription_client(engine):
    assert scalar(
        engine,
        "SELECT count(*) FROM raw.orders o JOIN raw.clients c USING (client_id) "
        "WHERE o.ordered_at < c.created_at",
    ) == 0


def test_receptions_apres_commande(engine):
    assert scalar(
        engine,
        "SELECT count(*) FROM raw.purchase_orders WHERE received_at <= ordered_at",
    ) == 0


def test_journal_append_only(engine):
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE raw.stock_movements SET quantity_delta = 1 WHERE movement_id = 1"))


def test_controles_qualite_ok(engine):
    from smartsupply.quality.checks import run_quality_checks
    assert all(v == 0 for v in run_quality_checks(engine).values())


def test_commandes_reparties_sur_beaucoup_de_clients(engine):
    # La sélection doit toucher une large part de la base clients, pas 3 clients.
    distincts = scalar(engine, "SELECT count(DISTINCT client_id) FROM raw.orders")
    total = scalar(engine, "SELECT count(*) FROM raw.clients WHERE created_at < '2024-01-01'")
    assert distincts >= 0.05 * total


def test_historique_de_demande_relu_depuis_la_base(engine):
    """L'acheteur relit chaque jour la demande des 28 derniers jours depuis la base :
    la dernière valeur doit être la demande totale de la veille, par produit x entrepôt."""
    from datetime import timedelta

    from smartsupply.generator.repository import load_recent_demand
    from smartsupply.generator.simulator import DEMAND_WINDOW_DAYS

    with engine.connect() as conn:
        last = conn.execute(text("SELECT max(simulated_date) FROM raw.simulation_runs")).scalar()
        if last is None:
            pytest.skip("aucune journée simulée")
        balances = {(r[0], r[1]): 0 for r in conn.execute(
            text("SELECT product_id, warehouse_id FROM raw.inventory_balances"))}
        history = load_recent_demand(conn, last + timedelta(days=1), balances)
        expected = {
            (r.product_id, r.warehouse_id): int(r.qty)
            for r in conn.execute(text(
                "SELECT product_id, warehouse_id, sum(requested_qty) AS qty "
                "FROM analytics.v_daily_sales WHERE sales_date = :d GROUP BY 1, 2"), {"d": last})
        }
    assert all(len(h) == DEMAND_WINDOW_DAYS for h in history.values())
    assert {k: h[-1] for k, h in history.items() if h[-1]} == expected
