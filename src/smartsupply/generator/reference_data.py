"""Données de référence : fournisseurs, produits, clients, entrepôts.

Usage :
    python -m smartsupply.generator.reference_data
    python -m smartsupply.generator.reference_data --clients 1000 --products 50 --suppliers 10

Principes :
  * Reproductible : même seed -> mêmes données (Faker ET NumPy sont seedés).
  * Idempotent : relancer le script n'ajoute pas de doublons
    (identifiants fixes + ON CONFLICT DO NOTHING).
  * Atomique : tout est écrit dans une seule transaction (tout ou rien).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from faker import Faker
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from smartsupply.db.connection import get_engine

SEED = 42

# Les entités doivent exister AVANT le début de l'historique simulé :
# aucune commande ne pourra être datée avant la création d'un produit/client.
ENTITIES_START = datetime(2022, 1, 1, tzinfo=timezone.utc)
HISTORY_START = datetime(2024, 1, 1, tzinfo=timezone.utc)

CATEGORIES = [
    "Épicerie", "Boissons", "Hygiène", "Entretien",
    "Papeterie", "Bricolage", "Électroménager", "Textile",
]
COUNTRIES = [
    "France", "Allemagne", "Espagne", "Italie",
    "Belgique", "Pologne", "Chine", "Maroc",
]
WAREHOUSES = [
    ("Entrepôt Paris Nord", "Paris", 50_000),
    ("Entrepôt Lyon Sud", "Lyon", 35_000),
    ("Entrepôt Marseille Port", "Marseille", 30_000),
]
B2B_SHARE = 0.30  # 30 % de clients professionnels


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------
def _money(value: float) -> Decimal:
    """Arrondi monétaire à 2 décimales (pas de float pour l'argent)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _random_date(rng: np.random.Generator, start: datetime, end: datetime) -> datetime:
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=int(rng.integers(0, seconds)))


# ---------------------------------------------------------------------------
# Construction des lignes (fonctions pures, testables sans base)
# ---------------------------------------------------------------------------
def build_suppliers(n: int, fake: Faker, rng: np.random.Generator) -> list[dict]:
    return [
        {
            "supplier_id": i,
            "name": fake.unique.company(),
            "country": str(rng.choice(COUNTRIES)),
            "lead_time_days": int(rng.integers(3, 31)),            # 3 à 30 jours
            # Loi bêta(8,2) : la plupart des fournisseurs sont fiables (~0.8)
            "reliability_score": round(float(rng.beta(8, 2)), 3),
        }
        for i in range(1, n + 1)
    ]


def build_products(
    n: int, n_suppliers: int, fake: Faker, rng: np.random.Generator
) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        category = str(rng.choice(CATEGORIES))
        # Loi log-normale : beaucoup de produits bon marché, quelques chers.
        cost = max(float(rng.lognormal(mean=2.3, sigma=0.8)), 0.50)
        margin = float(rng.uniform(1.2, 2.0))  # prix = coût x 1.2 à 2.0
        rows.append(
            {
                "product_id": i,
                "sku": f"SKU-{i:05d}",
                "name": f"{category} {fake.word().capitalize()} #{i}",
                "category": category,
                "unit_cost": _money(cost),
                "unit_price": _money(cost * margin),
                "supplier_id": int(rng.integers(1, n_suppliers + 1)),
                "created_at": _random_date(rng, ENTITIES_START, HISTORY_START),
                "active": True,
            }
        )
    return rows


def build_clients(n: int, fake: Faker, rng: np.random.Generator) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        is_b2b = bool(rng.random() < B2B_SHARE)
        rows.append(
            {
                "client_id": i,
                "name": fake.company() if is_b2b else fake.name(),
                "segment": "B2B" if is_b2b else "B2C",
                "city": fake.city(),
                "created_at": _random_date(rng, ENTITIES_START, HISTORY_START),
            }
        )
    return rows


def build_warehouses() -> list[dict]:
    return [
        {"warehouse_id": i, "name": name, "city": city, "capacity": capacity}
        for i, (name, city, capacity) in enumerate(WAREHOUSES, start=1)
    ]


# ---------------------------------------------------------------------------
# Écriture en base
# ---------------------------------------------------------------------------
def _count(conn: Connection, table: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def _insert(conn: Connection, table: str, id_col: str, rows: list[dict]) -> int:
    """Insère en lot ; ignore les lignes déjà présentes. Retourne le nb ajouté."""
    before = _count(conn, table)
    cols = list(rows[0].keys())
    sql = text(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT ({id_col}) DO NOTHING"
    )
    conn.execute(sql, rows)  # liste de dicts = insertion par lot (executemany)

    # On fournit les id nous-mêmes : la séquence IDENTITY n'a donc pas avancé.
    # On la recale, sinon une future insertion sans id tomberait sur id=1 -> conflit.
    conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{id_col}'), "
            f"(SELECT max({id_col}) FROM {table}))"
        )
    )
    return _count(conn, table) - before


def generate_reference_data(
    n_clients: int = 1000,
    n_products: int = 50,
    n_suppliers: int = 10,
    seed: int = SEED,
    engine: Engine | None = None,
) -> dict[str, int]:
    fake = Faker("fr_FR")
    fake.seed_instance(seed)
    rng = np.random.default_rng(seed)

    # L'ordre de génération compte pour la reproductibilité (même flux aléatoire).
    suppliers = build_suppliers(n_suppliers, fake, rng)
    products = build_products(n_products, n_suppliers, fake, rng)
    clients = build_clients(n_clients, fake, rng)
    warehouses = build_warehouses()

    engine = engine or get_engine()
    # engine.begin() = transaction : COMMIT si tout passe, ROLLBACK sinon.
    with engine.begin() as conn:
        # Fournisseurs AVANT produits, à cause de la clé étrangère.
        return {
            "raw.suppliers": _insert(conn, "raw.suppliers", "supplier_id", suppliers),
            "raw.products": _insert(conn, "raw.products", "product_id", products),
            "raw.clients": _insert(conn, "raw.clients", "client_id", clients),
            "raw.warehouses": _insert(conn, "raw.warehouses", "warehouse_id", warehouses),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Génère les données de référence.")
    parser.add_argument("--clients", type=int, default=1000)
    parser.add_argument("--products", type=int, default=50)
    parser.add_argument("--suppliers", type=int, default=10)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    stats = generate_reference_data(
        n_clients=args.clients,
        n_products=args.products,
        n_suppliers=args.suppliers,
        seed=args.seed,
    )
    for table, inserted in stats.items():
        print(f"{table:<16} +{inserted} lignes")


if __name__ == "__main__":
    main()
