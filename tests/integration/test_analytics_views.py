"""Les vues analytics doivent redonner EXACTEMENT les chiffres des tables raw."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('analytics.v_daily_sales')")).scalar()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if exists is None:
        pytest.skip("Vues absentes : exécute database/analytics_views.sql")
    return engine


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


# Le pipeline peut ajouter un jour pendant les tests : chaque comparaison est faite
# dans UNE seule requête SQL, donc sur la même photo (snapshot) de la base.

def test_ventes_journalieres_egales_aux_lignes_brutes(engine):
    assert scalar(
        engine,
        "SELECT (SELECT sum(requested_qty) FROM analytics.v_daily_sales)"
        "     = (SELECT sum(requested_qty) FROM raw.order_items)",
    )


def test_ca_identique_entre_vues(engine):
    assert scalar(
        engine,
        "SELECT (SELECT sum(revenue_eur) FROM analytics.v_daily_kpis)"
        "     = (SELECT sum(revenue_eur) FROM analytics.v_daily_sales)",
    )


def test_un_statut_de_stock_par_produit_et_entrepot(engine):
    assert scalar(
        engine,
        "SELECT (SELECT count(*) FROM analytics.v_stock_status)"
        "     = (SELECT count(*) FROM raw.inventory_balances)",
    )


def test_tous_les_fournisseurs_presents(engine):
    assert scalar(
        engine,
        "SELECT (SELECT count(*) FROM analytics.v_supplier_reliability)"
        "     = (SELECT count(*) FROM raw.suppliers)",
    )


def test_regle_metier_client_perdu(engine):
    """Perdu <=> 12 mois sans commande ; un client perdu n'a aucune commande récente."""
    assert scalar(
        engine,
        "SELECT bool_and((client_status = 'perdu') = (days_without_order >= 365)) FROM analytics.v_clients",
    )
    assert scalar(
        engine,
        "SELECT count(*) FROM analytics.v_clients WHERE client_status = 'perdu' "
        "AND last_order > (SELECT max(simulated_date) FROM raw.simulation_runs) - 365",
    ) == 0


def test_taux_de_perte_faible(engine):
    lost = scalar(engine, "SELECT count(*) FROM analytics.v_clients WHERE client_status = 'perdu'")
    total = scalar(engine, "SELECT count(*) FROM analytics.v_clients")
    assert lost / total < 0.15      # ~4-5 % par an sur ~3 ans d'historique
