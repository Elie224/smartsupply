"""Tests d'intégration : nécessitent le PostgreSQL Docker démarré."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.generator.reference_data import generate_reference_data


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


@pytest.mark.parametrize(
    "table, minimum",
    [("raw.suppliers", 10), ("raw.products", 50), ("raw.clients", 1000), ("raw.warehouses", 3)],
)
def test_lignes_presentes(engine, table, minimum):
    assert scalar(engine, f"SELECT count(*) FROM {table}") >= minimum


def test_aucun_produit_orphelin(engine):
    orphelins = scalar(
        engine,
        """
        SELECT count(*) FROM raw.products p
        LEFT JOIN raw.suppliers s ON s.supplier_id = p.supplier_id
        WHERE s.supplier_id IS NULL
        """,
    )
    assert orphelins == 0


def test_relance_sans_doublons(engine):
    avant = scalar(engine, "SELECT count(*) FROM raw.clients")
    stats = generate_reference_data(engine=engine)
    apres = scalar(engine, "SELECT count(*) FROM raw.clients")
    assert apres == avant
    assert all(v == 0 for v in stats.values())


def test_entites_creees_avant_historique(engine):
    # Produits et clients de référence existent avant l'historique.
    # Les clients inscrits après 2024 doivent l'avoir été un jour simulé.
    assert scalar(
        engine,
        """
        SELECT (SELECT count(*) FROM raw.products WHERE created_at >= '2024-01-01')
             + (SELECT count(*) FROM raw.clients c
                WHERE c.created_at >= '2024-01-01'
                  AND NOT EXISTS (SELECT 1 FROM raw.simulation_runs r
                                  WHERE r.simulated_date = c.created_at::date))
        """,
    ) == 0
